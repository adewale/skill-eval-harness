import contextlib
import importlib.util
import io
import json
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
                instruction, skill_args, inputs, skill_paths = smoke.materialize_runtime_workspace(data, repo, data["cases"][0], "without_skill", Path(wd))
                self.assertEqual(skill_args, ["--no-skills"])
                self.assertEqual(skill_paths, [])
                self.assertEqual(len(inputs), 1)
                self.assertTrue(str(inputs[0]).startswith(str(Path(wd).resolve())))
                self.assertFalse((Path(wd) / "skills").exists())
                self.assertIn("not present", instruction)
            with tempfile.TemporaryDirectory() as wd:
                _, skill_args, _, skill_paths = smoke.materialize_runtime_workspace(data, repo, data["cases"][0], "with_skill", Path(wd))
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

    def test_anchor_removal(self):
        with tempfile.TemporaryDirectory() as td:
            res = self.materialize_one(Path(td), {"id": "no-scope", "removed_component": "scope check", "mechanism": "anchor", "target": {"anchor": "no-scope"}})
            text = self.skill_text(res)
            self.assertNotIn("flag unrelated changes", text)
            self.assertNotIn("ablation:no-scope", text)
            self.assertIn("Naming: match", text)

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


if __name__ == "__main__":
    unittest.main()
