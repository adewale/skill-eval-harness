import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("skill_benchmark", ROOT / "skill_benchmark.py")
sb = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(sb)
TRIGGER_SPEC = importlib.util.spec_from_file_location("run_pi_trigger_eval", ROOT / "run_pi_trigger_eval.py")
tr = importlib.util.module_from_spec(TRIGGER_SPEC)
assert TRIGGER_SPEC.loader is not None
TRIGGER_SPEC.loader.exec_module(tr)


class SkillBenchmarkTests(unittest.TestCase):
    def make_manifest(self, root: Path) -> Path:
        repo = root / "repo"
        (repo / "skill").mkdir(parents=True)
        (repo / "skill" / "SKILL.md").write_text("---\nname: demo\ndescription: Demo skill\n---\n", encoding="utf-8")
        (repo / "evals").mkdir()
        manifest = {
            "version": 1,
            "skill_name": "demo",
            "skill_paths": ["skill/SKILL.md"],
            "variants": ["with_skill", "without_skill"],
            "cases": [
                {
                    "id": "case-1",
                    "split": "tune",
                    "kind": "behavior",
                    "prompt": "Say alpha and beta.",
                    "expected_behavior": ["Say alpha and beta"],
                    "assertions": [
                        {"name": "has-alpha", "type": "contains", "value": "alpha"},
                        {"name": "has-beta", "type": "contains", "value": "beta"},
                        {"name": "quality", "type": "judge", "rubric": ["Complete"]},
                    ],
                }
            ],
            "ablations": [],
        }
        path = repo / "evals" / "shared-benchmark.json"
        path.write_text(json.dumps(manifest), encoding="utf-8")
        return path

    def test_repeated_runs_artifact_outputs_and_flaky_flag(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            manifest = self.make_manifest(root)
            runs = root / "repo" / "eval-runs" / "latest"
            for variant, outputs in {
                "with_skill": ["alpha beta", "alpha only"],
                "without_skill": ["alpha only", "alpha only"],
            }.items():
                for i, text in enumerate(outputs, 1):
                    base = runs / "case-1" / variant / f"run-{i}"
                    base.mkdir(parents=True)
                    if variant == "with_skill" and i == 1:
                        (base / "outputs").mkdir()
                        (base / "outputs" / "answer.md").write_text(text, encoding="utf-8")
                    else:
                        (base / "output.md").write_text(text, encoding="utf-8")
                    (base / "metadata.json").write_text(json.dumps({"elapsed_ms": 1000 * i, "total_tokens": 100 + i}), encoding="utf-8")
            report = sb.build_benchmark_report(manifest, runs)
            self.assertEqual(len(report["results"]), 4)
            self.assertEqual(report["summary"]["with_skill"]["objective_pass_rate"]["n"], 2)
            self.assertAlmostEqual(report["summary"]["with_skill"]["mean_objective_pass_rate"], 0.75)
            flags = report["case_flags"][0]["flags"]
            self.assertIn("flaky repeated pass rates: with_skill", flags)

    def test_judge_results_merge_and_anthropic_grading_shape(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            manifest = self.make_manifest(root)
            runs = root / "repo" / "eval-runs" / "latest"
            base = runs / "case-1" / "with_skill"
            base.mkdir(parents=True)
            (base / "output.md").write_text("alpha beta", encoding="utf-8")
            judge_results = root / "judge.jsonl"
            judge_results.write_text(json.dumps({"judge_task_id": "case-1::with_skill::run-1::quality", "passed": True, "evidence": "complete"}) + "\n", encoding="utf-8")
            report = sb.build_benchmark_report(manifest, runs, variants_arg=["with_skill"], judge_results_path=str(judge_results))
            result = report["results"][0]
            self.assertEqual(result["combined_total"], 3)
            self.assertEqual(result["combined_passed"], 3)
            grading = sb.anthropic_grading_json(result)
            self.assertIn("expectations", grading)
            self.assertEqual(grading["summary"]["pass_rate"], 1.0)
            self.assertTrue(all({"text", "passed", "evidence"}.issubset(e) for e in grading["expectations"]))

    def test_prepare_omits_answer_key_by_default(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            manifest = self.make_manifest(root)
            class Args:
                pass
            Args.manifest = str(manifest)
            Args.include_old_skill = False
            Args.include_ablations = False
            Args.runs_per_variant = 2
            Args.split = "tune"
            Args.out = str(root / "tasks.jsonl")
            Args.allow_missing_prompts = False
            Args.include_answer_key = False
            sb.prepare(Args)
            rows = [json.loads(line) for line in Path(Args.out).read_text(encoding="utf-8").splitlines()]
            self.assertEqual(len(rows), 4)
            self.assertNotIn("expected_behavior", rows[0])
            self.assertEqual(rows[1]["run_dir"], "case-1/with_skill/run-2")

    def test_anthropic_export_contains_required_top_level_fields(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            manifest = self.make_manifest(root)
            runs = root / "repo" / "eval-runs" / "latest"
            for variant in ["with_skill", "without_skill"]:
                base = runs / "case-1" / variant
                base.mkdir(parents=True)
                (base / "output.md").write_text("alpha beta" if variant == "with_skill" else "alpha", encoding="utf-8")
            report = sb.build_benchmark_report(manifest, runs)
            exported = sb.anthropic_benchmark_from_report(report, "skill/SKILL.md")
            self.assertIn("metadata", exported)
            self.assertIn("runs", exported)
            self.assertIn("run_summary", exported)
            self.assertEqual(exported["runs"][0]["configuration"], "with_skill")
            self.assertIn("delta", exported["run_summary"])

    def test_audit_manifest_reports_missing_categories_and_fixtures(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            manifest = self.make_manifest(root)
            report = sb.audit_manifest_report(manifest, min_positive=2, min_negative=1, min_adversarial=1, min_trigger_pos=1, min_trigger_neg=1)
            kinds = {f["kind"] for f in report["findings"]}
            self.assertIn("missing-negative-evals", kinds)
            self.assertIn("missing-adversarial-evals", kinds)
            self.assertIn("missing-hidden-splits", kinds)
            self.assertIn("missing-trigger-no-trigger-cases", kinds)
            self.assertTrue(report["recommended_fixture_repos_files"])

    def test_audit_manifest_run_aware_assertion_discrimination(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            manifest = self.make_manifest(root)
            runs = root / "repo" / "eval-runs" / "latest"
            for variant in ["with_skill", "without_skill"]:
                base = runs / "case-1" / variant
                base.mkdir(parents=True)
                (base / "output.md").write_text("alpha beta", encoding="utf-8")
            report = sb.audit_manifest_report(manifest, runs=str(runs))
            kinds = {f["kind"] for f in report["findings"]}
            self.assertIn("saturated-eval", kinds)
            self.assertIn("non-discriminating-assertions", kinds)

    def test_missing_outputs_do_not_create_no_lift_flags(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            manifest = self.make_manifest(root)
            data = json.loads(manifest.read_text(encoding="utf-8"))
            data["cases"].append({
                "id": "case-2",
                "split": "tune",
                "kind": "behavior",
                "prompt": "Say gamma.",
                "assertions": [{"name": "has-gamma", "type": "contains", "value": "gamma"}],
            })
            manifest.write_text(json.dumps(data), encoding="utf-8")
            runs = root / "repo" / "eval-runs" / "latest"
            for variant in ["with_skill", "without_skill"]:
                base = runs / "case-1" / variant
                base.mkdir(parents=True)
                (base / "output.md").write_text("alpha beta", encoding="utf-8")
            report = sb.build_benchmark_report(manifest, runs)
            flagged_ids = {f["case_id"] for f in report["case_flags"]}
            self.assertNotIn("case-2", flagged_ids)

    def test_trigger_eval_extracts_real_user_prompt(self):
        case = {
            "prompt": "Trigger decision eval. User prompt: write a README\n\nReturn exactly one label first: TRIGGER or NO_TRIGGER."
        }
        self.assertEqual(tr.trigger_query_from_case(case), "write a README")

    def test_trigger_detector_uses_copied_skill_paths_not_bare_skill_name(self):
        copied = [Path("/tmp/pi-trigger-x/skills/good-readme/SKILL.md")]
        repo_event = json.dumps({"tool_input": {"path": "good-readme/README.md"}})
        self.assertEqual(tr.detect_trigger(repo_event, "good-readme", copied), (False, []))
        skill_event = json.dumps({"tool_input": {"path": "/tmp/pi-trigger-x/skills/good-readme/SKILL.md"}})
        triggered, evidence = tr.detect_trigger(skill_event, "good-readme", copied)
        self.assertTrue(triggered)
        self.assertIn("/tmp/pi-trigger-x/skills/good-readme/SKILL.md", evidence[0])

    def test_prepare_includes_input_files(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            manifest = self.make_manifest(root)
            fixture = manifest.parent / "fixtures" / "case-1" / "input.txt"
            fixture.parent.mkdir(parents=True)
            fixture.write_text("fixture", encoding="utf-8")
            data = json.loads(manifest.read_text(encoding="utf-8"))
            data["cases"][0]["files"] = ["fixtures/case-1/input.txt"]
            manifest.write_text(json.dumps(data), encoding="utf-8")
            class Args:
                pass
            Args.manifest = str(manifest)
            Args.include_old_skill = False
            Args.include_ablations = False
            Args.runs_per_variant = 1
            Args.split = "tune"
            Args.out = str(root / "tasks.jsonl")
            Args.allow_missing_prompts = False
            Args.include_answer_key = False
            sb.prepare(Args)
            first = json.loads(Path(Args.out).read_text(encoding="utf-8").splitlines()[0])
            self.assertEqual(first["input_files"], [str(fixture.resolve())])


if __name__ == "__main__":
    unittest.main()
