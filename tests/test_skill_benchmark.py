import contextlib
import importlib.util
import io
import json
import os
import sys
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
SMOKE_SPEC = importlib.util.spec_from_file_location("run_pi_smoke", ROOT / "examples" / "adewale-workspace" / "run_pi_smoke.py")
smoke = importlib.util.module_from_spec(SMOKE_SPEC)
assert SMOKE_SPEC.loader is not None
SMOKE_SPEC.loader.exec_module(smoke)


class SkillBenchmarkTests(unittest.TestCase):
    def test_infra_failure_excluded_from_every_report_view(self):
        # Invariant: an infrastructure failure (execution_valid False) must not
        # affect ANY report view. Adding a crashed with_skill run (rate 0.0) must
        # leave the paired delta, every slice mean, and mean_rate unchanged — if a
        # view forgot the scorable predicate, the 0.0 would drag its rate down.
        def row(variant, rate, valid=True):
            return {"case_id": "c1", "variant": variant, "objective_pass_rate": rate,
                    "combined_pass_rate": rate, "missing_output": False, "execution_valid": valid,
                    "domain": "d", "success_goals": ["g"],
                    "assertions": [{"name": "a", "passed": rate >= 1.0}], "qualitative_assertions": []}
        good, base = row("with_skill", 1.0), row("without_skill", 0.0)
        crashed = row("with_skill", 0.0, valid=False)
        clean, polluted = [good, base], [good, crashed, base]
        variants = ["with_skill", "without_skill"]
        self.assertEqual(sb.build_paired_summary(polluted), sb.build_paired_summary(clean))
        self.assertEqual(sb.build_slice_summary(polluted, variants), sb.build_slice_summary(clean, variants))
        self.assertEqual(sb.mean_rate([good, crashed]), sb.mean_rate([good]))
        self.assertEqual(sb.mean_rate([good, crashed]), 1.0)   # the crash did not drag it to 0.5

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
            # The default judge is soft: its merged verdict fills the graded/soft
            # channel while the combined pass rate is carried by the two gates.
            self.assertEqual(result["combined_total"], 2)
            self.assertEqual(result["combined_passed"], 2)
            self.assertEqual(result["soft_total"], 1)
            self.assertEqual(result["soft_passed"], 1)
            self.assertTrue(result["qualitative_assertions"][0]["passed"])
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

    def test_pi_message_end_trace_normalizes_usage_and_skill_load(self):
        records = [
            {"type": "tool_use", "tool_input": {"path": "/tmp/demo/skill/SKILL.md"}},
            {"type": "message_end", "message": {"role": "assistant", "content": [{"type": "text", "text": "alpha beta"}], "usage": {"input": 10, "output": 5, "totalTokens": 15}}},
        ]
        events, metrics = sb.normalize_trace_records(records, source="pi")
        self.assertEqual([e["type"] for e in events["events"]], ["skill_load", "message"])
        self.assertEqual(metrics["total_tokens"], 15)
        self.assertTrue(metrics["skill_invoked"])

    def test_actual_codex_jsonl_shape_extracts_answer_and_tokens(self):
        records = [
            {"type": "thread.started", "thread_id": "thread_1"},
            {"type": "turn.started"},
            {"type": "item.started", "item": {"id": "item_1", "type": "command_execution", "command": "/bin/zsh -lc \"sed -n '1,80p' skills/good-pr/SKILL.md\"", "status": "in_progress"}},
            {"type": "item.completed", "item": {"id": "item_1", "type": "command_execution", "command": "/bin/zsh -lc \"sed -n '1,80p' skills/good-pr/SKILL.md\"", "aggregated_output": "---", "exit_code": 0, "status": "completed"}},
            {"type": "item.completed", "item": {"id": "item_0", "type": "agent_message", "text": "codex-trace-ok"}},
            {"type": "turn.completed", "usage": {"input_tokens": 100, "cached_input_tokens": 20, "output_tokens": 9, "reasoning_output_tokens": 0}},
        ]
        events, metrics = sb.normalize_trace_records(records, source="codex")
        self.assertEqual(sb.final_answer_from_events(events), "codex-trace-ok")
        self.assertEqual(metrics["commands"], 1)
        self.assertTrue(metrics["skill_invoked"])
        self.assertEqual(metrics["input_tokens"], 100)
        self.assertEqual(metrics["output_tokens"], 9)
        self.assertEqual(metrics["total_tokens"], 109)

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
                "trajectory": {
                    "usage": {"input_tokens": 5, "output_tokens": 7, "total_tokens": 12},
                    "elapsed_ms": 34,
                    "events": [
                        {"type": "tool_call", "tool_input": {"path": "/tmp/demo/skill/SKILL.md"}},
                        {"type": "exec_command", "command": "python -m unittest"},
                    ],
                },
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
            self.assertTrue((runs / "case-1" / "with_skill" / "trace.jsonl").exists())
            events = json.loads((runs / "case-1" / "with_skill" / "events.json").read_text(encoding="utf-8"))
            metrics = json.loads((runs / "case-1" / "with_skill" / "metrics.json").read_text(encoding="utf-8"))
            self.assertTrue(any(e["type"] == "skill_load" for e in events["events"]))
            self.assertEqual(metrics["commands"], 1)
            self.assertEqual(metrics["total_tokens"], 12)
            self.assertIn("JETTY FAILURE", (runs / "case-1" / "without_skill" / "output.md").read_text(encoding="utf-8"))
            report = sb.build_benchmark_report(manifest, runs, variants_arg=["with_skill"])
            self.assertEqual(report["summary"]["with_skill"]["mean_objective_pass_rate"], 1.0)

    def test_import_jetty_persists_ablation_provenance_into_metadata(self):
        # The harness-only ablation provenance must land in the run metadata so the
        # report can verify a materialized tree was mounted (not just trust the dir).
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            manifest = self.make_manifest(root)
            runs = root / "runs"
            jetty_runs = root / "jr.jsonl"
            prov = {"id": "no-rp", "mode": "materialized", "population": "answer", "skill_hash": "abc123", "components": [{"class": "instructions", "mechanism": "section"}]}
            rec = {
                "harness": {"skill_name": "demo", "case_id": "case-1", "variant": "ablation:no-rp", "run_number": 1, "split": "tune", "run_dir": "case-1/ablation:no-rp", "ablation": prov},
                "status": "completed", "trajectory_id": "t1",
                "jetty": {"collection": "c", "task": "t", "agent": "claude-code", "model": "m", "model_provider": "anthropic", "snapshot": "s"},
                "artifacts": [{"path": "/app/results/output.md", "content": "x"}],
            }
            jetty_runs.write_text(json.dumps(rec) + "\n", encoding="utf-8")
            sb.import_jetty_results(SimpleNamespace(manifest=str(manifest), jetty_runs=str(jetty_runs), runs=str(runs)))
            meta = json.loads((runs / "case-1" / "ablation:no-rp" / "metadata.json").read_text(encoding="utf-8"))
            self.assertEqual(meta["ablation"], prov)

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

    def test_pi_smoke_workspace_omits_skill_for_without_skill(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            manifest = self.make_manifest(root)
            repo = manifest.parent.parent
            fixture = manifest.parent / "fixtures" / "input.txt"
            fixture.parent.mkdir()
            fixture.write_text("fixture", encoding="utf-8")
            data = json.loads(manifest.read_text(encoding="utf-8"))
            data["cases"][0]["files"] = ["fixtures/input.txt"]
            with tempfile.TemporaryDirectory() as wd:
                instruction, skill_args, inputs, skill_paths, _ = smoke.materialize_runtime_workspace(data, repo, data["cases"][0], "without_skill", Path(wd))
                self.assertEqual(skill_args, ["--no-skills"])
                self.assertEqual(skill_paths, [])
                self.assertEqual(len(inputs), 1)
                self.assertTrue(str(inputs[0]).startswith(str(Path(wd).resolve())))
                self.assertFalse((Path(wd) / "skills").exists())
                self.assertIn("not present", instruction)
            with tempfile.TemporaryDirectory() as wd:
                _, skill_args, _, skill_paths, _ = smoke.materialize_runtime_workspace(data, repo, data["cases"][0], "with_skill", Path(wd))
                self.assertTrue(skill_paths)
                self.assertIn("--skill", skill_args)
                self.assertTrue(all(str(p.resolve()).startswith(str(Path(wd).resolve())) for p in skill_paths))

    def test_pi_trigger_trace_artifact_writer_uses_detector_evidence(self):
        with tempfile.TemporaryDirectory() as td:
            run_dir = Path(td) / "trigger-run"
            stdout = "\n".join([
                json.dumps({"type": "tool_use", "tool_input": {"path": "/tmp/pi-trigger/skills/demo/SKILL.md"}}),
                json.dumps({"type": "message_end", "message": {"role": "assistant", "content": [{"type": "text", "text": "done"}], "usage": {"input": 3, "output": 2, "totalTokens": 5}}}),
            ]) + "\n"
            result = {"query": "demo", "should_trigger": True, "triggered": True, "pass": True, "elapsed_ms": 50, "returncode": 0, "timed_out": False, "evidence": ["/tmp/pi-trigger/skills/demo/SKILL.md"]}
            tr.write_trigger_trace_artifacts(run_dir, stdout, result)
            metrics = json.loads((run_dir / "metrics.json").read_text(encoding="utf-8"))
            meta = json.loads((run_dir / "metadata.json").read_text(encoding="utf-8"))
            self.assertTrue(metrics["skill_invoked"])
            self.assertEqual(metrics["total_tokens"], 5)
            self.assertEqual(meta["query"], "demo")

    def test_script_assertion_requires_opt_in_and_executes_oracle(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            manifest = self.make_manifest(root)
            oracle = manifest.parent / "oracles" / "oracle.py"
            oracle.parent.mkdir(parents=True)
            oracle.write_text(
                "import pathlib, sys\n"
                "out = pathlib.Path(sys.argv[1]) / 'output.md'\n"
                "text = out.read_text()\n"
                "print('checked output')\n"
                "raise SystemExit(0 if 'alpha beta' in text else 2)\n",
                encoding="utf-8",
            )
            data = json.loads(manifest.read_text(encoding="utf-8"))
            data["cases"][0]["assertions"] = [{
                "name": "oracle-pass",
                "type": "script",
                "command": [sys.executable, "oracles/oracle.py", "{output_dir}"],
                "timeout_s": 5,
            }]
            manifest.write_text(json.dumps(data), encoding="utf-8")
            runs = root / "repo" / "eval-runs" / "latest"
            base = runs / "case-1" / "with_skill"
            base.mkdir(parents=True)
            (base / "output.md").write_text("alpha beta", encoding="utf-8")
            blocked = sb.build_benchmark_report(manifest, runs, variants_arg=["with_skill"])
            self.assertEqual(blocked["results"][0]["objective_pass_rate"], 0.0)
            self.assertIn("--allow-scripts", blocked["results"][0]["assertions"][0]["evidence"])
            allowed = sb.build_benchmark_report(manifest, runs, variants_arg=["with_skill"], allow_scripts=True)
            self.assertEqual(allowed["results"][0]["objective_pass_rate"], 1.0)
            self.assertIn("checked output", allowed["results"][0]["assertions"][0]["evidence"])

    def test_prompt_assertion_leakage_lint_finds_literal_contains_values(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            manifest = self.make_manifest(root)
            findings = sb.prompt_assertion_leakage_findings(sb.load_json(manifest), manifest)
            self.assertTrue(any(f["case_id"] == "case-1" and f["value"] == "alpha" for f in findings))

    def test_judge_command_backend_writes_loadable_results(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            manifest = self.make_manifest(root)
            runs = root / "repo" / "eval-runs" / "latest"
            base = runs / "case-1" / "with_skill"
            base.mkdir(parents=True)
            (base / "output.md").write_text("alpha beta", encoding="utf-8")
            judge = root / "judge.py"
            judge.write_text(
                "import json, sys\n"
                "_ = sys.stdin.read()\n"
                "print('prefix ' + json.dumps({'score': 4, 'passed': True, 'rationale': 'ok {brace}'}) + ' suffix')\n",
                encoding="utf-8",
            )
            out = root / "judge-results.jsonl"
            transcripts = root / "judge-transcripts"
            sb.judge_command(SimpleNamespace(
                manifest=str(manifest), runs=str(runs), split="tune", variant=["with_skill"],
                judge_cmd=f"{sys.executable} {judge}", out=str(out), transcripts=str(transcripts), judge_runs=1,
            ))
            rows = [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(rows[0]["judge_task_id"], "case-1::with_skill::run-1::quality")
            self.assertTrue(rows[0]["passed"])
            self.assertIn("{brace}", rows[0]["evidence"])
            self.assertTrue(any(transcripts.rglob("prompt.md")))

    def test_trace_import_process_and_efficiency_assertions(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            manifest = self.make_manifest(root)
            data = json.loads(manifest.read_text(encoding="utf-8"))
            data["cases"][0]["domain"] = "writing"
            data["cases"][0]["difficulty"] = "core"
            data["cases"][0]["trigger_type"] = "explicit"
            data["cases"][0]["success_goals"] = ["outcome", "process", "efficiency"]
            data["cases"][0]["assertions"] = [
                {"name": "loaded-skill", "type": "skill_invoked", "expected": True},
                {"name": "ran-tests", "type": "command_ran", "pattern": "npm test"},
                {"name": "safe-command", "type": "command_not_ran", "pattern": "rm -rf"},
                {"name": "ordered", "type": "command_order", "patterns": ["npm install", "npm test"]},
                {"name": "command-budget", "type": "command_count_le", "max": 3},
                {"name": "token-budget", "type": "total_tokens_le", "max": 200},
                {"name": "time-budget", "type": "elapsed_seconds_le", "max": 5},
            ]
            manifest.write_text(json.dumps(data), encoding="utf-8")
            run_dir = root / "repo" / "eval-runs" / "latest" / "case-1" / "with_skill"
            run_dir.mkdir(parents=True)
            (run_dir / "output.md").write_text("alpha beta", encoding="utf-8")
            trace = run_dir / "trace.jsonl"
            rows = [
                {"type": "file_read", "path": str(root / "repo" / "skill" / "SKILL.md")},
                {"type": "exec_command", "command": "npm install", "duration_ms": 1000},
                {"type": "exec_command", "command": "npm test", "duration_ms": 1200},
                {"type": "usage", "usage": {"input_tokens": 80, "output_tokens": 20, "total_tokens": 100}},
            ]
            trace.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
            sb.import_trace(SimpleNamespace(source="codex", trace=str(trace), run_dir=str(run_dir), out_events=None, out_metrics=None, write_metadata=True))
            events = json.loads((run_dir / "events.json").read_text(encoding="utf-8"))
            metrics = json.loads((run_dir / "metrics.json").read_text(encoding="utf-8"))
            self.assertEqual([e["type"] for e in events["events"]][:3], ["skill_load", "command", "command"])
            self.assertTrue(metrics["skill_invoked"])
            self.assertEqual(metrics["commands"], 2)
            report = sb.build_benchmark_report(manifest, root / "repo" / "eval-runs" / "latest", variants_arg=["with_skill"])
            result = report["results"][0]
            self.assertEqual(result["objective_pass_rate"], 1.0)
            self.assertEqual(result["process_pass_rate"], 1.0)
            self.assertEqual(result["efficiency_pass_rate"], 1.0)
            self.assertEqual(report["summary"]["with_skill"]["telemetry_availability"]["events"], 1)
            self.assertEqual(report["slice_summary"]["domain"]["writing"]["with_skill"]["runs"], 1)

    def test_variant_scoped_process_assertions_do_not_penalize_other_variants(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            manifest = self.make_manifest(root)
            data = json.loads(manifest.read_text(encoding="utf-8"))
            data["cases"][0]["assertions"] = [
                {"name": "with-skill-load", "type": "skill_invoked", "expected": True, "variants": ["with_skill"]},
                {"name": "without-skill-no-load", "type": "skill_invoked", "expected": False, "variants": ["without_skill"]},
            ]
            manifest.write_text(json.dumps(data), encoding="utf-8")
            runs = root / "repo" / "eval-runs" / "latest"
            for variant, invoked in [("with_skill", True), ("without_skill", False)]:
                base = runs / "case-1" / variant
                base.mkdir(parents=True)
                (base / "output.md").write_text("alpha beta", encoding="utf-8")
                sb.write_trace_artifacts(base, "", source="test", extra_metrics={"skill_invoked": invoked, "skill_invocation_evidence": [variant] if invoked else []}, write_metadata=True)
            report = sb.build_benchmark_report(manifest, runs)
            by_variant = {r["variant"]: r for r in report["results"]}
            self.assertEqual(by_variant["with_skill"]["objective_total"], 1)
            self.assertEqual(by_variant["without_skill"]["objective_total"], 1)
            self.assertEqual(by_variant["with_skill"]["objective_pass_rate"], 1.0)
            self.assertEqual(by_variant["without_skill"]["objective_pass_rate"], 1.0)

    def test_process_and_efficiency_assertions_fail_closed_without_evidence(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            manifest = self.make_manifest(root)
            data = json.loads(manifest.read_text(encoding="utf-8"))
            data["cases"][0]["assertions"] = [
                {"name": "loaded-skill", "type": "skill_invoked", "expected": True},
                {"name": "token-budget", "type": "total_tokens_le", "max": 200},
            ]
            manifest.write_text(json.dumps(data), encoding="utf-8")
            run_dir = root / "repo" / "eval-runs" / "latest" / "case-1" / "with_skill"
            run_dir.mkdir(parents=True)
            (run_dir / "output.md").write_text("alpha beta", encoding="utf-8")
            report = sb.build_benchmark_report(manifest, root / "repo" / "eval-runs" / "latest", variants_arg=["with_skill"])
            result = report["results"][0]
            self.assertEqual(result["objective_pass_rate"], 0.0)
            self.assertIn("missing", result["assertions"][0]["evidence"])
            self.assertIn("missing", result["assertions"][1]["evidence"])

    def test_benchmark_reports_delta_normalized_gain_and_negative_cases(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            manifest = self.make_manifest(root)
            data = json.loads(manifest.read_text(encoding="utf-8"))
            data["cases"][0]["domain"] = "docs"
            data["cases"][0]["difficulty"] = "core"
            data["cases"].append({
                "id": "case-2",
                "split": "tune",
                "kind": "behavior",
                "domain": "docs",
                "difficulty": "extended",
                "prompt": "Say gamma.",
                "assertions": [{"name": "has-gamma", "type": "contains", "value": "gamma"}],
            })
            manifest.write_text(json.dumps(data), encoding="utf-8")
            runs = root / "repo" / "eval-runs" / "latest"
            outputs = {
                ("case-1", "with_skill"): "alpha",
                ("case-1", "without_skill"): "alpha beta",
                ("case-2", "with_skill"): "gamma",
                ("case-2", "without_skill"): "nope",
            }
            for (case_id, variant), text in outputs.items():
                base = runs / case_id / variant
                base.mkdir(parents=True)
                (base / "output.md").write_text(text, encoding="utf-8")
            report = sb.build_benchmark_report(manifest, runs)
            self.assertAlmostEqual(report["paired_summary"]["absolute_delta"], 0.25)
            self.assertEqual(report["paired_summary"]["negative_delta_cases"][0]["case_id"], "case-1")
            self.assertAlmostEqual(report["paired_summary"]["normalized_gain"], 0.5)
            self.assertIn("extended", report["slice_summary"]["difficulty"])

    def test_profile_skill_reports_size_and_reference_warnings(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            manifest = self.make_manifest(root)
            skill = root / "repo" / "skill" / "SKILL.md"
            refs = root / "repo" / "skill" / "references"
            refs.mkdir()
            (refs / "a.md").write_text("one two three\n" * 200, encoding="utf-8")
            (refs / "b.md").write_text("four five six\n" * 200, encoding="utf-8")
            skill.write_text("---\nname: demo\n---\n# Demo\n" + "## Module\ntext\n" * 12, encoding="utf-8")
            report = sb.profile_skill_report(manifest, max_skill_tokens=20, max_references=1, max_modules=3)
            kinds = {f["kind"] for f in report["findings"]}
            self.assertIn("skill-too-large", kinds)
            self.assertIn("many-references", kinds)
            self.assertIn("many-modules", kinds)

    def test_token_overhead_reports_static_and_runtime_pairs(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            manifest = self.make_manifest(root)
            runs = root / "repo" / "eval-runs" / "latest"
            for variant, total, input_tokens, output_tokens in [
                ("with_skill", 150, 120, 30),
                ("without_skill", 60, 50, 10),
            ]:
                base = runs / "case-1" / variant
                base.mkdir(parents=True)
                (base / "output.md").write_text("alpha beta", encoding="utf-8")
                (base / "metrics.json").write_text(json.dumps({
                    "total_tokens": total,
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                    "skill_invoked": variant == "with_skill",
                }), encoding="utf-8")
            report = sb.paired_token_overhead_report(manifest, runs=runs)
            self.assertEqual(report["summary"]["paired_runtime_rows"], 1)
            self.assertEqual(report["pairs"][0]["total_token_delta"], 90)
            self.assertEqual(report["pairs"][0]["input_token_delta"], 70)
            self.assertEqual(report["pairs"][0]["objective_delta"], 0.0)
            self.assertEqual(report["pairs"][0]["objective_lift_per_1k_total_tokens"], 0.0)
            self.assertGreater(report["summary"]["static_skill_tokens"], 0)

    def test_command_assertions_match_command_inputs_not_outputs(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            manifest = self.make_manifest(root)
            data = json.loads(manifest.read_text(encoding="utf-8"))
            data["cases"][0]["assertions"] = [
                {"name": "did-not-run-rm", "type": "command_not_ran", "pattern": "rm -rf"},
                {"name": "ran-tests", "type": "command_ran", "pattern": "npm test"},
            ]
            manifest.write_text(json.dumps(data), encoding="utf-8")
            run_dir = root / "repo" / "eval-runs" / "latest" / "case-1" / "with_skill"
            run_dir.mkdir(parents=True)
            (run_dir / "output.md").write_text("alpha beta", encoding="utf-8")
            (run_dir / "events.json").write_text(json.dumps({
                "schema_version": 1,
                "source": "test",
                "events": [{
                    "index": 1,
                    "type": "command",
                    "name": "bash",
                    "status": "completed",
                    "input_summary": "echo harmless",
                    "output_summary": "docs mention npm test and rm -rf as examples",
                }],
            }), encoding="utf-8")
            report = sb.build_benchmark_report(manifest, root / "repo" / "eval-runs" / "latest", variants_arg=["with_skill"])
            assertions = {a["name"]: a for a in report["results"][0]["assertions"]}
            self.assertTrue(assertions["did-not-run-rm"]["passed"])
            self.assertFalse(assertions["ran-tests"]["passed"])

    def test_run_codex_writes_trace_output_and_metrics(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            manifest = self.make_manifest(root)
            tasks = root / "tasks.jsonl"
            rows = sb.prepared_task_rows(manifest, sb.load_json(manifest))
            tasks.write_text("".join(json.dumps(row) + "\n" for row in rows[:1]), encoding="utf-8")
            fake = root / "fake_codex.py"
            fake.write_text(
                "import json, sys\n"
                "_prompt = sys.stdin.read()\n"
                "print(json.dumps({'type': 'exec_command', 'command': 'npm test'}))\n"
                "print(json.dumps({'role': 'assistant', 'content': 'alpha beta'}))\n",
                encoding="utf-8",
            )
            runs = root / "runs"
            sb.run_codex(SimpleNamespace(tasks=str(tasks), runs=str(runs), codex_cmd=f"{sys.executable} {fake}", timeout=5))
            base = runs / "case-1" / "with_skill"
            self.assertTrue((base / "trace.jsonl").exists())
            self.assertEqual((base / "output.md").read_text(encoding="utf-8"), "alpha beta")
            metrics = json.loads((base / "metrics.json").read_text(encoding="utf-8"))
            self.assertEqual(metrics["commands"], 1)
            meta = json.loads((base / "metadata.json").read_text(encoding="utf-8"))
            self.assertEqual(meta["provider"], "codex")

    def test_run_codex_malformed_jsonl_still_writes_failure_artifacts(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            manifest = self.make_manifest(root)
            tasks = root / "tasks.jsonl"
            rows = sb.prepared_task_rows(manifest, sb.load_json(manifest))
            tasks.write_text(json.dumps(rows[0]) + "\n", encoding="utf-8")
            fake = root / "bad_codex.py"
            fake.write_text("import sys\nprint('{not json')\nprint('plain diagnostic')\nsys.exit(2)\n", encoding="utf-8")
            runs = root / "runs"
            sb.run_codex(SimpleNamespace(tasks=str(tasks), runs=str(runs), codex_cmd=f"{sys.executable} {fake}", timeout=5))
            base = runs / "case-1" / "with_skill"
            self.assertIn("CODEX FAILURE", (base / "output.md").read_text(encoding="utf-8"))
            metrics = json.loads((base / "metrics.json").read_text(encoding="utf-8"))
            self.assertIn("parse_errors", metrics)
            meta = json.loads((base / "metadata.json").read_text(encoding="utf-8"))
            self.assertEqual(meta["returncode"], 2)

    def test_run_codex_rejects_unsafe_run_dir(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            tasks = root / "tasks.jsonl"
            tasks.write_text(json.dumps({"case_id": "case", "variant": "with_skill", "run_dir": "../outside", "prompt": "x"}) + "\n", encoding="utf-8")
            with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
                sb.run_codex(SimpleNamespace(tasks=str(tasks), runs=str(root / "runs"), codex_cmd=f"{sys.executable} -c 'print(1)'", timeout=5))
            self.assertFalse((root / "outside").exists())

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


SKILL_FIXTURE = """\
---
name: good-pr
description: Review pull requests for correctness and tests. Use when reviewing a PR or diff.
when_to_use: When asked to review a PR, diff, or patch.
allowed-tools: Read, Grep
---

# Good PR review

Review like a careful maintainer.

## Review checklist

<!-- ablation:no-scope:start -->
- Scope: flag unrelated changes and ask to split them.
<!-- ablation:no-scope:end -->
- Tests: confirm the tests would fail on the pre-change code.
- Naming: match the surrounding conventions.

## Regression-proof requirement

Require a test that fails without the fix and passes with it.

### How to check

Revert the fix mentally and re-run.

```text
## (anti-pattern) toBeDefined as the only assertion
```

## Severity

Pick a verdict. See [the severity guide](references/severity.md).
"""


class SkillAblationTests(unittest.TestCase):
    def build(self, root: Path, *, skill_paths=None, ablations=None) -> Path:
        repo = root / "repo"
        skill_dir = repo / "skills" / "good-pr"
        (skill_dir / "references").mkdir(parents=True, exist_ok=True)
        (skill_dir / "SKILL.md").write_text(SKILL_FIXTURE, encoding="utf-8")
        (skill_dir / "references" / "severity.md").write_text("# Severity\n\nBlocking, Minor, Clean.\n", encoding="utf-8")
        (repo / "evals").mkdir(exist_ok=True)
        manifest = {
            "version": 1,
            "skill_name": "good-pr",
            "skill_paths": skill_paths or ["skills/good-pr/SKILL.md"],
            "variants": ["with_skill", "without_skill"],
            "cases": [{"id": "c1", "split": "tune", "prompt": "Review.", "assertions": [{"name": "a", "type": "contains", "value": "x"}]}],
            "ablations": ablations if ablations is not None else [],
        }
        path = repo / "evals" / "shared-benchmark.json"
        path.write_text(json.dumps(manifest), encoding="utf-8")
        return path

    def materialize_one(self, root: Path, ablation: dict, out_name="out") -> dict:
        path = self.build(root, ablations=[ablation])
        manifest = sb.validate_manifest(path)
        repo_root = sb.repo_root_for_manifest(path)
        res = sb.materialize_ablation(repo_root, manifest, ablation, root / out_name)
        return res

    def skill_text(self, res: dict) -> str:
        return Path(res["skill_files"]["skills/good-pr/SKILL.md"]).read_text(encoding="utf-8")

    def test_section_removal_is_fence_aware(self):
        with tempfile.TemporaryDirectory() as td:
            res = self.materialize_one(Path(td), {"id": "no-regression-proof", "removed_component": "regression-proof", "mechanism": "section", "target": {"heading": "## Regression-proof requirement"}})
            text = self.skill_text(res)
            self.assertNotIn("Regression-proof requirement", text)
            self.assertNotIn("(anti-pattern)", text)   # the fenced block went with the section
            self.assertIn("## Severity", text)          # stopped at the real next heading, not the in-fence one
            self.assertIn("## Review checklist", text)
            self.assertEqual(res["population"], "answer")

    def test_frontmatter_field_removal(self):
        with tempfile.TemporaryDirectory() as td:
            res = self.materialize_one(Path(td), {"id": "no-tools", "removed_component": "tool preapproval", "mechanism": "frontmatter_field", "target": {"field": "allowed-tools"}})
            text = self.skill_text(res)
            self.assertNotIn("allowed-tools", text)
            self.assertIn("name: good-pr", text)
            self.assertIn("description:", text)

    def test_list_item_removal(self):
        with tempfile.TemporaryDirectory() as td:
            res = self.materialize_one(Path(td), {"id": "no-test-bullet", "removed_component": "test bullet", "mechanism": "list_item", "target": {"section": "## Review checklist", "contains": ["pre-change code"]}})
            text = self.skill_text(res)
            self.assertNotIn("fail on the pre-change code", text)
            self.assertIn("Naming: match", text)

    def test_reference_pointer_unlinks_but_keeps_file(self):
        with tempfile.TemporaryDirectory() as td:
            res = self.materialize_one(Path(td), {"id": "no-sev-ptr", "removed_component": "severity ref", "mechanism": "reference", "target": {"path": "references/severity.md", "remove": "pointer"}})
            text = self.skill_text(res)
            self.assertNotIn("](references/severity.md)", text)
            self.assertIn("the severity guide", text)   # visible text kept; no new prose
            self.assertTrue((Path(res["dir"]) / "skills_good-pr_SKILL.md" / "references" / "severity.md").exists())

    def test_reference_content_deletes_file_keeps_pointer(self):
        with tempfile.TemporaryDirectory() as td:
            res = self.materialize_one(Path(td), {"id": "no-sev-file", "removed_component": "severity ref", "mechanism": "reference", "target": {"path": "references/severity.md", "remove": "content"}})
            text = self.skill_text(res)
            self.assertIn("](references/severity.md)", text)
            self.assertFalse((Path(res["dir"]) / "skills_good-pr_SKILL.md" / "references" / "severity.md").exists())

    def test_patch_deletion_only_ok_and_plus_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            lines = SKILL_FIXTURE.split("\n")
            n = lines.index("Revert the fix mentally and re-run.") + 1
            (root / "patch.diff").parent.mkdir(parents=True, exist_ok=True)
            # deletion-only patch authored under the repo
            path = self.build(root)
            repo = path.parent.parent
            (repo / "evals" / "ablations").mkdir(parents=True)
            (repo / "evals" / "ablations" / "p.patch").write_text(f"@@ -{n},1 +{n},0 @@\n-Revert the fix mentally and re-run.\n", encoding="utf-8")
            manifest = sb.validate_manifest(path)
            ab = {"id": "weaken", "removed_component": "revert check", "mechanism": "patch", "target": {"patch": "evals/ablations/p.patch"}}
            res = sb.materialize_ablation(sb.repo_root_for_manifest(path), manifest, ab, root / "out")
            self.assertNotIn("Revert the fix mentally", self.skill_text(res))
            # a '+'-bearing patch is a swap, not an ablation
            with self.assertRaises(sb.AblationError):
                sb.patch_delete_ops(SKILL_FIXTURE, f"@@ -{n},1 +{n},1 @@\n-Revert the fix mentally and re-run.\n+Optionally revert.\n")

    def _patch_ablation(self, root: Path, patch_text: str, *, cls=None):
        """Build a repo with a patch file and return (manifest, repo_root, ablation)."""
        path = self.build(root)
        repo = path.parent.parent
        (repo / "evals" / "ablations").mkdir(parents=True, exist_ok=True)
        (repo / "evals" / "ablations" / "p.patch").write_text(patch_text, encoding="utf-8")
        manifest = sb.validate_manifest(path)
        tgt = {"patch": "evals/ablations/p.patch"}
        ab = {"id": "p", "removed_component": "x", "mechanism": "patch", "target": tgt}
        if cls:
            ab["class"] = cls
        return manifest, sb.repo_root_for_manifest(path), ab

    def test_patch_discovery_class_deletes_frontmatter_ok(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            lines = SKILL_FIXTURE.split("\n")
            n = lines.index("when_to_use: When asked to review a PR, diff, or patch.") + 1
            manifest, repo_root, ab = self._patch_ablation(
                root, f"@@ -{n},1 +{n},0 @@\n-when_to_use: When asked to review a PR, diff, or patch.\n", cls="discovery")
            res = sb.materialize_ablation(repo_root, manifest, ab, root / "out")
            text = self.skill_text(res)
            self.assertNotIn("when_to_use", text)
            self.assertEqual(res["population"], "trigger")   # discovery -> trigger cases

    def test_patch_instructions_class_on_frontmatter_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            lines = SKILL_FIXTURE.split("\n")
            n = lines.index("when_to_use: When asked to review a PR, diff, or patch.") + 1
            # default class for a patch is instructions; deleting a frontmatter line must be rejected
            manifest, repo_root, ab = self._patch_ablation(
                root, f"@@ -{n},1 +{n},0 @@\n-when_to_use: When asked to review a PR, diff, or patch.\n")
            with self.assertRaises(sb.AblationError) as cm:
                sb.materialize_ablation(repo_root, manifest, ab, root / "out")
            self.assertIn("frontmatter", str(cm.exception))

    def test_patch_discovery_class_on_body_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            lines = SKILL_FIXTURE.split("\n")
            n = lines.index("Revert the fix mentally and re-run.") + 1
            manifest, repo_root, ab = self._patch_ablation(
                root, f"@@ -{n},1 +{n},0 @@\n-Revert the fix mentally and re-run.\n", cls="discovery")
            with self.assertRaises(sb.AblationError) as cm:
                sb.materialize_ablation(repo_root, manifest, ab, root / "out")
            self.assertIn("body", str(cm.exception))

    def test_patch_discovery_class_on_runtime_field_rejected(self):
        # allowed-tools is a RUNTIME field; a discovery patch (routed to trigger
        # cases) must not delete it.
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            lines = SKILL_FIXTURE.split("\n")
            n = lines.index("allowed-tools: Read, Grep") + 1
            manifest, repo_root, ab = self._patch_ablation(
                root, f"@@ -{n},1 +{n},0 @@\n-allowed-tools: Read, Grep\n", cls="discovery")
            with self.assertRaises(sb.AblationError) as cm:
                sb.materialize_ablation(repo_root, manifest, ab, root / "out")
            self.assertIn("non-discovery", str(cm.exception))

    def test_patch_runtime_class_on_runtime_field_ok(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            lines = SKILL_FIXTURE.split("\n")
            n = lines.index("allowed-tools: Read, Grep") + 1
            manifest, repo_root, ab = self._patch_ablation(
                root, f"@@ -{n},1 +{n},0 @@\n-allowed-tools: Read, Grep\n", cls="runtime")
            res = sb.materialize_ablation(repo_root, manifest, ab, root / "out")
            self.assertNotIn("allowed-tools", self.skill_text(res))
            self.assertEqual(res["population"], "answer")   # runtime -> answer cases, not trigger

    def test_patch_runtime_class_on_discovery_field_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            lines = SKILL_FIXTURE.split("\n")
            n = lines.index("when_to_use: When asked to review a PR, diff, or patch.") + 1
            manifest, repo_root, ab = self._patch_ablation(
                root, f"@@ -{n},1 +{n},0 @@\n-when_to_use: When asked to review a PR, diff, or patch.\n", cls="runtime")
            with self.assertRaises(sb.AblationError) as cm:
                sb.materialize_ablation(repo_root, manifest, ab, root / "out")
            self.assertIn("discovery frontmatter field", str(cm.exception))

    def test_patch_runtime_cannot_gut_block_scalar_discovery_field(self):
        # Bypass the OLD line-regex heuristic: deleting the INDENTED body of a
        # block-scalar `description` (a discovery field) under a runtime patch. The
        # deleted lines don't start with a column-0 key, so the heuristic saw no
        # field and allowed it; structural ownership attributes them to description.
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = root / "repo"; sd = repo / "skills" / "good-pr"; sd.mkdir(parents=True)
            skill = ("---\nname: good-pr\ndescription: >\n  first line of the description\n"
                     "  second line of the description\nallowed-tools: Read\n---\n\n# Body\n\nText.\n")
            (sd / "SKILL.md").write_text(skill, encoding="utf-8")
            (repo / "evals" / "ablations").mkdir(parents=True)
            m = {"version": 1, "skill_name": "good-pr", "skill_paths": ["skills/good-pr/SKILL.md"], "variants": ["with_skill", "without_skill"], "cases": [{"id": "c", "split": "tune", "prompt": "x", "assertions": [{"name": "a", "type": "contains", "value": "x"}]}], "ablations": []}
            p = repo / "evals" / "shared-benchmark.json"; p.write_text(json.dumps(m), encoding="utf-8")
            lines = skill.split("\n")
            n = lines.index("  first line of the description") + 1
            (repo / "evals" / "ablations" / "p.patch").write_text(
                f"@@ -{n},2 +{n},0 @@\n-  first line of the description\n-  second line of the description\n", encoding="utf-8")
            manifest = sb.validate_manifest(p)
            ab = {"id": "p", "removed_component": "x", "mechanism": "patch", "class": "runtime", "target": {"patch": "evals/ablations/p.patch"}}
            with self.assertRaises(sb.AblationError) as cm:
                sb.materialize_ablation(sb.repo_root_for_manifest(p), manifest, ab, root / "out")
            self.assertIn("discovery frontmatter field 'description'", str(cm.exception))

    def test_patch_frontmatter_fence_deletion_rejected(self):
        # Deleting a structural line (the closing '---') belongs to no field.
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            lines = SKILL_FIXTURE.split("\n")
            close = lines.index("---", 1) + 1
            manifest, repo_root, ab = self._patch_ablation(root, f"@@ -{close},1 +{close},0 @@\n----\n", cls="discovery")
            with self.assertRaises(sb.AblationError) as cm:
                sb.materialize_ablation(repo_root, manifest, ab, root / "out")
            self.assertIn("outside any field", str(cm.exception))

    def test_patch_spanning_frontmatter_and_body_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            lines = SKILL_FIXTURE.split("\n")
            close = lines.index("---", 1) + 1   # 1-based line of the closing frontmatter fence
            # One hunk deletes the closing '---' (frontmatter) plus the blank line
            # and heading (body): a deletion that crosses the layer boundary.
            patch = (
                f"@@ -{close},3 +{close},0 @@\n"
                "----\n"               # tag '-' + content '---' (the closing fence)
                "-\n"                  # tag '-' + empty content (the blank line)
                "-# Good PR review\n"  # tag '-' + the heading
            )
            manifest, repo_root, ab = self._patch_ablation(root, patch, cls="discovery")
            with self.assertRaises(sb.AblationError) as cm:
                sb.materialize_ablation(repo_root, manifest, ab, root / "out")
            self.assertIn("both", str(cm.exception))

    def test_output_dir_overlapping_skill_root_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            ab = {"id": "x", "removed_component": "sev", "mechanism": "section", "target": {"heading": "## Severity"}}
            path = self.build(root, ablations=[ab])
            manifest = sb.validate_manifest(path)
            repo_root = sb.repo_root_for_manifest(path)
            skill_dir = repo_root / "skills" / "good-pr"
            with self.assertRaises(sb.AblationError):                 # equal to a skill root
                sb.materialize_ablation(repo_root, manifest, ab, skill_dir)
            with self.assertRaises(sb.AblationError):                 # inside a skill root
                sb.materialize_ablation(repo_root, manifest, ab, skill_dir / "out")
            with self.assertRaises(sb.AblationError):                 # contains a skill root
                sb.materialize_ablation(repo_root, manifest, ab, repo_root)
            # a sibling output dir is fine
            res = sb.materialize_ablation(repo_root, manifest, ab, root / "safe-out")
            self.assertEqual(res["mode"], "materialized")

    def test_bad_ablation_dir_does_not_mutate_source_tree_before_rejecting(self):
        # An --ablation-dir INSIDE a source skill root is rejected, but the rejection
        # must happen BEFORE _ensure_ablation_dir creates/marks anything — otherwise
        # the command writes a .skill-ablation-dir marker into the live skill tree.
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            ab = {"id": "x", "removed_component": "sev", "mechanism": "section", "target": {"heading": "## Severity"}}
            path = self.build(root, ablations=[ab])
            manifest = sb.validate_manifest(path)
            repo_root = sb.repo_root_for_manifest(path)
            skill_dir = repo_root / "skills" / "good-pr"
            before = {p.relative_to(skill_dir).as_posix() for p in skill_dir.rglob("*")}
            bad_out = skill_dir / "_ablations"                 # inside a source skill root
            with self.assertRaises(SystemExit):
                sb.materialize_declared_ablations(repo_root, manifest, bad_out)
            self.assertFalse(bad_out.exists())                 # never created
            self.assertEqual({p.relative_to(skill_dir).as_posix() for p in skill_dir.rglob("*")}, before)

    def test_bad_ablation_dir_does_not_clear_owned_dir_before_rejecting(self):
        # A harness-owned dir (has the marker) passed as --out-dir but sitting inside a
        # skill root must NOT be cleared before the containment gate rejects it.
        import argparse
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            ab = {"id": "x", "removed_component": "sev", "mechanism": "section", "target": {"heading": "## Severity"}}
            path = self.build(root, ablations=[ab])
            repo_root = sb.repo_root_for_manifest(path)
            bad_out = repo_root / "skills" / "good-pr" / "_ablations"   # inside a skill root
            bad_out.mkdir()
            (bad_out / sb._ABLATION_MARKER).write_text("owned\n", encoding="utf-8")
            sentinel = bad_out / "keep.txt"
            sentinel.write_text("precious", encoding="utf-8")
            with self.assertRaises(SystemExit):
                sb.materialize_ablations(argparse.Namespace(manifest=str(path), out_dir=str(bad_out), out=None))
            self.assertTrue(sentinel.exists())                 # not cleared before reject
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "precious")

    def test_overlapping_ancestor_skill_roots_rejected(self):
        # A root-level SKILL.md (copy-dir = repo root) alongside skills/audit/SKILL.md:
        # copying the ancestor would include an UNABLATED duplicate of the audit skill.
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = root / "repo"
            (repo / "skills" / "audit").mkdir(parents=True)
            (repo / "SKILL.md").write_text("---\nname: top\ndescription: Top. Use for top.\n---\n\n# Top\n\n## A\n\nbody\n", encoding="utf-8")
            (repo / "skills" / "audit" / "SKILL.md").write_text("---\nname: audit\ndescription: Audit. Use for audits.\n---\n\n# Audit\n\n## A\n\nx\n", encoding="utf-8")
            (repo / "evals").mkdir()
            m = {"version": 1, "skill_name": "top", "skill_paths": ["SKILL.md", "skills/audit/SKILL.md"], "variants": ["with_skill", "without_skill"], "cases": [{"id": "c", "split": "tune", "prompt": "x", "assertions": [{"name": "a", "type": "contains", "value": "x"}]}], "ablations": []}
            p = repo / "evals" / "shared-benchmark.json"
            p.write_text(json.dumps(m), encoding="utf-8")
            manifest = sb.validate_manifest(p)
            repo_root = sb.repo_root_for_manifest(p)
            ab = {"id": "x", "removed_component": "audit-a", "mechanism": "section", "class": "instructions", "target": {"skill_root": "skills/audit/SKILL.md", "heading": "## A"}}
            with self.assertRaises(sb.AblationError) as cm:
                sb.materialize_ablation(repo_root, manifest, ab, root / "out")
            self.assertIn("ancestor", str(cm.exception))
            with self.assertRaises(sb.AblationError):   # the canonical with_skill tree rejects too
                sb.build_canonical_skill_tree(repo_root, manifest, root / "wst")

    def test_multi_component_is_order_independent(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            a = {"mechanism": "frontmatter_field", "class": "runtime", "target": {"field": "allowed-tools"}}
            b = {"mechanism": "section", "class": "instructions", "target": {"heading": "## Regression-proof requirement"}}
            res1 = self.materialize_one(root, {"id": "combo", "removed_component": "two", "components": [a, b]}, out_name="o1")
            res2 = self.materialize_one(root, {"id": "combo", "removed_component": "two", "components": [b, a]}, out_name="o2")
            self.assertEqual(self.skill_text(res1), self.skill_text(res2))
            self.assertNotIn("allowed-tools", self.skill_text(res1))
            self.assertNotIn("Regression-proof requirement", self.skill_text(res1))

    def test_overlapping_components_refused(self):
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(sb.AblationError):
                self.materialize_one(Path(td), {"id": "overlap", "removed_component": "x", "components": [
                    {"mechanism": "section", "class": "instructions", "target": {"heading": "## Review checklist"}},
                    {"mechanism": "list_item", "class": "instructions", "target": {"section": "## Review checklist", "contains": ["Naming"]}},
                ]})

    def test_required_field_preservation_blocks_description_removal(self):
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(sb.AblationError):
                self.materialize_one(Path(td), {"id": "no-desc", "removed_component": "desc", "mechanism": "frontmatter_field", "class": "discovery", "target": {"field": "description"}})

    def test_layer_cohesion_refuses_discovery_plus_answer(self):
        with self.assertRaises(sb.AblationError):
            sb.derived_population([
                {"mechanism": "frontmatter_field", "target": {"field": "when_to_use"}},
                {"mechanism": "section", "target": {"heading": "## x"}},
            ])

    def test_materialization_is_deterministic(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            ab = {"id": "no-regression-proof", "removed_component": "rp", "mechanism": "section", "target": {"heading": "## Regression-proof requirement"}}
            r1 = self.materialize_one(root, ab, out_name="a")
            r2 = self.materialize_one(root, ab, out_name="b")
            self.assertEqual(self.skill_text(r1), self.skill_text(r2))

    def test_instruction_simulated_has_no_components(self):
        self.assertEqual(sb.ablation_components({"id": "x", "removed_component": "y"}), [])
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            path = self.build(root, ablations=[{"id": "x", "removed_component": "y"}])
            with self.assertRaises(sb.AblationError):
                sb.materialize_ablation(sb.repo_root_for_manifest(path), sb.validate_manifest(path), {"id": "x", "removed_component": "y"}, root / "out")

    def test_validate_rejects_bad_and_duplicate_ids(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            with self.assertRaises(SystemExit):
                sb.validate_manifest(self.build(root, ablations=[{"id": "Bad_ID", "removed_component": "x"}]))
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            with self.assertRaises(SystemExit):
                sb.validate_manifest(self.build(root, ablations=[{"id": "dup", "removed_component": "x"}, {"id": "dup", "removed_component": "y"}]))

    def test_validate_rejects_path_traversal_and_missing_skill_root(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            with self.assertRaises(SystemExit):
                sb.validate_manifest(self.build(root, ablations=[{"id": "esc", "removed_component": "x", "mechanism": "reference", "target": {"path": "../../etc/passwd", "remove": "content"}}]))
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            with self.assertRaises(SystemExit):
                sb.validate_manifest(self.build(root, skill_paths=["SKILL.md", "skills/audit/SKILL.md"], ablations=[{"id": "noroot", "removed_component": "x", "mechanism": "section", "target": {"heading": "## X"}}]))


class AblationRunnerIntegrationTests(unittest.TestCase):
    SECTION_ABL = {"id": "no-rp", "removed_component": "regression-proof", "mechanism": "section", "class": "instructions", "target": {"heading": "## Regression-proof requirement"}}
    DISCO_ABL = {"id": "no-wtu", "removed_component": "when-to-use", "mechanism": "frontmatter_field", "class": "discovery", "target": {"field": "when_to_use"}}
    SIM_ABL = {"id": "sim", "removed_component": "something"}

    def repo(self, root: Path, ablations: list) -> Path:
        repo = root / "repo"
        sd = repo / "skills" / "good-pr"
        (sd / "references").mkdir(parents=True, exist_ok=True)
        (sd / "SKILL.md").write_text(SKILL_FIXTURE, encoding="utf-8")
        (sd / "references" / "severity.md").write_text("# Severity\n\nBlocking.\n", encoding="utf-8")
        (repo / "evals").mkdir(exist_ok=True)
        manifest = {
            "version": 1, "skill_name": "good-pr", "skill_paths": ["skills/good-pr/SKILL.md"],
            "variants": ["with_skill", "without_skill"],
            "cases": [
                {"id": "ans", "split": "tune", "kind": "behavior", "prompt": "Review.", "assertions": [{"name": "a", "type": "contains", "value": "x"}]},
                {"id": "trig", "split": "tune", "kind": "trigger", "prompt": "Trigger decision eval. User prompt: review my PR", "expected_behavior": ["should trigger"], "assertions": []},
            ],
            "ablations": ablations,
        }
        p = repo / "evals" / "shared-benchmark.json"
        p.write_text(json.dumps(manifest), encoding="utf-8")
        return p

    def test_pi_smoke_mounts_materialized_tree_not_original(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            p = self.repo(root, [self.SECTION_ABL])
            manifest = sb.validate_manifest(p)
            repo_root = sb.repo_root_for_manifest(p)
            with tempfile.TemporaryDirectory() as wd:
                instr, skill_args, _, _, _ = smoke.materialize_runtime_workspace(manifest, repo_root, manifest["cases"][0], "ablation:no-rp", Path(wd))
                mounted = Path(skill_args[skill_args.index("--skill") + 1]).read_text(encoding="utf-8")
                self.assertNotIn("Regression-proof requirement", mounted)   # ablated content reaches the runner
                self.assertNotIn("ignore/remove", instr)                    # not the instruction-simulated text
                self.assertNotIn("instruction-simulated", instr)

    def test_pi_smoke_instruction_simulated_mounts_real_skill(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            p = self.repo(root, [self.SIM_ABL])
            manifest = sb.validate_manifest(p)
            repo_root = sb.repo_root_for_manifest(p)
            with tempfile.TemporaryDirectory() as wd:
                instr, skill_args, _, _, _ = smoke.materialize_runtime_workspace(manifest, repo_root, manifest["cases"][0], "ablation:sim", Path(wd))
                self.assertIn("simulate this ablation", instr)   # the directive from variant_instruction (the owner)
                self.assertIn("Regression-proof requirement", Path(skill_args[skill_args.index("--skill") + 1]).read_text(encoding="utf-8"))

    def test_pi_smoke_materialized_arm_is_blind_no_path_leak(self):
        # The materialized arm must be indistinguishable from with_skill: same
        # mount paths, same instruction, and NOTHING in the model-visible
        # workspace, mount path, or prompt that names the ablation id.
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            p = self.repo(root, [self.SECTION_ABL])   # id "no-rp"
            manifest = sb.validate_manifest(p)
            repo_root = sb.repo_root_for_manifest(p)
            with tempfile.TemporaryDirectory() as wd_w, tempfile.TemporaryDirectory() as wd_a:
                instr_w, args_w, _, paths_w, _ = smoke.materialize_runtime_workspace(manifest, repo_root, manifest["cases"][0], "with_skill", Path(wd_w))
                instr_a, args_a, _, paths_a, prov = smoke.materialize_runtime_workspace(manifest, repo_root, manifest["cases"][0], "ablation:no-rp", Path(wd_a))
                # identical workspace-relative mount names
                rel_w = [str(Path(x).relative_to(wd_w)) for x in paths_w]
                rel_a = [str(Path(x).relative_to(wd_a)) for x in paths_a]
                self.assertEqual(rel_w, rel_a)
                self.assertTrue(all(r.startswith("skills/root-") for r in rel_a))
                # ablated content actually reaches the runner
                self.assertNotIn("Regression-proof requirement", Path(args_a[args_a.index("--skill") + 1]).read_text(encoding="utf-8"))
                self.assertIn("Regression-proof requirement", Path(args_w[args_w.index("--skill") + 1]).read_text(encoding="utf-8"))
                # the model-visible instruction is byte-identical (blinding)
                self.assertEqual(instr_w, instr_a)
                # no ablation id leaks into the prompt, mount path, or workspace tree
                self.assertNotIn("no-rp", instr_a)
                self.assertNotIn("no-rp", " ".join(rel_a))
                tree = [str(q.relative_to(wd_a)) for q in Path(wd_a).rglob("*")]
                self.assertFalse(any("no-rp" in entry for entry in tree), f"ablation id leaked into workspace: {tree}")
                # provenance is still returned for the harness-only record
                self.assertEqual(prov["mode"], "materialized")

    def test_pi_trigger_mounts_materialized_discovery_skill(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            p = self.repo(root, [self.DISCO_ABL])
            manifest = sb.validate_manifest(p)
            with tempfile.TemporaryDirectory() as cd:
                copied, prov = tr.copy_skill_to_config(p, manifest, Path(cd), ablation_id="no-wtu")
                self.assertIn("skill_hash", prov)               # provenance returned for the run record (#9)
                self.assertIn("parent_skill_hash", prov)        # canonical hash for same-revision pairing
                self.assertIn("components", prov)               # components recorded for the run record (#8)
                text = Path(copied[0]).read_text(encoding="utf-8")
                self.assertNotIn("when_to_use", text)
                self.assertIn("name: good-pr", text)

    def test_pi_trigger_baseline_matches_ablation_surface(self):
        # The baseline (no-ablation) arm must use the SAME canonical tree builder as
        # the ablation arm, so the two are file-for-file identical apart from the edit.
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            p = self.repo(root, [self.DISCO_ABL])
            manifest = sb.validate_manifest(p)
            with tempfile.TemporaryDirectory() as cb, tempfile.TemporaryDirectory() as ca:
                base_copied, base_prov = tr.copy_skill_to_config(p, manifest, Path(cb))
                abl_copied, abl_prov = tr.copy_skill_to_config(p, manifest, Path(ca), ablation_id="no-wtu")

                def rel_files(cfg):
                    sd = Path(cfg) / "skills"
                    return {q.relative_to(sd).as_posix() for q in sd.rglob("*") if q.is_file()}

                # identical mount dir names and identical relative file trees
                self.assertEqual({d.name for d in (Path(cb) / "skills").iterdir()},
                                 {d.name for d in (Path(ca) / "skills").iterdir()})
                self.assertEqual(rel_files(cb), rel_files(ca))
                # the references file survives in BOTH arms (the old ad-hoc copier path is gone)
                self.assertTrue(any(f.endswith("references/severity.md") for f in rel_files(cb)))
                # only difference is the edited frontmatter
                self.assertIn("when_to_use", Path(base_copied[0]).read_text(encoding="utf-8"))
                self.assertNotIn("when_to_use", Path(abl_copied[0]).read_text(encoding="utf-8"))
                self.assertEqual(base_prov["mode"], "baseline")
                self.assertEqual(abl_prov["mode"], "materialized")
                # both arms record the SAME canonical parent hash -> same revision, pairable
                self.assertEqual(base_prov["skill_tree_hash"], abl_prov["parent_skill_hash"])

    def test_materialized_ablation_is_blind_across_every_runner(self):
        # Invariant (not a single surface): a materialized ablation must be
        # model-indistinguishable from with_skill on EVERY channel the model
        # receives bytes through. The ablation id must leak into none of them, and
        # the harness-controlled model-visible surface must equal the with_skill
        # arm's. This is the generalization of the blinding leaks found one-per-round
        # (instruction text -> mount path -> temp dir name -> Jetty filename -> runbook).
        ID = "no-rp"

        def names_under(d):
            return [q.relative_to(d).as_posix() for q in Path(d).rglob("*")]

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            p = self.repo(root, [self.SECTION_ABL])       # answer-population, materialized
            manifest = sb.validate_manifest(p)
            repo_root = sb.repo_root_for_manifest(p)
            case = manifest["cases"][0]
            leaks = {}   # channel -> the model-visible string(s) that must not contain ID

            # 1) generic instruction
            instr_a = sb.variant_instruction("ablation:no-rp", manifest, repo_root)
            instr_w = sb.variant_instruction("with_skill", manifest, repo_root)
            self.assertEqual(instr_a, instr_w)             # blinded: identical
            leaks["variant_instruction"] = instr_a

            # 2) Pi smoke: instruction + mount names (relative) + workspace tree names
            with tempfile.TemporaryDirectory() as wa, tempfile.TemporaryDirectory() as wb:
                pi_instr_a, pi_args_a, _, paths_a, _ = smoke.materialize_runtime_workspace(manifest, repo_root, case, "ablation:no-rp", Path(wa))
                pi_instr_w, _, _, paths_w, _ = smoke.materialize_runtime_workspace(manifest, repo_root, case, "with_skill", Path(wb))
                self.assertEqual(pi_instr_a, pi_instr_w)
                self.assertEqual([Path(x).relative_to(wa).as_posix() for x in paths_a],
                                 [Path(x).relative_to(wb).as_posix() for x in paths_w])
                leaks["pi_smoke_instruction"] = pi_instr_a
                leaks["pi_smoke_workspace_tree"] = " ".join(names_under(wa))

            # 3) codex: prompt + mounted relative paths + workspace tree names
            rows = sb.prepared_task_rows(p, manifest, include_ablations=True, ablation_dir=root / "abl")
            arow = next(r for r in rows if r["variant"] == "ablation:no-rp")
            wrow = next(r for r in rows if r["variant"] == "with_skill")
            apt, wpt = sb.PreparedTask.from_row(arow), sb.PreparedTask.from_row(wrow)
            with tempfile.TemporaryDirectory() as ca, tempfile.TemporaryDirectory() as cb:
                sa, ia = sb.codex_skill_workspace(apt, Path(ca))
                sw, iw = sb.codex_skill_workspace(wpt, Path(cb))
                cpa, cpw = sb.codex_task_prompt(apt, sa, ia), sb.codex_task_prompt(wpt, sw, iw)
                self.assertEqual(cpa, cpw)
                self.assertEqual(sa, sw)                   # identical relative mounts
                leaks["codex_prompt"] = cpa
                leaks["codex_workspace_tree"] = " ".join(names_under(ca))

            # 4) Jetty: the jetty request (runbook + jetty block) + upload names
            trees = {"no-rp": sb.materialize(sb.ValidatedAblation.validate(repo_root, manifest, self.SECTION_ABL), root / "jabl")}
            payload = sb.build_jetty_payload(apt, manifest, collection="c", task_prefix=None, agent="claude-code", model="m", model_provider="anthropic", snapshot="s", ablation_trees=trees)
            leaks["jetty_request"] = json.dumps(payload["jetty_request"])
            leaks["jetty_upload_names"] = json.dumps([{"p": f.get("placeholder"), "h": f.get("remote_path_hint")} for f in payload["upload_plan"]["files"]])

            # The ablation id leaks into NONE of the model-visible channels.
            for channel, blob in leaks.items():
                self.assertNotIn(ID, blob, f"ablation id leaked via {channel}: {blob[:200]}")
            # And the harness still records the truth out of the model's sight.
            self.assertEqual(payload["harness"]["variant"], "ablation:no-rp")

    def test_every_runner_emits_the_same_minimum_provenance_schema(self):
        # Invariant: every provenance source records the same minimum schema, so the
        # report's verifier can rely on it uniformly regardless of which runner ran.
        REQUIRED = {"id", "mode", "population", "skill_hash", "parent_skill_hash", "components"}
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            p = self.repo(root, [self.SECTION_ABL])
            manifest = sb.validate_manifest(p)
            repo_root = sb.repo_root_for_manifest(p)

            # Source A: materialize_ablation (consumed by Pi smoke and Pi trigger).
            res = sb.materialize_ablation(repo_root, manifest, self.SECTION_ABL, root / "abl")
            self.assertTrue(REQUIRED.issubset(res), f"materialize missing {REQUIRED - set(res)}")

            # Source B: prepared rows (consumed by codex and Jetty). The ablation arm
            # carries the full schema; both skill-bearing arms carry skill_tree_hash.
            rows = sb.prepared_task_rows(p, manifest, include_ablations=True, ablation_dir=root / "abl2")
            arow = next(r for r in rows if r["variant"] == "ablation:no-rp")
            wrow = next(r for r in rows if r["variant"] == "with_skill")
            self.assertTrue(REQUIRED.issubset(arow["ablation"]), f"prepared row missing {REQUIRED - set(arow['ablation'])}")
            self.assertIn("skill_tree_hash", arow)
            self.assertIn("skill_tree_hash", wrow)

            # Jetty harness record carries the same ablation provenance + canonical hash.
            arm = sb.materialize(sb.ValidatedAblation.validate(repo_root, manifest, self.SECTION_ABL), root / "jabl")
            payload = sb.build_jetty_payload(sb.PreparedTask.from_row(arow), manifest, collection="c", task_prefix=None, agent="claude-code", model="m", model_provider="anthropic", snapshot="s", ablation_trees={"no-rp": arm})
            self.assertTrue(REQUIRED.issubset(payload["harness"]["ablation"]))
            self.assertIn("skill_tree_hash", payload["harness"])

            # Source C: Pi trigger adapter (discovery ablation).
            pd = self.repo(root / "disc", [self.DISCO_ABL])
            dm = sb.validate_manifest(pd)
            with tempfile.TemporaryDirectory() as cd:
                _, tprov = tr.copy_skill_to_config(pd, dm, Path(cd), ablation_id="no-wtu")
            self.assertTrue(REQUIRED.issubset(tprov), f"pi-trigger missing {REQUIRED - set(tprov)}")

    def test_recorded_provenance_is_a_provenance_value_object(self):
        # The recorded provenance IS Provenance.as_dict(), not a coincidentally-shaped
        # dict: the prepared row records exactly the materialize tree's provenance.
        import ablation_model as am
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            p = self.repo(root, [self.SECTION_ABL])
            manifest = sb.validate_manifest(p)
            repo_root = sb.repo_root_for_manifest(p)
            res = sb.materialize_ablation(repo_root, manifest, self.SECTION_ABL, root / "abl")
            expected = am.Provenance.from_dict(res).as_dict()
            arow = next(r for r in sb.prepared_task_rows(p, manifest, include_ablations=True, ablation_dir=root / "abl2") if r["variant"] == "ablation:no-rp")
            self.assertEqual(arow["ablation"], expected)                     # one schema, end to end
            self.assertEqual(set(arow["ablation"]), {"id", "mode", "population", "skill_hash", "parent_skill_hash", "components"})

    def test_prepare_skips_discovery_ablations_for_generic_runners(self):
        # Answer-population ablations are emitted for non-trigger cases; discovery
        # ablations are NOT emitted at all (the forced-load generic runners can't
        # measure autonomous triggering — that is the trigger adapter's job).
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            p = self.repo(root, [self.SECTION_ABL, self.DISCO_ABL])
            manifest = sb.validate_manifest(p)
            rows = sb.prepared_task_rows(p, manifest, include_ablations=True, ablation_dir=root / "abl")
            rp = [r for r in rows if r["variant"] == "ablation:no-rp"]
            wtu = [r for r in rows if r["variant"] == "ablation:no-wtu"]
            self.assertEqual({r["case_id"] for r in rp}, {"ans"})    # answer pop -> non-trigger only
            self.assertEqual(wtu, [])                                # discovery pop -> not emitted here
            self.assertEqual(rp[0]["ablation"]["mode"], "materialized")
            self.assertEqual(rp[0]["ablation"]["population"], "answer")
            self.assertIn("skill_hash", rp[0]["ablation"])

    def test_pi_smoke_rejects_discovery_ablation(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            p = self.repo(root, [self.DISCO_ABL])
            manifest = sb.validate_manifest(p)
            repo_root = sb.repo_root_for_manifest(p)
            with tempfile.TemporaryDirectory() as wd, self.assertRaises(RuntimeError) as cm:
                smoke.materialize_runtime_workspace(manifest, repo_root, manifest["cases"][0], "ablation:no-wtu", Path(wd))
            self.assertIn("trigger", str(cm.exception))

    def test_pi_trigger_rejects_answer_population_ablation(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            p = self.repo(root, [self.SECTION_ABL])   # answer-population (section/instructions)
            manifest = sb.validate_manifest(p)
            with tempfile.TemporaryDirectory() as cd, self.assertRaises(RuntimeError) as cm:
                tr.copy_skill_to_config(p, manifest, Path(cd), ablation_id="no-rp")
            self.assertIn("answer-population", str(cm.exception))

    def test_prepare_fails_without_ablation_dir_for_materialized(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            p = self.repo(root, [self.SECTION_ABL])
            with self.assertRaises(SystemExit):  # must materialize, never label original skill materialized
                sb.prepared_task_rows(p, sb.validate_manifest(p), include_ablations=True)

    def test_prepare_ablation_dir_points_rows_at_altered_tree(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            p = self.repo(root, [self.SECTION_ABL])
            manifest = sb.validate_manifest(p)
            rows = sb.prepared_task_rows(p, manifest, include_ablations=True, ablation_dir=root / "abl")
            rp = [r for r in rows if r["variant"] == "ablation:no-rp"][0]
            self.assertTrue(all("abl" in Path(sp).parts for sp in rp["skill_paths"]))
            self.assertNotIn("Regression-proof requirement", Path(rp["skill_paths"][0]).read_text(encoding="utf-8"))

    def test_jetty_export_uploads_materialized_tree_with_relative_paths(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            p = self.repo(root, [self.SECTION_ABL])
            manifest = sb.validate_manifest(p)
            row = [r for r in sb.prepared_task_rows(p, manifest, include_ablations=True, ablation_dir=root / "rabl") if r["variant"] == "ablation:no-rp"][0]
            trees = {"no-rp": sb.materialize(sb.ValidatedAblation.validate(sb.repo_root_for_manifest(p), manifest, self.SECTION_ABL), root / "jabl")}
            payload = sb.build_jetty_payload(sb.PreparedTask.from_row(row), manifest, collection="c", task_prefix=None, agent="claude-code", model="m", model_provider="anthropic", snapshot="s", ablation_trees=trees)
            skill_files = [f for f in payload["upload_plan"]["files"] if f["role"] == "skill"]
            hints = [f["remote_path_hint"] for f in skill_files]
            self.assertTrue(any(h.endswith("references/severity.md") for h in hints))  # structure preserved, not flattened
            skill_md = next(f for f in skill_files if f["remote_path_hint"].endswith("SKILL.md"))
            self.assertNotIn("Regression-proof requirement", Path(skill_md["local_path"]).read_text(encoding="utf-8"))
            task_json = json.loads(next(f for f in payload["upload_plan"]["files"] if f["role"] == "task")["content"])
            self.assertEqual(task_json["variant"], "with_skill")              # blinded: model sees with_skill
            self.assertNotIn("ablation", task_json)                          # no hypothesis leaked to the model
            self.assertEqual(payload["harness"]["variant"], "ablation:no-rp")           # truth in harness-only record
            self.assertEqual(payload["harness"]["ablation"]["mode"], "materialized")

    def test_jetty_blinds_ablation_id_from_model_visible_names(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            p = self.repo(root, [self.SECTION_ABL])
            manifest = sb.validate_manifest(p)
            row = [r for r in sb.prepared_task_rows(p, manifest, include_ablations=True, ablation_dir=root / "abl") if r["variant"] == "ablation:no-rp"][0]
            trees = {"no-rp": sb.materialize(sb.ValidatedAblation.validate(sb.repo_root_for_manifest(p), manifest, self.SECTION_ABL), root / "jabl")}
            payload = sb.build_jetty_payload(sb.PreparedTask.from_row(row), manifest, collection="c", task_prefix=None, agent="claude-code", model="m", model_provider="anthropic", snapshot="s", ablation_trees=trees)
            # What the model actually sees: the jetty request (runbook + jetty block,
            # incl. file_paths and template_variables) and each upload's remote path.
            model_visible = json.dumps(payload["jetty_request"]) + json.dumps(
                [{"placeholder": f.get("placeholder"), "remote_path_hint": f.get("remote_path_hint")} for f in payload["upload_plan"]["files"]])
            self.assertNotIn("no-rp", model_visible)
            self.assertNotIn("ablation", model_visible.lower())
            # the harness-only record still carries the truth
            self.assertEqual(payload["harness"]["variant"], "ablation:no-rp")
            self.assertEqual(payload["harness"]["ablation"]["mode"], "materialized")

    def test_export_jetty_command_materializes_declared_ablation(self):
        # Regression guard: export-jetty used to call prepared_task_rows WITHOUT
        # an ablation dir, so a declared-removal ablation tripped the prepare-or-fail
        # guard and the whole command died. The command must materialize once and
        # thread the trees through to both rows and upload payloads.
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            p = self.repo(root, [self.SECTION_ABL])
            out = root / "jetty.jsonl"
            args = SimpleNamespace(
                manifest=str(p), split=None, runs_per_variant=1,
                include_old_skill=False, include_ablations=True, allow_missing_prompts=False,
                jetty_collection="skill-evals", jetty_task_prefix=None,
                jetty_agent="claude-code", jetty_model="claude-sonnet-4-6",
                jetty_model_provider="anthropic", jetty_snapshot="python312-uv",
                use_trial_keys=False, out=str(out), dry_run=False,
                ablation_dir=str(root / "abl"),
            )
            rc = sb.export_jetty(args)   # must NOT raise SystemExit
            self.assertEqual(rc, 0)
            payloads = [json.loads(l) for l in out.read_text(encoding="utf-8").splitlines()]
            abl = [p for p in payloads if p["harness"]["variant"] == "ablation:no-rp"]
            withs = [p for p in payloads if p["harness"]["variant"] == "with_skill"]
            self.assertTrue(abl, "export-jetty produced no ablation payloads")
            self.assertEqual(abl[0]["harness"]["ablation"]["mode"], "materialized")
            # The mounted ablated SKILL.md really has the section removed.
            skill_files = [f for f in abl[0]["upload_plan"]["files"] if f["role"] == "skill"]
            skill_md = next(f for f in skill_files if f["remote_path_hint"].endswith("SKILL.md"))
            self.assertNotIn("Regression-proof requirement", Path(skill_md["local_path"]).read_text(encoding="utf-8"))
            # with_skill and the ablation arm expose an identical remote file surface.
            abl_hints = {f["remote_path_hint"] for f in skill_files}
            with_hints = {f["remote_path_hint"] for f in withs[0]["upload_plan"]["files"] if f["role"] == "skill"}
            self.assertEqual(abl_hints, with_hints)
            # Materialized exactly once: a single tree dir per ablation id, no dup suffixes.
            abl_dirs = sorted(d.name for d in (root / "abl").iterdir() if d.is_dir() and not d.name.startswith("_"))
            self.assertEqual(abl_dirs, ["no-rp"])


class AblationRegressionReportTests(unittest.TestCase):
    MANIFEST = {
        "skill_name": "good-pr", "skill_paths": ["skills/good-pr/SKILL.md"],
        "ablations": [{
            "id": "no-rp", "removed_component": "regression-proof", "mechanism": "section",
            "class": "instructions", "target": {"heading": "## Regression-proof requirement"},
            "expected_regressions": [{"summary": "accepts weak tests", "cases": ["c1"], "assertions": ["detect-weak"]}],
        }],
    }
    PARENT = "canonical-parent-hash"   # the canonical (pre-edit) tree hash both arms record

    def prov(self, manifest=None, **over):
        """metadata dict a runner records for the manifest's first ablation, with an
        exact component fingerprint + parent hash. Override fields via kwargs."""
        m = manifest or self.MANIFEST
        ab = m["ablations"][0]
        comps = sb.ablation_components(ab)
        p = {
            "id": ab["id"],
            "mode": "invalid_skill" if ab.get("invalid_skill") else "materialized",
            "population": sb.derived_population(comps),
            "skill_hash": "abl-hash",
            "parent_skill_hash": self.PARENT,
            "components": [{"class": sb.component_class(c), "mechanism": c.get("mechanism"),
                            "skill_root": c.get("target", {}).get("skill_root") or m["skill_paths"][0],
                            "target": c.get("target", {})} for c in comps],
        }
        p.update(over)
        return {"metadata": {"ablation": p}}

    def ws(self, parent=None):
        """with_skill run metadata recording the canonical tree hash."""
        return {"metadata": {"skill_tree_hash": parent or self.PARENT}}

    def test_expected_regression_confirmed_when_named_assertion_flips(self):
        # Feature 1: a confirmation must clear the replication significance gate, so
        # the regression is replicated 4x per arm (a single shot ties at p=1.0 and
        # downgrades to INDETERMINATE — see the next test).
        results = ([{"case_id": "c1", "variant": "with_skill", "objective_pass_rate": 1.0, "assertions": [{"name": "detect-weak", "passed": True}], "qualitative_assertions": [], **self.ws()} for _ in range(4)]
                   + [{"case_id": "c1", "variant": "ablation:no-rp", "objective_pass_rate": 0.0, "assertions": [{"name": "detect-weak", "passed": False}], "qualitative_assertions": [], **self.prov()} for _ in range(4)])
        entry = sb.build_ablation_regression_report(self.MANIFEST, results)[0]
        self.assertEqual(entry["status"], "measured")
        self.assertTrue(entry["provenance_verified"])
        reg = entry["regressions"][0]
        self.assertTrue(reg["expected_regression_confirmed"])
        self.assertEqual(reg["evidence_class"], "confirmed_causal")   # the typed verdict, via the guard
        self.assertTrue(reg["score_regressed"])
        self.assertTrue(reg["significance"]["significant_at_0_05"])   # replicated, not eyeballed
        self.assertEqual(reg["evidence"][0]["case"], "c1")
        self.assertEqual(reg["evidence"][0]["assertion"], "detect-weak")

    def test_single_shot_regression_is_indeterminate_not_confirmed(self):
        # Feature 1: one run per arm shows the drop but cannot rule out noise, so the
        # verdict is INDETERMINATE (None), never confirmed — the n=5 walkthrough lesson.
        results = [
            {"case_id": "c1", "variant": "with_skill", "objective_pass_rate": 1.0, "assertions": [{"name": "detect-weak", "passed": True}], "qualitative_assertions": [], **self.ws()},
            {"case_id": "c1", "variant": "ablation:no-rp", "objective_pass_rate": 0.0, "assertions": [{"name": "detect-weak", "passed": False}], "qualitative_assertions": [], **self.prov()},
        ]
        reg = sb.build_ablation_regression_report(self.MANIFEST, results)[0]["regressions"][0]
        self.assertIsNone(reg["expected_regression_confirmed"])
        self.assertEqual(reg["evidence_class"], "indeterminate")
        self.assertTrue(reg["confirmed_cases"])                        # the drop WAS observed
        self.assertFalse(reg["significance"]["significant_at_0_05"])   # just not significant yet
        self.assertIn("not significant", reg["note"])

    def test_multi_case_single_shot_does_not_confirm(self):
        # Soundness (audit fix): significance is per CASE, not pooled across cases.
        # Two cases, ONE run per arm each, both flip + drop. Per case each ties
        # (p=1.0) so the verdict is INDETERMINATE. Pooling across cases would have
        # given n=2 vs 2 -> p<=0.05 and wrongly CONFIRMED a stack of single shots,
        # defeating the "single-shot can never confirm" guarantee.
        manifest = {"skill_name": "s", "skill_paths": ["skills/good-pr/SKILL.md"], "ablations": [{
            "id": "no-rp", "removed_component": "rp", "mechanism": "section", "class": "instructions",
            "target": {"heading": "## X"},
            "expected_regressions": [{"summary": "x", "cases": ["c1", "c2"], "assertions": ["detect-weak"]}],
        }]}
        results = []
        for cid in ("c1", "c2"):
            results.append({"case_id": cid, "variant": "with_skill", "objective_pass_rate": 1.0, "combined_pass_rate": 1.0, "assertions": [{"name": "detect-weak", "passed": True}], "qualitative_assertions": [], **self.ws()})
            results.append({"case_id": cid, "variant": "ablation:no-rp", "objective_pass_rate": 0.0, "combined_pass_rate": 0.0, "assertions": [{"name": "detect-weak", "passed": False}], "qualitative_assertions": [], **self.prov(manifest)})
        reg = sb.build_ablation_regression_report(manifest, results)[0]["regressions"][0]
        self.assertEqual(sorted(reg["confirmed_cases"]), ["c1", "c2"])   # both drops observed
        self.assertIsNone(reg["expected_regression_confirmed"])          # but none significant per case
        self.assertEqual(reg["evidence_class"], "indeterminate")
        self.assertFalse(reg["significance"]["significant_at_0_05"])

    def test_single_confirmed_case_replicated_confirms_among_many(self):
        # The other side: significant iff at least one confirmed case clears the bar.
        # c1 replicated 4x per arm (perfect drop -> p=0.0286); c2 is noise (no drop).
        manifest = {"skill_name": "s", "skill_paths": ["skills/good-pr/SKILL.md"], "ablations": [{
            "id": "no-rp", "removed_component": "rp", "mechanism": "section", "class": "instructions",
            "target": {"heading": "## X"},
            "expected_regressions": [{"summary": "x", "cases": ["c1", "c2"], "assertions": ["detect-weak"]}],
        }]}
        results = []
        for _ in range(4):
            results.append({"case_id": "c1", "variant": "with_skill", "objective_pass_rate": 1.0, "combined_pass_rate": 1.0, "assertions": [{"name": "detect-weak", "passed": True}], "qualitative_assertions": [], **self.ws()})
            results.append({"case_id": "c1", "variant": "ablation:no-rp", "objective_pass_rate": 0.0, "combined_pass_rate": 0.0, "assertions": [{"name": "detect-weak", "passed": False}], "qualitative_assertions": [], **self.prov(manifest)})
            results.append({"case_id": "c2", "variant": "with_skill", "objective_pass_rate": 1.0, "combined_pass_rate": 1.0, "assertions": [{"name": "detect-weak", "passed": True}], "qualitative_assertions": [], **self.ws()})
            results.append({"case_id": "c2", "variant": "ablation:no-rp", "objective_pass_rate": 1.0, "combined_pass_rate": 1.0, "assertions": [{"name": "detect-weak", "passed": True}], "qualitative_assertions": [], **self.prov(manifest)})
        reg = sb.build_ablation_regression_report(manifest, results)[0]["regressions"][0]
        self.assertEqual(reg["confirmed_cases"], ["c1"])                 # only c1 dropped
        self.assertTrue(reg["expected_regression_confirmed"])
        self.assertTrue(reg["significance"]["significant_at_0_05"])
        self.assertTrue(reg["significance"]["by_case"]["c1"]["significant_at_0_05"])

    def test_score_drop_without_named_flip_is_not_confirmed(self):
        # The named assertion still passes; an unrelated assertion fails and drags
        # the aggregate down. A score drop is necessary, not sufficient.
        results = [
            {"case_id": "c1", "variant": "with_skill", "objective_pass_rate": 1.0, "assertions": [{"name": "detect-weak", "passed": True}, {"name": "other", "passed": True}], "qualitative_assertions": [], **self.ws()},
            {"case_id": "c1", "variant": "ablation:no-rp", "objective_pass_rate": 0.5, "assertions": [{"name": "detect-weak", "passed": True}, {"name": "other", "passed": False}], "qualitative_assertions": [], **self.prov()},
        ]
        reg = sb.build_ablation_regression_report(self.MANIFEST, results)[0]["regressions"][0]
        self.assertFalse(reg["expected_regression_confirmed"])
        self.assertTrue(reg["score_regressed"])

    def test_instruction_simulated_ablation_omitted_from_report(self):
        manifest = {"skill_name": "s", "skill_paths": ["x"], "ablations": [{"id": "sim", "removed_component": "x", "expected_regressions": ["y"]}]}
        self.assertEqual(sb.build_ablation_regression_report(manifest, []), [])

    def test_unmeasured_when_no_ablation_rows(self):
        results = [{"case_id": "c1", "variant": "with_skill", "objective_pass_rate": 1.0, "assertions": [{"name": "detect-weak", "passed": True}], "qualitative_assertions": [], **self.ws()}]
        entry = sb.build_ablation_regression_report(self.MANIFEST, results)[0]
        self.assertEqual(entry["status"], "unmeasured")   # absence of evidence, not confirmed:false
        self.assertNotIn("regressions", entry)

    def test_repeated_runs_use_symmetric_rates(self):
        # with_skill 1/2 pass, ablation 1/2 pass -> equal rates, no flip (not all-pass-vs-one-fail)
        results = [
            {"case_id": "c1", "variant": "with_skill", "objective_pass_rate": 0.5, "assertions": [{"name": "detect-weak", "passed": True}], "qualitative_assertions": [], **self.ws()},
            {"case_id": "c1", "variant": "with_skill", "objective_pass_rate": 0.0, "assertions": [{"name": "detect-weak", "passed": False}], "qualitative_assertions": [], **self.ws()},
            {"case_id": "c1", "variant": "ablation:no-rp", "objective_pass_rate": 0.5, "assertions": [{"name": "detect-weak", "passed": True}], "qualitative_assertions": [], **self.prov()},
            {"case_id": "c1", "variant": "ablation:no-rp", "objective_pass_rate": 0.0, "assertions": [{"name": "detect-weak", "passed": False}], "qualitative_assertions": [], **self.prov()},
        ]
        reg = sb.build_ablation_regression_report(self.MANIFEST, results)[0]["regressions"][0]
        self.assertFalse(reg["expected_regression_confirmed"])

    def test_invalid_skill_never_confirms_behavioral_regression(self):
        manifest = {"skill_name": "s", "skill_paths": ["x"], "ablations": [{"id": "no-desc", "invalid_skill": True, "removed_component": "d", "mechanism": "frontmatter_field", "class": "discovery", "target": {"field": "description"}, "expected_regressions": [{"summary": "x", "cases": ["c1"], "assertions": ["a"]}]}]}
        results = [
            {"case_id": "c1", "variant": "with_skill", "objective_pass_rate": 1.0, "assertions": [{"name": "a", "passed": True}], "qualitative_assertions": [], **self.ws()},
            {"case_id": "c1", "variant": "ablation:no-desc", "objective_pass_rate": 0.0, "assertions": [{"name": "a", "passed": False}], "qualitative_assertions": [], **self.prov(manifest)},
        ]
        entry = sb.build_ablation_regression_report(manifest, results)[0]
        self.assertTrue(entry["provenance_verified"])             # invalid_skill mode verifies as such
        reg = entry["regressions"][0]
        self.assertIsNone(reg["expected_regression_confirmed"])   # parser rejection != behavioral evidence

    def test_missing_output_ablation_row_never_confirms_regression(self):
        # The ablation arm produced NO output (the run failed/never ran). Its
        # assertions were graded against an empty string (all-fail). That must
        # not masquerade as a confirmed regression — it is absence of evidence.
        results = [
            {"case_id": "c1", "variant": "with_skill", "objective_pass_rate": 1.0, "missing_output": False, "assertions": [{"name": "detect-weak", "passed": True}], "qualitative_assertions": [], **self.ws()},
            {"case_id": "c1", "variant": "ablation:no-rp", "objective_pass_rate": 0.0, "missing_output": True, "assertions": [{"name": "detect-weak", "passed": False}], "qualitative_assertions": []},
        ]
        entry = sb.build_ablation_regression_report(self.MANIFEST, results)[0]
        self.assertEqual(entry["status"], "unmeasured")            # every ablation run was missing output
        self.assertNotIn("regressions", entry)
        self.assertEqual(entry["coverage"]["ablation"], {"runs": 1, "missing": 1, "errored": 0})
        self.assertIn("missing output", entry["note"])

    def test_partial_coverage_uses_only_graded_runs(self):
        # Two ablation runs: one missing output, one real run that still passes.
        # The missing run must be dropped; the real run shows no flip -> not confirmed.
        results = [
            {"case_id": "c1", "variant": "with_skill", "objective_pass_rate": 1.0, "missing_output": False, "assertions": [{"name": "detect-weak", "passed": True}], "qualitative_assertions": [], **self.ws()},
            {"case_id": "c1", "variant": "ablation:no-rp", "objective_pass_rate": 0.0, "missing_output": True, "assertions": [{"name": "detect-weak", "passed": False}], "qualitative_assertions": []},
            {"case_id": "c1", "variant": "ablation:no-rp", "objective_pass_rate": 1.0, "missing_output": False, "assertions": [{"name": "detect-weak", "passed": True}], "qualitative_assertions": [], **self.prov()},
        ]
        entry = sb.build_ablation_regression_report(self.MANIFEST, results)[0]
        self.assertEqual(entry["status"], "measured")
        self.assertEqual(entry["coverage"]["ablation"], {"runs": 2, "missing": 1, "errored": 0})
        reg = entry["regressions"][0]
        self.assertFalse(reg["expected_regression_confirmed"])     # the one graded run passed
        self.assertEqual(reg["measured_cases"], ["c1"])

    def test_infra_failure_ablation_row_never_confirms_regression(self):
        # The ablation arm crashed: a runner wrote a synthetic failure output (so
        # missing_output is False) and the assertions failed. That is not a
        # behavioral regression — it must be excluded from scoring/confirmation.
        results = [
            {"case_id": "c1", "variant": "with_skill", "objective_pass_rate": 1.0, "missing_output": False, "execution_valid": True, "assertions": [{"name": "detect-weak", "passed": True}], "qualitative_assertions": [], **self.ws()},
            {"case_id": "c1", "variant": "ablation:no-rp", "objective_pass_rate": 0.0, "missing_output": False, "execution_valid": False, "assertions": [{"name": "detect-weak", "passed": False}], "qualitative_assertions": [], **self.prov()},
        ]
        entry = sb.build_ablation_regression_report(self.MANIFEST, results)[0]
        self.assertEqual(entry["status"], "unmeasured")            # the only run was an infra failure
        self.assertEqual(entry["coverage"]["ablation"], {"runs": 1, "missing": 0, "errored": 1})
        self.assertNotIn("regressions", entry)

    def test_no_measured_pair_yields_none_not_false(self):
        # with_skill has a graded run but the ablation arm has only a missing one
        # for the cited case: confirmation is indeterminate (None), not refuted.
        results = [
            {"case_id": "c1", "variant": "with_skill", "objective_pass_rate": 1.0, "missing_output": False, "assertions": [{"name": "detect-weak", "passed": True}], "qualitative_assertions": [], **self.ws()},
            {"case_id": "c1", "variant": "ablation:no-rp", "objective_pass_rate": 0.0, "missing_output": True, "assertions": [{"name": "detect-weak", "passed": False}], "qualitative_assertions": []},
            # a second, graded ablation run on a DIFFERENT (uncited) case keeps the variant "measured"
            {"case_id": "c2", "variant": "ablation:no-rp", "objective_pass_rate": 1.0, "missing_output": False, "assertions": [{"name": "x", "passed": True}], "qualitative_assertions": [], **self.prov()},
        ]
        entry = sb.build_ablation_regression_report(self.MANIFEST, results)[0]
        self.assertEqual(entry["status"], "measured")
        reg = entry["regressions"][0]
        self.assertIsNone(reg["expected_regression_confirmed"])
        self.assertEqual(reg["measured_cases"], [])
        self.assertIn("insufficient coverage", reg["note"])

    def test_qualitative_regression_confirms_via_combined_score(self):
        # The objective rate is unchanged; only a judge/rubric (qualitative)
        # assertion regresses. Scoring on the COMBINED rate must let it confirm.
        manifest = {"skill_name": "s", "skill_paths": ["skills/good-pr/SKILL.md"], "ablations": [{
            "id": "no-rp", "removed_component": "rp", "mechanism": "section", "class": "instructions",
            "target": {"heading": "## X"}, "expected_regressions": [{"summary": "weaker rubric", "cases": ["c1"], "assertions": ["rubric"]}],
        }]}
        # Replicated 4x per arm so the combined-score regression clears the
        # significance gate (feature 1).
        results = ([{"case_id": "c1", "variant": "with_skill", "objective_pass_rate": 1.0, "combined_pass_rate": 1.0, "assertions": [], "qualitative_assertions": [{"name": "rubric", "passed": True}], **self.ws()} for _ in range(4)]
                   + [{"case_id": "c1", "variant": "ablation:no-rp", "objective_pass_rate": 1.0, "combined_pass_rate": 0.0, "assertions": [], "qualitative_assertions": [{"name": "rubric", "passed": False}], **self.prov(manifest)} for _ in range(4)])
        reg = sb.build_ablation_regression_report(manifest, results)[0]["regressions"][0]
        self.assertTrue(reg["expected_regression_confirmed"])
        self.assertEqual(reg["confirmed_cases"], ["c1"])

    def test_cross_case_evidence_does_not_confirm(self):
        # Flip on case cA but NO score drop on cA (a sibling assertion compensates);
        # score drop on cB but NO flip on cB. OR-ing across cases used to confirm;
        # per-case logic must not.
        manifest = {"skill_name": "s", "skill_paths": ["skills/good-pr/SKILL.md"], "ablations": [{
            "id": "no-rp", "removed_component": "rp", "mechanism": "section", "class": "instructions",
            "target": {"heading": "## X"}, "expected_regressions": [{"summary": "x", "cases": ["cA", "cB"], "assertions": ["x"]}],
        }]}
        results = [
            # cA: x flips 1->0 but combined stays 0.5 (y compensates), so no score drop on cA
            {"case_id": "cA", "variant": "with_skill", "objective_pass_rate": 0.5, "combined_pass_rate": 0.5, "assertions": [{"name": "x", "passed": True}, {"name": "y", "passed": False}], "qualitative_assertions": [], **self.ws()},
            {"case_id": "cA", "variant": "ablation:no-rp", "objective_pass_rate": 0.5, "combined_pass_rate": 0.5, "assertions": [{"name": "x", "passed": False}, {"name": "y", "passed": True}], "qualitative_assertions": [], **self.prov(manifest)},
            # cB: combined drops 1.0->0.5 but x does NOT flip
            {"case_id": "cB", "variant": "with_skill", "objective_pass_rate": 1.0, "combined_pass_rate": 1.0, "assertions": [{"name": "x", "passed": True}, {"name": "z", "passed": True}], "qualitative_assertions": [], **self.ws()},
            {"case_id": "cB", "variant": "ablation:no-rp", "objective_pass_rate": 0.5, "combined_pass_rate": 0.5, "assertions": [{"name": "x", "passed": True}, {"name": "z", "passed": False}], "qualitative_assertions": [], **self.prov(manifest)},
        ]
        reg = sb.build_ablation_regression_report(manifest, results)[0]["regressions"][0]
        self.assertTrue(reg["evidence"])                          # there IS a flip (on cA)
        self.assertTrue(reg["score_regressed"])                   # there IS a score drop (on cB)
        self.assertEqual(reg["confirmed_cases"], [])              # but no single case had both
        self.assertFalse(reg["expected_regression_confirmed"])

    def test_confirmation_blocked_when_provenance_missing(self):
        # A genuine flip + score drop, but NO run recorded ablation provenance: we
        # cannot prove a materialized tree was mounted, so we must not confirm.
        results = [
            {"case_id": "c1", "variant": "with_skill", "objective_pass_rate": 1.0, "assertions": [{"name": "detect-weak", "passed": True}], "qualitative_assertions": [], **self.ws()},
            {"case_id": "c1", "variant": "ablation:no-rp", "objective_pass_rate": 0.0, "assertions": [{"name": "detect-weak", "passed": False}], "qualitative_assertions": []},
        ]
        entry = sb.build_ablation_regression_report(self.MANIFEST, results)[0]
        self.assertFalse(entry["provenance_verified"])
        reg = entry["regressions"][0]
        self.assertIsNone(reg["expected_regression_confirmed"])    # not confirmed despite the flip
        self.assertIn("provenance unverified", reg["note"])

    def test_confirmation_blocked_when_a_measured_run_lacks_provenance(self):
        # One ablation run records provenance, a SECOND measured run does not. The
        # unprovenanced run could be driving the rate, so confirmation is blocked.
        results = [
            {"case_id": "c1", "variant": "with_skill", "objective_pass_rate": 1.0, "assertions": [{"name": "detect-weak", "passed": True}], "qualitative_assertions": [], **self.ws()},
            {"case_id": "c1", "variant": "ablation:no-rp", "objective_pass_rate": 0.0, "assertions": [{"name": "detect-weak", "passed": False}], "qualitative_assertions": [], **self.prov()},
            {"case_id": "c1", "variant": "ablation:no-rp", "objective_pass_rate": 0.0, "assertions": [{"name": "detect-weak", "passed": False}], "qualitative_assertions": []},
        ]
        entry = sb.build_ablation_regression_report(self.MANIFEST, results)[0]
        self.assertFalse(entry["provenance_verified"])
        self.assertIn("recorded no provenance", entry["provenance_note"])

    def test_confirmation_blocked_when_recorded_mode_is_instruction_simulated(self):
        # The run actually mounted the FULL skill (mode instruction_simulated), so a
        # measured drop is not evidence the materialized ablation caused it.
        results = [
            {"case_id": "c1", "variant": "with_skill", "objective_pass_rate": 1.0, "assertions": [{"name": "detect-weak", "passed": True}], "qualitative_assertions": [], **self.ws()},
            {"case_id": "c1", "variant": "ablation:no-rp", "objective_pass_rate": 0.0, "assertions": [{"name": "detect-weak", "passed": False}], "qualitative_assertions": [], **self.prov(mode="instruction_simulated")},
        ]
        entry = sb.build_ablation_regression_report(self.MANIFEST, results)[0]
        self.assertFalse(entry["provenance_verified"])
        self.assertIn("mode", entry["provenance_note"])
        self.assertIsNone(entry["regressions"][0]["expected_regression_confirmed"])

    def test_confirmation_blocked_when_recorded_provenance_is_malformed(self):
        # A runner recorded an ablation provenance missing skill_hash. The strict
        # JSON-boundary parser rejects it, but the report must DEGRADE (block the
        # confirmation with a note) rather than crash with an unhandled parse error.
        results = [
            {"case_id": "c1", "variant": "with_skill", "objective_pass_rate": 1.0, "assertions": [{"name": "detect-weak", "passed": True}], "qualitative_assertions": [], **self.ws()},
            {"case_id": "c1", "variant": "ablation:no-rp", "objective_pass_rate": 0.0, "assertions": [{"name": "detect-weak", "passed": False}], "qualitative_assertions": [], **self.prov(skill_hash=None)},
        ]
        entry = sb.build_ablation_regression_report(self.MANIFEST, results)[0]
        self.assertFalse(entry["provenance_verified"])
        self.assertIn("malformed", entry["provenance_note"])
        self.assertIsNone(entry["regressions"][0]["expected_regression_confirmed"])

    def test_confirmation_blocked_when_with_skill_revision_differs(self):
        # The with_skill arm recorded a DIFFERENT canonical hash than the ablation's
        # parent: the two arms were built from different skill revisions.
        results = [
            {"case_id": "c1", "variant": "with_skill", "objective_pass_rate": 1.0, "assertions": [{"name": "detect-weak", "passed": True}], "qualitative_assertions": [], **self.ws(parent="OTHER-REVISION")},
            {"case_id": "c1", "variant": "ablation:no-rp", "objective_pass_rate": 0.0, "assertions": [{"name": "detect-weak", "passed": False}], "qualitative_assertions": [], **self.prov()},
        ]
        entry = sb.build_ablation_regression_report(self.MANIFEST, results)[0]
        self.assertFalse(entry["provenance_verified"])
        self.assertIn("different skill revisions", entry["provenance_note"])

    def test_confirmation_blocked_when_component_target_differs(self):
        # The recorded component targets a different heading than the manifest declares.
        results = [
            {"case_id": "c1", "variant": "with_skill", "objective_pass_rate": 1.0, "assertions": [{"name": "detect-weak", "passed": True}], "qualitative_assertions": [], **self.ws()},
            {"case_id": "c1", "variant": "ablation:no-rp", "objective_pass_rate": 0.0, "assertions": [{"name": "detect-weak", "passed": False}], "qualitative_assertions": [],
             **self.prov(components=[{"class": "instructions", "mechanism": "section", "skill_root": "skills/good-pr/SKILL.md", "target": {"heading": "## A DIFFERENT SECTION"}}])},
        ]
        entry = sb.build_ablation_regression_report(self.MANIFEST, results)[0]
        self.assertFalse(entry["provenance_verified"])
        self.assertIn("components", entry["provenance_note"])

    def test_confirmation_blocked_when_runs_disagree_on_tree(self):
        # Two ablation runs report different skill_hash: they didn't mount the same
        # tree, so the paired comparison is unsound.
        results = [
            {"case_id": "c1", "variant": "with_skill", "objective_pass_rate": 1.0, "assertions": [{"name": "detect-weak", "passed": True}], "qualitative_assertions": [], **self.ws()},
            {"case_id": "c1", "variant": "ablation:no-rp", "objective_pass_rate": 0.0, "assertions": [{"name": "detect-weak", "passed": False}], "qualitative_assertions": [], **self.prov(skill_hash="AAA")},
            {"case_id": "c1", "variant": "ablation:no-rp", "objective_pass_rate": 0.0, "assertions": [{"name": "detect-weak", "passed": False}], "qualitative_assertions": [], **self.prov(skill_hash="BBB")},
        ]
        entry = sb.build_ablation_regression_report(self.MANIFEST, results)[0]
        self.assertFalse(entry["provenance_verified"])
        self.assertIn("skill_hash mismatch", entry["provenance_note"])
        self.assertIsNone(entry["regressions"][0]["expected_regression_confirmed"])


class AblationCoverageTests(unittest.TestCase):
    """Exercises paths claimed in the spec acceptance criteria but not covered by
    the other test classes: script/asset/reference-both mechanisms, multi-root
    materialization with arbitrary-file survival, and provenance completeness."""

    def multiroot_repo(self, root: Path) -> Path:
        repo = root / "repo"
        a = repo / "skills" / "good-pr"
        (a / "scripts").mkdir(parents=True)
        (a / "assets").mkdir()
        (a / "references").mkdir()
        (a / "SKILL.md").write_text(SKILL_FIXTURE, encoding="utf-8")
        (a / "references" / "severity.md").write_text("# sev\n", encoding="utf-8")
        (a / "scripts" / "run.py").write_text("print('hi')\n", encoding="utf-8")
        (a / "assets" / "tmpl.txt").write_text("template\n", encoding="utf-8")
        (a / "NOTES.md").write_text("arbitrary extra file\n", encoding="utf-8")  # outside references/scripts/assets
        b = repo / "skills" / "audit"
        b.mkdir(parents=True)
        (b / "SKILL.md").write_text("---\nname: audit\ndescription: Audit code. Use for audits.\nallowed-tools: Read\n---\n\n# Audit\n\nbody.\n", encoding="utf-8")
        (repo / "evals").mkdir()
        manifest = {
            "version": 1, "skill_name": "good-pr",
            "skill_paths": ["skills/good-pr/SKILL.md", "skills/audit/SKILL.md"],
            "variants": ["with_skill", "without_skill"],
            "cases": [{"id": "c1", "split": "tune", "prompt": "x", "assertions": [{"name": "a", "type": "contains", "value": "x"}]}],
            "ablations": [],
        }
        p = repo / "evals" / "shared-benchmark.json"
        p.write_text(json.dumps(manifest), encoding="utf-8")
        return p

    def materialize(self, root: Path, ablation: dict) -> dict:
        p = self.multiroot_repo(root)
        manifest = json.loads(p.read_text())
        manifest["ablations"] = [ablation]
        p.write_text(json.dumps(manifest), encoding="utf-8")
        manifest = sb.validate_manifest(p)
        return sb.materialize_ablation(sb.repo_root_for_manifest(p), manifest, ablation, root / "out")

    def test_script_mechanism_removes_file(self):
        with tempfile.TemporaryDirectory() as td:
            res = self.materialize(Path(td), {"id": "no-script", "removed_component": "s", "mechanism": "script", "class": "resource", "target": {"skill_root": "skills/good-pr/SKILL.md", "path": "scripts/run.py"}})
            base = Path(res["dir"]) / "skills_good-pr_SKILL.md"
            self.assertFalse((base / "scripts" / "run.py").exists())

    def test_asset_mechanism_removes_file(self):
        with tempfile.TemporaryDirectory() as td:
            res = self.materialize(Path(td), {"id": "no-asset", "removed_component": "a", "mechanism": "asset", "class": "resource", "target": {"skill_root": "skills/good-pr/SKILL.md", "path": "assets/tmpl.txt"}})
            base = Path(res["dir"]) / "skills_good-pr_SKILL.md"
            self.assertFalse((base / "assets" / "tmpl.txt").exists())

    def test_reference_both_unlinks_and_deletes(self):
        with tempfile.TemporaryDirectory() as td:
            res = self.materialize(Path(td), {"id": "no-sev", "removed_component": "r", "mechanism": "reference", "class": "resource", "target": {"skill_root": "skills/good-pr/SKILL.md", "path": "references/severity.md", "remove": "both"}})
            base = Path(res["dir"]) / "skills_good-pr_SKILL.md"
            self.assertFalse((base / "references" / "severity.md").exists())
            self.assertNotIn("](references/severity.md)", (base / "SKILL.md").read_text(encoding="utf-8"))

    def test_multi_root_preserved_separately_with_arbitrary_files(self):
        with tempfile.TemporaryDirectory() as td:
            res = self.materialize(Path(td), {"id": "two-root", "removed_component": "two", "components": [
                {"mechanism": "section", "class": "instructions", "target": {"skill_root": "skills/good-pr/SKILL.md", "heading": "## Regression-proof requirement"}},
                {"mechanism": "frontmatter_field", "class": "runtime", "target": {"skill_root": "skills/audit/SKILL.md", "field": "allowed-tools"}},
            ]})
            d = Path(res["dir"])
            pr = d / "skills_good-pr_SKILL.md"
            au = d / "skills_audit_SKILL.md"
            # both roots present and independently ablated
            self.assertNotIn("Regression-proof requirement", (pr / "SKILL.md").read_text(encoding="utf-8"))
            self.assertNotIn("allowed-tools", (au / "SKILL.md").read_text(encoding="utf-8"))
            # the audit root keeps its other content; the good-pr root keeps its checklist
            self.assertIn("name: audit", (au / "SKILL.md").read_text(encoding="utf-8"))
            self.assertIn("## Review checklist", (pr / "SKILL.md").read_text(encoding="utf-8"))
            # arbitrary file outside references/scripts/assets survived the copy
            self.assertTrue((pr / "NOTES.md").exists())
            self.assertEqual(len(res["skill_files"]), 2)

    def test_safe_under_rejects_symlink_escape(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td) / "root"
            base.mkdir()
            (Path(td) / "secret.txt").write_text("s", encoding="utf-8")
            link = base / "link"
            link.symlink_to(Path(td) / "secret.txt")
            with self.assertRaises(sb.AblationError):
                sb._safe_under(base, link)   # resolves outside base
            (base / "ok.txt").write_text("x", encoding="utf-8")
            self.assertTrue(str(sb._safe_under(base, base / "ok.txt")).endswith("ok.txt"))

    def test_preprocess_mechanism_removes_inline_command(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = root / "repo"
            sd = repo / "skills" / "good-pr"
            sd.mkdir(parents=True)
            (sd / "SKILL.md").write_text("---\nname: good-pr\ndescription: Review PRs. Use for PRs.\n---\n\n# PR\n\n## Gather\n\n!`git diff --stat`\n\nThen review.\n", encoding="utf-8")
            (repo / "evals").mkdir()
            ablation = {"id": "no-pre", "removed_component": "diff preprocess", "mechanism": "preprocess", "class": "preprocess", "target": {"skill_root": "skills/good-pr/SKILL.md", "contains": ["git diff"]}}
            manifest = {"version": 1, "skill_name": "good-pr", "skill_paths": ["skills/good-pr/SKILL.md"], "variants": ["with_skill", "without_skill"], "cases": [{"id": "c", "split": "tune", "prompt": "x", "assertions": [{"name": "a", "type": "contains", "value": "x"}]}], "ablations": [ablation]}
            p = repo / "evals" / "shared-benchmark.json"
            p.write_text(json.dumps(manifest), encoding="utf-8")
            m = sb.validate_manifest(p)
            res = sb.materialize_ablation(sb.repo_root_for_manifest(p), m, ablation, root / "out")
            text = Path(res["skill_files"]["skills/good-pr/SKILL.md"]).read_text(encoding="utf-8")
            self.assertNotIn("git diff --stat", text)
            self.assertIn("Then review.", text)
            self.assertEqual(res["population"], "answer")

    def test_provenance_includes_diff_stat_and_hash(self):
        with tempfile.TemporaryDirectory() as td:
            res = self.materialize(Path(td), {"id": "p", "removed_component": "s", "mechanism": "section", "class": "instructions", "target": {"skill_root": "skills/good-pr/SKILL.md", "heading": "## Regression-proof requirement"}})
            self.assertIn("skill_hash", res)
            self.assertTrue(all("removed_bytes" in c for c in res["components"]))


class AblationSpecCompletenessTests(unittest.TestCase):
    def repo(self, root: Path, ablations: list) -> Path:
        repo = root / "repo"
        sd = repo / "skills" / "good-pr"
        (sd / "references").mkdir(parents=True, exist_ok=True)
        (sd / "SKILL.md").write_text(SKILL_FIXTURE, encoding="utf-8")
        (sd / "references" / "severity.md").write_text("# sev\n", encoding="utf-8")
        (repo / "evals").mkdir(exist_ok=True)
        manifest = {"version": 1, "skill_name": "good-pr", "skill_paths": ["skills/good-pr/SKILL.md"], "variants": ["with_skill", "without_skill"], "cases": [{"id": "ans", "split": "tune", "prompt": "x", "assertions": [{"name": "a", "type": "contains", "value": "x"}]}], "ablations": ablations}
        p = repo / "evals" / "shared-benchmark.json"
        p.write_text(json.dumps(manifest), encoding="utf-8")
        return p

    def test_invalid_skill_mode_allows_required_field_removal(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            ab = {"id": "no-desc", "removed_component": "desc", "invalid_skill": True, "mechanism": "frontmatter_field", "class": "discovery", "target": {"field": "description"}}
            p = self.repo(root, [ab])
            res = sb.materialize_ablation(sb.repo_root_for_manifest(p), sb.validate_manifest(p), ab, root / "out")
            self.assertEqual(res["mode"], "invalid_skill")
            self.assertNotIn("description:", Path(res["skill_files"]["skills/good-pr/SKILL.md"]).read_text(encoding="utf-8"))

    def test_required_field_removal_refused_without_invalid_flag(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            ab = {"id": "no-desc", "removed_component": "desc", "mechanism": "frontmatter_field", "class": "discovery", "target": {"field": "description"}}
            p = self.repo(root, [ab])
            with self.assertRaises(sb.AblationError):
                sb.materialize_ablation(sb.repo_root_for_manifest(p), sb.validate_manifest(p), ab, root / "out")

    def test_isolation_warning_on_oversized_removal(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            ab = {"id": "big", "removed_component": "most of body", "mechanism": "section", "class": "instructions", "target": {"heading": "# Good PR review"}}
            p = self.repo(root, [ab])
            res = sb.materialize_ablation(sb.repo_root_for_manifest(p), sb.validate_manifest(p), ab, root / "out")
            self.assertTrue(res["isolation_warnings"])

    def test_check_ablations_dry_run_pass_and_fail(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            p = self.repo(root, [{"id": "good", "removed_component": "rp", "mechanism": "section", "class": "instructions", "target": {"heading": "## Regression-proof requirement"}}])
            self.assertEqual(sb.check_ablations_dry_run(p, sb.validate_manifest(p)), 0)
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            p = self.repo(root, [{"id": "bad", "removed_component": "x", "mechanism": "section", "class": "instructions", "target": {"heading": "## Nonexistent"}}])
            self.assertEqual(sb.check_ablations_dry_run(p, sb.validate_manifest(p)), 1)  # gate fires at materialize, not validate

    def test_audit_manifest_flags_ablation_hygiene(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            ablations = [
                {"id": "dangling", "removed_component": "x", "mechanism": "reference", "class": "resource", "target": {"path": "references/nope.md", "remove": "both"}},
                {"id": "badrefs", "removed_component": "x", "mechanism": "section", "class": "instructions", "target": {"heading": "## Severity"}, "expected_regressions": [{"summary": "s", "cases": ["ghost"], "assertions": ["missing"]}]},
            ]
            rep = sb.audit_manifest_report(self.repo(root, ablations))
            kinds = {f["kind"] for f in rep["findings"]}
            self.assertIn("ablation-dangling-reference", kinds)
            self.assertIn("ablation-no-expected-regression", kinds)
            self.assertIn("ablation-unknown-case", kinds)
            self.assertIn("ablation-unknown-assertion", kinds)

    def test_regression_report_tags_invalid_skill(self):
        manifest = {"skill_name": "s", "skill_paths": ["x"], "ablations": [{"id": "no-desc", "removed_component": "d", "invalid_skill": True, "mechanism": "frontmatter_field", "class": "discovery", "target": {"field": "description"}, "expected_regressions": []}]}
        self.assertTrue(sb.build_ablation_regression_report(manifest, [])[0]["invalid_skill"])


FAKE_PI = '''#!/usr/bin/env python3
import sys, json
args = sys.argv[1:]
texts = []
i = 0
while i < len(args):
    if args[i] == "--skill":
        try:
            texts.append(open(args[i + 1], encoding="utf-8").read())
        except Exception:
            pass
        i += 2
    else:
        i += 1
joined = "\\n".join(texts)
marker = "NO_SKILL" if not joined else ("HAS_RP" if "Regression-proof" in joined else "NO_RP")
print(json.dumps({"type": "agent_end", "messages": [{"role": "assistant", "content": [{"type": "text", "text": marker}], "usage": {"input": 1, "output": 1, "totalTokens": 2}, "model": "fake", "provider": "fake"}]}))
'''


class AblationLiveExecutionTests(unittest.TestCase):
    """End-to-end through the real Pi smoke execution path (subprocess ->
    event parse -> output.md) with a stubbed `pi` binary. Only the model is
    faked; the fake echoes a token derived from the skill files it is actually
    given, proving the materialized (ablated) content is what gets executed."""

    def test_pi_smoke_executes_materialized_ablated_skill(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            bindir = root / "bin"
            bindir.mkdir()
            (bindir / "pi").write_text(FAKE_PI, encoding="utf-8")
            (bindir / "pi").chmod(0o755)
            repo = root / "good-pr"
            sd = repo / "skills" / "good-pr"
            (sd / "references").mkdir(parents=True)
            (sd / "SKILL.md").write_text(SKILL_FIXTURE, encoding="utf-8")
            (sd / "references" / "severity.md").write_text("# sev\n", encoding="utf-8")
            (repo / "evals").mkdir()
            manifest = {
                "version": 1, "skill_name": "good-pr", "skill_paths": ["skills/good-pr/SKILL.md"],
                "variants": ["with_skill", "without_skill"],
                "cases": [{"id": "ans", "split": "tune", "prompt": "Review.", "assertions": [{"name": "a", "type": "contains", "value": "x"}]}],
                "ablations": [{"id": "no-rp", "removed_component": "rp", "mechanism": "section", "class": "instructions", "target": {"heading": "## Regression-proof requirement"}}],
            }
            (repo / "evals" / "shared-benchmark.json").write_text(json.dumps(manifest), encoding="utf-8")
            old_root, old_path = smoke.ROOT, os.environ["PATH"]
            results = {}
            try:
                smoke.ROOT = root
                os.environ["PATH"] = str(bindir) + os.pathsep + old_path
                for variant in ("with_skill", "without_skill", "ablation:no-rp"):
                    smoke.run_case("good-pr", manifest, manifest["cases"][0], variant, "run", 60)
                    results[variant] = (root / "good-pr" / "eval-runs" / "run" / "ans" / variant / "output.md").read_text(encoding="utf-8").strip()
            finally:
                smoke.ROOT, os.environ["PATH"] = old_root, old_path
            self.assertEqual(results["with_skill"], "HAS_RP")        # full skill executed
            self.assertEqual(results["without_skill"], "NO_SKILL")   # no skill executed
            self.assertEqual(results["ablation:no-rp"], "NO_RP")     # the ABLATED skill is what ran
            meta = json.loads((root / "good-pr" / "eval-runs" / "run" / "ans" / "ablation:no-rp" / "metadata.json").read_text(encoding="utf-8"))
            self.assertEqual(meta["ablation"]["mode"], "materialized")   # provenance persisted on the run (#9)
            # the runner emits the full minimum provenance schema + canonical hash
            self.assertTrue({"id", "mode", "population", "skill_hash", "parent_skill_hash", "components"}.issubset(meta["ablation"]))
            self.assertEqual(meta["skill_tree_hash"], meta["ablation"]["parent_skill_hash"])
            ws_meta = json.loads((root / "good-pr" / "eval-runs" / "run" / "ans" / "with_skill" / "metadata.json").read_text(encoding="utf-8"))
            self.assertEqual(ws_meta["skill_tree_hash"], meta["ablation"]["parent_skill_hash"])   # both arms, same revision


class AblationReviewFixesTests(unittest.TestCase):
    MIN_SKILL = "---\nname: good-pr\ndescription: Review PRs. Use for PRs.\n---\n\n# PR\n\n## A\n\nbody\n"

    def manifest(self, root: Path, ablations: list, skill_paths=None, second_root=False) -> Path:
        repo = root / "repo"
        sd = repo / "skills" / "good-pr"
        sd.mkdir(parents=True)
        (sd / "SKILL.md").write_text(self.MIN_SKILL, encoding="utf-8")
        if second_root:
            (repo / "skills" / "audit").mkdir(parents=True)
            (repo / "skills" / "audit" / "SKILL.md").write_text("---\nname: audit\ndescription: Audit. Use for audits.\n---\n\n# Audit\n", encoding="utf-8")
        (repo / "evals").mkdir()
        m = {"version": 1, "skill_name": "good-pr", "skill_paths": skill_paths or ["skills/good-pr/SKILL.md"], "variants": ["with_skill", "without_skill"], "cases": [{"id": "c", "split": "tune", "prompt": "x", "assertions": [{"name": "a", "type": "contains", "value": "x"}]}], "ablations": ablations}
        p = repo / "evals" / "shared-benchmark.json"
        p.write_text(json.dumps(m), encoding="utf-8")
        return p

    # --- #7 parsing ---
    def test_block_scalar_with_blank_line_removed_cleanly(self):
        text = "---\nname: s\ndescription: A skill.\nwhen_to_use: >\n  line one\n\n  line two\n---\n\n# Body\n\nkeep.\n"
        s, e = sb.frontmatter_field_span(text, "when_to_use")
        new = text[:s] + text[e:]
        self.assertNotIn("line one", new)
        self.assertNotIn("line two", new)          # whole block scalar gone, not truncated at the blank line
        self.assertIn("description: A skill.", new)
        self.assertTrue(sb.required_fields_present(new))

    def test_empty_folded_description_fails_required(self):
        self.assertFalse(sb.required_fields_present("---\nname: s\ndescription: >\n---\n\n# B\n"))
        self.assertTrue(sb.required_fields_present("---\nname: s\ndescription: Real.\n---\n\n# B\n"))

    def test_section_deletion_is_tilde_fence_aware(self):
        text = "---\nname: s\ndescription: d.\n---\n\n## Target\n\nbody\n\n~~~\n## not a heading\n~~~\n\n## Next\n\nkeep\n"
        s, e = sb.section_span(text, "## Target")
        new = text[:s] + text[e:]
        self.assertNotIn("body", new)
        self.assertNotIn("not a heading", new)     # the ~~~-fenced line is code, section runs to ## Next
        self.assertIn("## Next", new)

    def test_list_item_removes_continuation_lines(self):
        text = "---\nname: s\ndescription: d.\n---\n\n## L\n\n- First item\n  continued guidance here\n- Second item\n"
        new = sb._apply_edits(text, sb.list_item_ops(text, "## L", ["First item"]))
        self.assertNotIn("continued guidance here", new)
        self.assertIn("Second item", new)

    def test_reference_pointer_skips_code_fences(self):
        ops = sb.reference_pointer_ops("See [x](references/a.md).\n\n```\n[y](references/a.md)\n```\n", "references/a.md")
        self.assertEqual(len(ops), 1)              # only the real link, not the fenced example

    # --- #5 filesystem safety ---
    def test_copy_rejects_symlink_escaping_root(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            src = root / "skill"
            src.mkdir()
            (src / "SKILL.md").write_text(self.MIN_SKILL, encoding="utf-8")
            (root / "secret.txt").write_text("secret", encoding="utf-8")
            (src / "link").symlink_to(root / "secret.txt")
            with self.assertRaises(sb.AblationError):
                sb._copy_skill_root(src, root / "dst")

    def test_ensure_ablation_dir_refuses_nonempty_unowned(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td) / "user-data"
            d.mkdir()
            (d / "important.txt").write_text("keep", encoding="utf-8")
            with self.assertRaises(SystemExit):
                sb._ensure_ablation_dir(d)
            self.assertTrue((d / "important.txt").exists())   # never deleted

    def test_execution_valid_flags_infrastructure_failures(self):
        self.assertTrue(sb.execution_valid({"returncode": 0}, "a real answer"))
        self.assertTrue(sb.execution_valid(None, "answer with no metadata"))
        self.assertFalse(sb.execution_valid({"returncode": 1}, "x"))
        self.assertFalse(sb.execution_valid({"timed_out": True}, "x"))
        self.assertFalse(sb.execution_valid({"timeout": True}, "x"))
        self.assertFalse(sb.execution_valid({}, "[CODEX FAILURE: returncode=1]\n\n"))
        self.assertFalse(sb.execution_valid({}, "[JETTY FAILURE: trajectory failed before producing output]\n"))
        self.assertFalse(sb.execution_valid({}, "[TIMEOUT: no final assistant message captured]"))

    # --- #6 gate soundness ---
    def test_validate_rejects_mechanism_class_mismatch(self):
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(SystemExit):
                sb.validate_manifest(self.manifest(Path(td), [{"id": "x", "removed_component": "x", "mechanism": "section", "class": "discovery", "target": {"heading": "## A"}}]))

    def test_validate_rejects_resource_targeting_skill_md(self):
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(SystemExit):
                sb.validate_manifest(self.manifest(Path(td), [{"id": "x", "removed_component": "x", "mechanism": "asset", "class": "resource", "target": {"path": "SKILL.md"}}]))

    def test_two_components_deleting_same_file_refused(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            p = self.manifest(root, [])
            (p.parent.parent / "skills" / "good-pr" / "scripts").mkdir()
            (p.parent.parent / "skills" / "good-pr" / "scripts" / "x.py").write_text("print(1)\n", encoding="utf-8")
            ab = {"id": "dup-del", "removed_component": "x", "components": [
                {"mechanism": "script", "class": "resource", "target": {"path": "scripts/x.py"}},
                {"mechanism": "asset", "class": "resource", "target": {"path": "scripts/x.py"}},
            ]}
            with self.assertRaises(sb.AblationError):
                sb.materialize_ablation(sb.repo_root_for_manifest(p), sb.validate_manifest(p), ab, root / "out")

    # --- #3a all roots ---
    def test_unreferenced_root_is_still_materialized(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            ab = {"id": "a-only", "removed_component": "x", "mechanism": "section", "class": "instructions", "target": {"skill_root": "skills/good-pr/SKILL.md", "heading": "## A"}}
            p = self.manifest(root, [ab], skill_paths=["skills/good-pr/SKILL.md", "skills/audit/SKILL.md"], second_root=True)
            res = sb.materialize_ablation(sb.repo_root_for_manifest(p), sb.validate_manifest(p), ab, root / "out")
            self.assertEqual(set(res["skill_files"]), {"skills/good-pr/SKILL.md", "skills/audit/SKILL.md"})  # B not dropped
            self.assertTrue(Path(res["skill_files"]["skills/audit/SKILL.md"]).exists())

    # --- #2 codex isolation ---
    def test_codex_workspace_isolates_skill_by_variant(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            skill = root / "skills" / "good-pr"
            skill.mkdir(parents=True)
            (skill / "SKILL.md").write_text(self.MIN_SKILL, encoding="utf-8")
            base = {"skill_paths": [str(skill / "SKILL.md")], "input_files": [], "prompt": "x"}
            with tempfile.TemporaryDirectory() as w1:
                ws = Path(w1)
                sk, _ = sb.codex_skill_workspace(sb.PreparedTask.from_row({**base, "variant": "without_skill"}), ws)
                self.assertEqual(sk, [])                       # without_skill mounts no skill
                self.assertFalse((ws / "skills").exists())
                self.assertIn("Do not use any skill", sb.codex_task_prompt(sb.PreparedTask.from_row({"variant": "without_skill", "prompt": "x"}), sk, []))
            with tempfile.TemporaryDirectory() as w2:
                ws = Path(w2)
                sk, _ = sb.codex_skill_workspace(sb.PreparedTask.from_row({**base, "variant": "with_skill"}), ws)
                self.assertTrue(sk and (ws / sk[0]).exists())  # skill mounted inside the isolated workspace
                self.assertTrue(sk[0].startswith("skills/"))   # workspace-relative, not the original repo path

    def test_codex_prompt_branches_on_ablation_mode(self):
        skills = ["skills/root-0/SKILL.md"]
        # instruction_simulated: the on-disk skill is the FULL skill, so the prompt
        # MUST carry the directive to drop the component — otherwise it's just with_skill.
        sim = {
            "variant": "ablation:no-rp", "prompt": "Review.",
            "ablation": {"id": "no-rp", "mode": "instruction_simulated", "population": "answer", "removed_component": "regression-proof"},
            "instruction": "Use the good-pr skill, but simulate this ablation: remove/ignore regression-proof. Expected regression to watch for: accepts weak tests.",
        }
        sim_prompt = sb.codex_task_prompt(sb.PreparedTask.from_row(sim), skills, [])
        self.assertIn("simulate this ablation", sim_prompt)
        self.assertIn("regression-proof", sim_prompt)
        # materialized: the on-disk skill is already altered -> blind, no hypothesis text.
        mat = {
            "variant": "ablation:no-rp", "prompt": "Review.",
            "ablation": {"id": "no-rp", "mode": "materialized", "population": "answer",
                         "skill_hash": "E", "parent_skill_hash": "C",
                         "components": [{"class": "instructions", "mechanism": "section", "skill_root": "skills/root-0/SKILL.md", "target": {"heading": "## H"}}]},
            "instruction": "Use the skill under test (good-pr). Its files are provided in your workspace ...",
        }
        mat_prompt = sb.codex_task_prompt(sb.PreparedTask.from_row(mat), skills, [])
        with_prompt = sb.codex_task_prompt(sb.PreparedTask.from_row({"variant": "with_skill", "prompt": "Review.", "instruction": "x"}), skills, [])
        self.assertNotIn("simulate", mat_prompt)
        self.assertNotIn("ignore/remove", mat_prompt)
        self.assertNotIn("regression-proof", mat_prompt)
        # blinded materialized prompt is identical to the with_skill prompt
        self.assertEqual(mat_prompt, with_prompt)

    # --- #3b Jetty with_skill surface parity ---
    def test_jetty_with_skill_uploads_tree_recursively(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = root / "repo"
            sd = repo / "skills" / "good-pr"
            (sd / "references").mkdir(parents=True)
            (sd / "SKILL.md").write_text("---\nname: good-pr\ndescription: d.\n---\n\n# B\n\nSee [g](references/g.md).\n", encoding="utf-8")
            (sd / "references" / "g.md").write_text("guide\n", encoding="utf-8")
            (repo / "evals").mkdir()
            m = {"version": 1, "skill_name": "good-pr", "skill_paths": ["skills/good-pr/SKILL.md"], "variants": ["with_skill", "without_skill"], "cases": [{"id": "c", "split": "tune", "prompt": "x", "assertions": [{"name": "a", "type": "contains", "value": "x"}]}], "ablations": []}
            p = repo / "evals" / "shared-benchmark.json"
            p.write_text(json.dumps(m), encoding="utf-8")
            manifest = sb.validate_manifest(p)
            row = [r for r in sb.prepared_task_rows(p, manifest) if r["variant"] == "with_skill"][0]
            tree_dir = sb.build_canonical_skill_tree(sb.repo_root_for_manifest(p), manifest, root / "wst")
            payload = sb.build_jetty_payload(sb.PreparedTask.from_row(row), manifest, collection="c", task_prefix=None, agent="claude-code", model="m", model_provider="anthropic", snapshot="s", with_skill_tree_dir=tree_dir)
            hints = [f["remote_path_hint"] for f in payload["upload_plan"]["files"] if f["role"] == "skill"]
            self.assertTrue(any(h.endswith("references/g.md") for h in hints))  # recursive, matching the ablation arm


def _is_subsequence(small: bytes, big: bytes) -> bool:
    """True if `small` can be obtained from `big` by deleting bytes only (no
    additions/substitutions) — i.e. the change is a pure deletion."""
    it = iter(big)
    return all(ch in it for ch in small)


def _tree_files(d: Path) -> dict[str, bytes]:
    return {p.relative_to(d).as_posix(): p.read_bytes() for p in sorted(d.rglob("*")) if p.is_file()}


class AblationDifferentialInvariantTests(unittest.TestCase):
    """Differential testing (testing-best-practices/references/differential-testing.md):
    the with_skill canonical tree is the trusted oracle; an ablation must equal it
    MINUS EXACTLY the declared edit and nothing else. This is the core
    experimental-contract invariant the earlier tests failed to assert."""

    SKILL = (
        "---\nname: good-pr\ndescription: Review pull requests for correctness and tests. Use when reviewing a PR.\n"
        "when_to_use: When asked to review a PR, diff, or patch.\nallowed-tools: Read, Grep\n---\n\n"
        "# Good PR review\n\n## Gather context\n\n!`git diff --stat`\n\n## Review checklist\n\n"
        "<!-- ablation:no-scope:start -->\n- Scope: flag unrelated changes and ask to split them.\n<!-- ablation:no-scope:end -->\n"
        "- Tests: confirm the tests would fail on the pre-change code.\n\n"
        "## Regression-proof requirement\n\nRequire a test that fails without the fix and passes with it.\n\n"
        "## Severity\n\nPick a verdict. See [the severity guide](references/severity.md).\n"
    )

    def repo(self, root: Path) -> Path:
        sd = root / "repo" / "skills" / "good-pr"
        (sd / "references").mkdir(parents=True)
        (sd / "scripts").mkdir()
        (sd / "assets").mkdir()
        (sd / "SKILL.md").write_text(self.SKILL, encoding="utf-8")
        (sd / "references" / "severity.md").write_text("# Severity\n\nBlocking, Minor, Clean.\n", encoding="utf-8")
        (sd / "scripts" / "run.py").write_text("print('hi')\n", encoding="utf-8")
        (sd / "assets" / "tmpl.txt").write_text("template\n", encoding="utf-8")
        (sd / "NOTES.md").write_text("arbitrary extra file\n", encoding="utf-8")  # not in references/scripts/assets
        (root / "repo" / "evals").mkdir()
        m = {"version": 1, "skill_name": "good-pr", "skill_paths": ["skills/good-pr/SKILL.md"], "variants": ["with_skill", "without_skill"], "cases": [{"id": "c", "split": "tune", "prompt": "x", "assertions": [{"name": "a", "type": "contains", "value": "x"}]}], "ablations": []}
        p = root / "repo" / "evals" / "shared-benchmark.json"
        p.write_text(json.dumps(m), encoding="utf-8")
        return p

    def pair(self, root: Path, p: Path, ablation: dict, tag: str):
        manifest = sb.validate_manifest(p)
        repo_root = sb.repo_root_for_manifest(p)
        with_dir = sb.build_canonical_skill_tree(repo_root, manifest, root / f"with-{tag}")
        res = sb.materialize_ablation(repo_root, manifest, ablation, root / f"abl-{tag}")
        abl_dir = Path(res["dir"])
        skill_key = Path(res["skill_files"]["skills/good-pr/SKILL.md"]).relative_to(abl_dir).as_posix()
        return _tree_files(with_dir), _tree_files(abl_dir), skill_key

    def assertDiffersOnlyBy(self, with_files, abl_files, *, edited=None, deleted=()):
        self.assertEqual(set(abl_files) - set(with_files), set(), "ablation ADDED files absent from with_skill")
        self.assertEqual(set(with_files) - set(abl_files), set(deleted), "removed-file set != declared deletions")
        changed = {k for k in set(with_files) & set(abl_files) if with_files[k] != abl_files[k]}
        self.assertEqual(changed, ({edited} if edited else set()), "content changed in unexpected file(s)")
        if edited:
            w, a = with_files[edited], abl_files[edited]
            self.assertLess(len(a), len(w), "edit did not shrink the file")
            self.assertTrue(_is_subsequence(a, w), "edit ADDED/substituted bytes — not a pure deletion")

    def test_edit_mechanisms_are_pure_deletions_vs_with_skill(self):
        edit_cases = {
            "section": {"mechanism": "section", "class": "instructions", "target": {"heading": "## Regression-proof requirement"}},
            "list_item": {"mechanism": "list_item", "class": "instructions", "target": {"section": "## Review checklist", "contains": ["pre-change code"]}},
            "frontmatter_field": {"mechanism": "frontmatter_field", "class": "runtime", "target": {"field": "allowed-tools"}},
            "preprocess": {"mechanism": "preprocess", "class": "preprocess", "target": {"contains": ["git diff"]}},
            "reference_pointer": {"mechanism": "reference", "class": "resource", "target": {"path": "references/severity.md", "remove": "pointer"}},
        }
        for name, target in edit_cases.items():
            with self.subTest(mechanism=name), tempfile.TemporaryDirectory() as td:
                root = Path(td)
                p = self.repo(root)
                ablation = {"id": f"e-{name}", "removed_component": name, **target}
                with_files, abl_files, skill_key = self.pair(root, p, ablation, name)
                self.assertDiffersOnlyBy(with_files, abl_files, edited=skill_key)

    def test_file_deletion_mechanisms_remove_only_target(self):
        delete_cases = {
            "reference_content": ({"mechanism": "reference", "class": "resource", "target": {"path": "references/severity.md", "remove": "content"}}, "references/severity.md"),
            "script": ({"mechanism": "script", "class": "resource", "target": {"path": "scripts/run.py"}}, "scripts/run.py"),
            "asset": ({"mechanism": "asset", "class": "resource", "target": {"path": "assets/tmpl.txt"}}, "assets/tmpl.txt"),
        }
        for name, (target, rel) in delete_cases.items():
            with self.subTest(mechanism=name), tempfile.TemporaryDirectory() as td:
                root = Path(td)
                p = self.repo(root)
                with_files, abl_files, skill_key = self.pair(root, p, {"id": f"d-{name}", "removed_component": name, **target}, name)
                removed = set(with_files) - set(abl_files)
                self.assertEqual({r.split("/", 1)[1] for r in removed}, {rel}, "removed exactly the target file")
                self.assertEqual(set(abl_files) - set(with_files), set(), "added nothing")
                self.assertEqual(with_files[skill_key], abl_files[skill_key], "SKILL.md must be untouched by a content deletion")

    def test_removed_span_is_exact_not_just_subsequence(self):
        # Subsequence is necessary but NOT sufficient: removing the WRONG region is
        # still a subsequence of the original. Pin the EXACT removed bytes with an
        # independent slice of the known fixture (the oracle), so a parser that
        # deleted a different span would be caught.
        start_marker, end_marker = "<!-- ablation:no-scope:start -->\n", "<!-- ablation:no-scope:end -->"
        cases = {
            "section": (
                {"mechanism": "section", "class": "instructions", "target": {"heading": "## Regression-proof requirement"}},
                self.SKILL[self.SKILL.index("## Regression-proof"):self.SKILL.index("## Severity")],
            ),
            "frontmatter_field": (
                {"mechanism": "frontmatter_field", "class": "runtime", "target": {"field": "allowed-tools"}},
                "allowed-tools: Read, Grep\n",
            ),
        }
        for name, (target, expected_removed) in cases.items():
            with self.subTest(mechanism=name), tempfile.TemporaryDirectory() as td:
                root = Path(td)
                p = self.repo(root)
                with_files, abl_files, skill_key = self.pair(root, p, {"id": f"x-{name}", "removed_component": name, **target}, name)
                w = with_files[skill_key].decode("utf-8")
                a = abl_files[skill_key].decode("utf-8")
                self.assertIn(expected_removed, w)                        # the oracle slice is real
                self.assertEqual(a, w.replace(expected_removed, "", 1))   # EXACTLY that span removed, once

    def test_parent_hash_equals_canonical_with_skill_hash(self):
        # The pairing invariant: a materialized ablation's recorded parent_skill_hash
        # equals the independently computed canonical with_skill tree hash, so the
        # report can prove both arms came from the same skill revision.
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            p = self.repo(root)
            manifest = sb.validate_manifest(p)
            repo_root = sb.repo_root_for_manifest(p)
            ablation = {"id": "x", "removed_component": "rp", "mechanism": "section", "class": "instructions", "target": {"heading": "## Regression-proof requirement"}}
            res = sb.materialize_ablation(repo_root, manifest, ablation, root / "abl")
            self.assertEqual(res["parent_skill_hash"], sb.canonical_skill_tree_hash(repo_root, manifest))
            self.assertNotEqual(res["skill_hash"], res["parent_skill_hash"])   # the edit changed the tree

    # --- regression-proof: prove the invariant has teeth against the ORIGINAL bug classes ---
    def test_invariant_catches_added_file(self):
        with self.assertRaises(AssertionError):  # the "different file surface" bug
            self.assertDiffersOnlyBy({"k/SKILL.md": b"x"}, {"k/SKILL.md": b"x", "k/EXTRA.md": b"y"})

    def test_invariant_catches_dropped_root(self):
        with self.assertRaises(AssertionError):  # the "copies only referenced roots" bug — root B vanishes
            self.assertDiffersOnlyBy({"a/SKILL.md": b"x", "b/SKILL.md": b"y"}, {"a/SKILL.md": b"x"})

    def test_invariant_catches_added_bytes(self):
        with self.assertRaises(AssertionError):  # a substitution leaking through "removal-only"
            self.assertDiffersOnlyBy({"k/SKILL.md": b"hello world"}, {"k/SKILL.md": b"hello brave world"}, edited="k/SKILL.md")

    # --- property: blinding invariant (model-visible instruction is identical across arms) ---
    def test_materialized_instruction_is_identical_to_with_skill(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            p = self.repo(root)
            manifest = sb.validate_manifest(p)
            repo_root = sb.repo_root_for_manifest(p)
            manifest = {**manifest, "ablations": [{"id": "x", "removed_component": "x", "mechanism": "section", "class": "instructions", "target": {"heading": "## Severity"}}]}
            self.assertEqual(
                sb.variant_instruction("ablation:x", manifest, repo_root),
                sb.variant_instruction("with_skill", manifest, repo_root),
            )

    def test_variant_instruction_is_path_neutral(self):
        # The instruction must never embed an absolute repo path: a repo-aware
        # runner would otherwise read the ORIGINAL skill and silently defeat a
        # materialized ablation. Each runner mounts the (possibly altered) files.
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            p = self.repo(root)
            manifest = sb.validate_manifest(p)
            repo_root = sb.repo_root_for_manifest(p)
            for variant in ("with_skill", "without_skill", "old_skill"):
                instr = sb.variant_instruction(variant, manifest, repo_root)
                self.assertNotIn(str(repo_root), instr)
                self.assertNotIn("/SKILL.md", instr)   # no embedded filesystem path


class AblationParserExactnessTests(unittest.TestCase):
    """Markdown/byte-level exactness of the removal parsers: fence length, heading
    level, inline-code safety, and CRLF preservation."""

    def test_fenced_mask_tracks_fence_length(self):
        # A 4-backtick fence is NOT closed by a 3-backtick line; the heading inside
        # stays masked (code), and only the matching 4-backtick line closes it.
        lines = "````\n```\n## Inside\n````\n## Real\n".splitlines(keepends=True)
        self.assertEqual(sb._fenced_mask(lines), [True, True, True, True, False])

    def test_fenced_mask_closer_needs_clean_line(self):
        # An info-string after the fence chars cannot close a block (it would be a
        # nested opener); only a fence followed by whitespace closes.
        lines = "```\ncode\n```python\nstill code\n```\nout\n".splitlines(keepends=True)
        self.assertEqual(sb._fenced_mask(lines), [True, True, True, True, True, False])

    def test_section_span_requires_declared_heading_level(self):
        # '### Foo' appears BEFORE '## Foo'; a '## Foo' target must skip the level-3
        # one and remove the level-2 section.
        text = "# T\n\n### Foo\n\nsub-body\n\n## Foo\n\nmain-body\n"
        s, e = sb.section_span(text, "## Foo")
        removed = text[s:e]
        self.assertTrue(removed.startswith("## Foo"))
        self.assertIn("main-body", removed)
        self.assertNotIn("sub-body", removed)

    def test_section_span_bare_text_target_matches_any_level(self):
        text = "# T\n\n### Foo\n\nsub-body\n\n## Foo\n\nmain-body\n"
        s, e = sb.section_span(text, "Foo")   # no '#': first 'Foo' at any level
        self.assertTrue(text[s:e].startswith("### Foo"))

    def test_reference_pointer_ignores_inline_code_sample(self):
        # The real link is unlinked; a link literal shown inside inline code is not.
        text = "See [guide](references/g.md) now.\n\nSyntax: `[guide](references/g.md)` shows a link.\n"
        ops = sb.reference_pointer_ops(text, "references/g.md")
        self.assertEqual(len(ops), 1)
        s, e, rep = ops[0]
        self.assertEqual(rep, "guide")
        self.assertLess(s, text.index("`"))    # the matched link precedes the inline-code span

    def test_list_item_continuation_includes_indented_code_block(self):
        # The targeted item contains an indented fenced code block; removing the
        # item must take the whole block, not stop at the fence.
        text = (
            "# T\n\n## Steps\n\n"
            "- Target item with code:\n  ```\n  do-it --now\n  ```\n  more of the item.\n"
            "- Keep this item.\n"
        )
        ops = sb.list_item_ops(text, "## Steps", ["Target item"])
        self.assertEqual(len(ops), 1)
        s, e, _ = ops[0]
        removed = text[s:e]
        self.assertIn("do-it --now", removed)       # the fenced block went with the item
        self.assertIn("more of the item.", removed)  # and the trailing continuation line
        self.assertNotIn("Keep this item", removed)

    def test_list_item_honors_heading_level(self):
        # '### Steps' (level 3) before '## Steps' (level 2): a '## Steps' target must
        # operate on the level-2 section.
        text = "# T\n\n### Steps\n\n- decoy item\n\n## Steps\n\n- real item\n"
        ops = sb.list_item_ops(text, "## Steps", ["item"])
        self.assertEqual(len(ops), 1)
        s, e, _ = ops[0]
        self.assertIn("real item", text[s:e])
        self.assertNotIn("decoy", text[s:e])

    def test_preprocess_skips_inline_code_example(self):
        # A real preprocess command is removed; one shown inside inline code is not.
        text = "Run !`deploy --prod` now.\n\nExample shown as code: `!`deploy --prod`` stays.\n"
        ops = sb.preprocess_ops(text, ["deploy"])
        self.assertEqual(len(ops), 1)
        s, e, _ = ops[0]
        self.assertLess(s, text.index("Example"))   # only the first (real) command line

    def test_every_text_mechanism_ignores_code_and_layer_decoys(self):
        # The RULE behind the R3/R4 parser findings, applied to EVERY text-searching
        # mechanism (including list_item and frontmatter_field, which no reviewer
        # named): a removal must ignore a lookalike target placed inside a code
        # region or the wrong structural layer, and remove only the live one. A
        # regression that weakened any one mechanism's code-awareness fails here.
        def removed_text(fn, text):
            r = fn(text)
            if isinstance(r, tuple):                          # a single (start, end) span
                return text[r[0]:r[1]]
            return "".join(text[s:e] for s, e, _ in r)        # a list of ops

        cases = [
            ("section",
             "# T\n\n```\n## Target\nDECOY\n```\n\n## Target\n\nLIVE body.\n\n## Next\n",
             lambda t: sb.section_span(t, "## Target")),
            ("list_item",
             "# T\n\n## S\n\n```\n- hit DECOY\n```\n- hit LIVE\n- keep\n",
             lambda t: sb.list_item_ops(t, "## S", ["hit"])),
            ("reference",   # removal keeps the visible text, so put the markers inside it
             "See [LIVE](refs/g.md) now.\n\nCode: `[DECOY](refs/g.md)` sample.\n",
             lambda t: sb.reference_pointer_ops(t, "refs/g.md")),
            ("preprocess",
             "Run !`go LIVE` now.\n\nCode: `!`go DECOY`` sample.\n",
             lambda t: sb.preprocess_ops(t, ["go"])),
            ("frontmatter_field",
             "---\nname: s\ndescription: d.\nwhen_to_use: LIVE.\n---\n\n# B\n\nwhen_to_use: DECOY body.\n",
             lambda t: sb.frontmatter_field_span(t, "when_to_use")),
        ]
        for name, text, fn in cases:
            with self.subTest(mechanism=name):
                out = removed_text(fn, text)
                self.assertIn("LIVE", out, f"{name}: removed the wrong region")
                self.assertNotIn("DECOY", out, f"{name}: removed a code/layer decoy")

    def test_crlf_is_preserved_through_materialization(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            sd = root / "repo" / "skills" / "good-pr"
            sd.mkdir(parents=True)
            (sd / "SKILL.md").write_bytes(SKILL_FIXTURE.replace("\n", "\r\n").encode("utf-8"))
            (root / "repo" / "evals").mkdir()
            m = {"version": 1, "skill_name": "good-pr", "skill_paths": ["skills/good-pr/SKILL.md"], "variants": ["with_skill", "without_skill"], "cases": [{"id": "c", "split": "tune", "prompt": "x", "assertions": [{"name": "a", "type": "contains", "value": "x"}]}], "ablations": []}
            p = root / "repo" / "evals" / "shared-benchmark.json"
            p.write_text(json.dumps(m), encoding="utf-8")
            manifest = sb.validate_manifest(p)
            ab = {"id": "x", "removed_component": "rp", "mechanism": "section", "class": "instructions", "target": {"heading": "## Regression-proof requirement"}}
            res = sb.materialize_ablation(sb.repo_root_for_manifest(p), manifest, ab, root / "out")
            raw = Path(res["skill_files"]["skills/good-pr/SKILL.md"]).read_bytes()
            self.assertIn(b"\r\n", raw)                              # CRLF preserved
            self.assertEqual(raw.count(b"\n"), raw.count(b"\r\n"))   # every LF is part of a CRLF; none introduced
            self.assertNotIn(b"Regression-proof requirement", raw)   # the edit still applied
            self.assertIn(b"## Severity", raw)


CORPUS_DIR = ROOT / "tests" / "corpus"


class SkillCorpusConformanceTests(unittest.TestCase):
    """Conformance/fuzz testing over a vendored corpus of REAL skills (not
    synthetic fixtures), so the parser and materializer face real-world YAML and
    Markdown — block-scalar descriptions, varied fences/headings/lists."""

    corpus = sorted(CORPUS_DIR.glob("*.SKILL.md")) if CORPUS_DIR.exists() else []

    @staticmethod
    def _has_block_scalar_desc(text: str) -> bool:
        return any(line.startswith("description:") and line.split(":", 1)[1].strip() in (">", "|", ">-", "|-", ">+", "|+") for line in text.splitlines())

    def test_corpus_is_present(self):
        self.assertGreaterEqual(len(self.corpus), 5, "expected a vendored real-skill corpus under tests/corpus/")

    def test_real_skills_parse_without_error_and_keep_required_fields(self):
        for f in self.corpus:
            with self.subTest(skill=f.name):
                text = f.read_text(encoding="utf-8")
                fm = sb.parse_frontmatter(text)
                self.assertIsInstance(fm, dict)
                self.assertTrue(isinstance(fm.get("name"), str) and fm["name"].strip(), "real skill must parse a name")
                self.assertTrue(sb.required_fields_present(text), f"{f.name}: required fields not detected")
                sb._fenced_mask(text.split("\n"))   # must not raise on real markdown

    def test_block_scalar_descriptions_parse_as_real_text(self):
        # the real-world YAML the old regex parser mangled: folded `description: >`
        block = [f for f in self.corpus if self._has_block_scalar_desc(f.read_text(encoding="utf-8"))]
        self.assertTrue(block, "corpus should include real skills with block-scalar descriptions")
        for f in block:
            with self.subTest(skill=f.name):
                desc = sb.parse_frontmatter(f.read_text(encoding="utf-8")).get("description")
                self.assertIsInstance(desc, str)
                self.assertGreater(len(desc.split()), 5, "folded description must parse to real text, not '>' or ''")

    def test_differential_invariant_holds_on_a_real_skill(self):
        src = CORPUS_DIR / "good-pr.SKILL.md"
        if not src.exists():
            self.skipTest("good-pr corpus missing")
        text = src.read_text(encoding="utf-8")
        lines = text.split("\n")
        mask = sb._fenced_mask(lines)
        heading = next((ln for i, ln in enumerate(lines) if not mask[i] and ln.startswith("## ")), None)
        self.assertIsNotNone(heading, "real skill should have a level-2 heading to ablate")
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            sd = root / "repo" / "skills" / "good-pr"
            sd.mkdir(parents=True)
            (sd / "SKILL.md").write_text(text, encoding="utf-8")
            (root / "repo" / "evals").mkdir()
            m = {"version": 1, "skill_name": "good-pr", "skill_paths": ["skills/good-pr/SKILL.md"], "variants": ["with_skill", "without_skill"], "cases": [{"id": "c", "split": "tune", "prompt": "x", "assertions": [{"name": "a", "type": "contains", "value": "x"}]}], "ablations": []}
            p = root / "repo" / "evals" / "shared-benchmark.json"
            p.write_text(json.dumps(m), encoding="utf-8")
            manifest = sb.validate_manifest(p)
            repo_root = sb.repo_root_for_manifest(p)
            with_dir = sb.build_canonical_skill_tree(repo_root, manifest, root / "w")
            res = sb.materialize_ablation(repo_root, manifest, {"id": "real-sec", "removed_component": "a real section", "mechanism": "section", "class": "instructions", "target": {"heading": heading.strip()}}, root / "a")
            wf, af = _tree_files(with_dir), _tree_files(Path(res["dir"]))
            skill_key = Path(res["skill_files"]["skills/good-pr/SKILL.md"]).relative_to(Path(res["dir"])).as_posix()
            self.assertEqual(set(af) - set(wf), set(), "added files")
            self.assertEqual(set(wf) - set(af), set(), "removed files")
            changed = {k for k in set(wf) & set(af) if wf[k] != af[k]}
            self.assertEqual(changed, {skill_key}, "changed an unexpected file")
            self.assertTrue(_is_subsequence(af[skill_key], wf[skill_key]), "real-skill section removal must be a pure deletion")
            self.assertLess(len(af[skill_key]), len(wf[skill_key]))


if __name__ == "__main__":
    unittest.main()
