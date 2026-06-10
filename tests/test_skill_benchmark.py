import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

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

    def test_export_jetty_payload_has_runbook_contract_and_variant_mounts(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            manifest = self.make_manifest(root)
            fixture = manifest.parent / "fixtures" / "case-1" / "input.txt"
            fixture.parent.mkdir(parents=True)
            fixture.write_text("fixture", encoding="utf-8")
            data = json.loads(manifest.read_text(encoding="utf-8"))
            data["cases"][0]["files"] = ["fixtures/case-1/input.txt"]
            manifest.write_text(json.dumps(data), encoding="utf-8")
            out = root / "jetty-payloads.jsonl"
            args = SimpleNamespace(
                manifest=str(manifest), split="tune", runs_per_variant=1,
                include_old_skill=False, include_ablations=False, allow_missing_prompts=False,
                jetty_collection="skill-evals", jetty_task_prefix=None,
                jetty_agent="claude-code", jetty_model="claude-sonnet-4-6",
                jetty_model_provider="anthropic", jetty_snapshot="python312-uv",
                use_trial_keys=False, out=str(out), dry_run=False,
            )
            sb.export_jetty(args)
            rows = [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines()]
            self.assertEqual([r["harness"]["variant"] for r in rows], ["with_skill", "without_skill"])
            with_row, without_row = rows
            jetty = with_row["jetty_request"]["jetty"]
            self.assertEqual(with_row["jetty_request"]["messages"][1]["content"], "Execute the runbook.")
            self.assertEqual(jetty["model_provider"], "anthropic")
            self.assertEqual(jetty["snapshot"], "python312-uv")
            self.assertEqual(jetty["template_variables"]["results_dir"], "/app/results")
            self.assertIn("task_json", jetty["template_variables"])
            self.assertNotIn("expected_behavior", json.dumps(with_row))
            self.assertNotIn("review_rubric", json.dumps(with_row))
            self.assertIn("{{task_json}}", with_row["jetty_request"]["messages"][0]["content"])
            with_roles = {f["role"] for f in with_row["upload_plan"]["files"]}
            without_roles = {f["role"] for f in without_row["upload_plan"]["files"]}
            self.assertTrue({"task", "skill", "fixture"}.issubset(with_roles))
            self.assertNotIn("skill", without_roles)
            self.assertTrue({"task", "fixture"}.issubset(without_roles))
            without_task_json = next(f for f in without_row["upload_plan"]["files"] if f["role"] == "task")["content"]
            self.assertEqual(json.loads(without_task_json)["skill_files"], [])

    def test_import_jetty_results_roundtrip_can_be_benchmarked(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            manifest = self.make_manifest(root)
            runs = root / "runs"
            jetty_runs = root / "jetty-runs.jsonl"
            completed = {
                "harness": {"skill_name": "demo", "case_id": "case-1", "variant": "with_skill", "run_number": 1, "split": "tune", "run_dir": "case-1/with_skill"},
                "status": "completed",
                "trajectory_id": "traj_1",
                "jetty": {"collection": "skill-evals", "task": "demo-case-1-with-skill-1", "agent": "claude-code", "model": "claude-sonnet-4-6", "model_provider": "anthropic", "snapshot": "python312-uv"},
                "trajectory": {"usage": {"total_tokens": 12}, "elapsed_ms": 34},
                "artifacts": [
                    {"path": "/app/results/output.md", "content": "alpha beta"},
                    {"path": "/app/results/metadata.json", "content": {"total_tool_calls": 2}},
                ],
            }
            failed = {
                "harness": {"skill_name": "demo", "case_id": "case-1", "variant": "without_skill", "run_number": 1, "split": "tune", "run_dir": "case-1/without_skill"},
                "status": "failed",
                "trajectory_id": "traj_2",
                "jetty": {"collection": "skill-evals", "task": "demo-case-1-without-skill-1", "agent": "claude-code", "model": "claude-sonnet-4-6", "model_provider": "anthropic", "snapshot": "python312-uv"},
                "trajectory": {"error": "boom"},
            }
            jetty_runs.write_text(json.dumps(completed) + "\n" + json.dumps(failed) + "\n", encoding="utf-8")
            sb.import_jetty_results(SimpleNamespace(manifest=str(manifest), jetty_runs=str(jetty_runs), runs=str(runs)))
            self.assertEqual((runs / "case-1" / "with_skill" / "output.md").read_text(encoding="utf-8"), "alpha beta")
            meta = json.loads((runs / "case-1" / "with_skill" / "metadata.json").read_text(encoding="utf-8"))
            self.assertEqual(meta["provider"], "jetty")
            self.assertEqual(meta["jetty_trajectory_id"], "traj_1")
            self.assertIn("JETTY FAILURE", (runs / "case-1" / "without_skill" / "output.md").read_text(encoding="utf-8"))
            report = sb.build_benchmark_report(manifest, runs, variants_arg=["with_skill"])
            self.assertEqual(report["summary"]["with_skill"]["mean_objective_pass_rate"], 1.0)

    def test_export_jetty_hidden_prompt_placeholder_is_non_executable(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            manifest = self.make_manifest(root)
            data = json.loads(manifest.read_text(encoding="utf-8"))
            data["cases"] = [{
                "id": "holdout-1",
                "split": "holdout",
                "kind": "behavior",
                "prompt_ref": "holdout/private.md",
                "assertions": [{"name": "has-alpha", "type": "contains", "value": "alpha"}],
            }]
            manifest.write_text(json.dumps(data), encoding="utf-8")
            out = root / "jetty-payloads.jsonl"
            args = SimpleNamespace(
                manifest=str(manifest), split="holdout", runs_per_variant=1,
                include_old_skill=False, include_ablations=False, allow_missing_prompts=True,
                jetty_collection="skill-evals", jetty_task_prefix=None,
                jetty_agent="claude-code", jetty_model="claude-sonnet-4-6",
                jetty_model_provider="anthropic", jetty_snapshot="python312-uv",
                use_trial_keys=False, out=str(out), dry_run=True,
            )
            sb.export_jetty(args)
            row = json.loads(out.read_text(encoding="utf-8").splitlines()[0])
            self.assertFalse(row["harness"]["executable"])

            class ShouldNotCallClient:
                def upload(self, *args, **kwargs):
                    raise AssertionError("non-executable payload should not upload")

            records = list(sb.execute_jetty_payloads([row], client=ShouldNotCallClient()))
            self.assertEqual(records[0]["status"], "failed")
            self.assertIn("non-executable", records[0]["error"])

    def test_run_jetty_uploads_submits_polls_and_replaces_placeholders(self):
        row = {
            "harness": {"skill_name": "demo", "case_id": "case-1", "variant": "with_skill", "run_number": 1, "split": "tune", "run_dir": "case-1/with_skill"},
            "jetty_request": {
                "model": "claude-sonnet-4-6",
                "messages": [{"role": "system", "content": "runbook"}, {"role": "user", "content": "Execute the runbook."}],
                "stream": False,
                "jetty": {
                    "runbook": True,
                    "collection": "skill-evals",
                    "task": "demo-case-1-with-skill-1",
                    "agent": "claude-code",
                    "model_provider": "anthropic",
                    "snapshot": "python312-uv",
                    "template_variables": {"results_dir": "/app/results", "task_json": "upload://task-json"},
                    "file_paths": ["upload://task-json"],
                },
            },
            "upload_plan": {"files": [{"role": "task", "placeholder": "upload://task-json", "content": "{}", "remote_path_hint": "task.json", "private": True}]},
        }

        class FakeClient:
            def __init__(self):
                self.submitted = None
            def upload(self, item, collection):
                self.uploaded = (item, collection)
                return "uploads/task.json"
            def submit(self, request_body):
                self.submitted = request_body
                return {"trajectory_id": "traj_1"}
            def poll(self, collection, task, trajectory_id, *, timeout_s=1800, poll_interval_s=5):
                return {"status": "completed", "artifacts": [{"path": "/app/results/output.md", "content": "alpha beta"}]}

        client = FakeClient()
        records = list(sb.execute_jetty_payloads([row], client=client, timeout_s=1, poll_interval_s=0))
        self.assertEqual(client.uploaded[1], "skill-evals")
        self.assertEqual(client.submitted["jetty"]["template_variables"]["task_json"], "uploads/task.json")
        self.assertEqual(client.submitted["jetty"]["file_paths"], ["uploads/task.json"])
        self.assertEqual(records[0]["status"], "completed")
        self.assertEqual(records[0]["trajectory_id"], "traj_1")


if __name__ == "__main__":
    unittest.main()
