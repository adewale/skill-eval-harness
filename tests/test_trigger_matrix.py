"""The trigger matrix (run_trigger_matrix.py) measured offline and live.

Offline: the stub adapter runs the whole pipeline in CI with no model — the
demo skill's should-fire query triggers, the should-not-fire query doesn't,
and weakening the mounted description measurably under-triggers (the tuning
loop's core signal, reproduced deterministically). Claude-specific detection
and the observation-window rule are covered with canned event streams; Codex is
covered through its adapter contract and shared path-evidence detector.

Live (manual): RUN_AGENT_INVOKE_SMOKE=1 runs one cheap invocation for every
supported live trigger adapter/model to verify auth/network/process plumbing.
RUN_TRIGGER_SMOKE=1 runs the fuller Claude trigger matrix across haiku, sonnet,
and opus; RUN_CODEX_TRIGGER_SMOKE=1, RUN_PI_TRIGGER_SMOKE=1, and
RUN_VIBE_TRIGGER_SMOKE=1 run the same trigger path for those adapters:

    RUN_AGENT_INVOKE_SMOKE=1 python3 -m unittest tests.test_trigger_matrix.AgentInvokeSmokeTests -v
    RUN_TRIGGER_SMOKE=1 python3 -m unittest tests.test_trigger_matrix -v

Live smokes need the relevant CLI and API credentials, and spend real tokens.
The cheap agent smoke asserts invocation only; the trigger-matrix smokes assert
observed trigger-eval runs and at least one autonomous load.
"""
import json
import os
import sys
import tempfile
import unittest
from types import SimpleNamespace
from pathlib import Path
from unittest import mock

from agent_capabilities import AGENT_CAPABILITIES
import run_pi_trigger_eval as tr
import run_trigger_matrix as tm

ROOT = Path(__file__).resolve().parents[1]
DEMO_MANIFEST = ROOT / "examples" / "demo-skill" / "evals" / "shared-benchmark.json"


def demo_trigger_rows():
    manifest = tm.load_manifest(DEMO_MANIFEST)
    return tm.cases_from_manifest(manifest, "tune")


class TriggerRowBoundaryTests(unittest.TestCase):
    def test_eval_set_requires_real_boolean_should_trigger(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "rows.json"
            path.write_text(json.dumps([{"query": "review this", "should_trigger": "false"}]), encoding="utf-8")
            args = SimpleNamespace(eval_set=str(path), split="tune")
            with self.assertRaises(SystemExit) as ctx:
                tm.eval_rows_from_args(args, DEMO_MANIFEST)
        self.assertIn("should_trigger must be true or false", str(ctx.exception))

    def test_eval_set_preserves_false_boolean(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "rows.json"
            path.write_text(json.dumps({"evals": [{"query": "hello", "should_trigger": False}]}), encoding="utf-8")
            args = SimpleNamespace(eval_set=str(path), split="tune")
            rows = tm.eval_rows_from_args(args, DEMO_MANIFEST)
        self.assertEqual(rows, [{"query": "hello", "should_trigger": False}])

    def test_pi_trigger_runner_invokes_pi_from_isolated_workspace(self):
        seen = {}

        def fake_run(argv, *, cwd, env, timeout):
            seen.update({"argv": argv, "cwd": str(cwd), "config_dir": env["PI_CODING_AGENT_DIR"], "timeout": timeout})
            return {"stdout": "", "stderr": "", "returncode": 0, "timed_out": False,
                    "elapsed_ms": 1, "observation_complete": True}

        with mock.patch.object(tr, "run_argv_with_timeout", side_effect=fake_run):
            result = tr.run_query(DEMO_MANIFEST, "ordinary chat", False, 12, None)
        self.assertTrue(result["pass"])
        self.assertEqual(seen["cwd"], seen["config_dir"])
        self.assertIn("pi-trigger-", seen["cwd"])
        self.assertNotEqual(Path(seen["cwd"]).resolve(), ROOT.resolve())


    def test_pi_json_provider_error_cannot_pass_a_negative_trigger(self):
        provider_error = json.dumps({
            "type": "agent_end", "willRetry": False,
            "messages": [{"role": "assistant", "content": [], "stopReason": "error",
                          "errorMessage": "Mistral API error (400): Invalid model",
                          "usage": {"input": 7, "output": 2, "totalTokens": 9,
                                    "cost": {"total": 0.009}}}],
        })

        def failed_provider(*args, **kwargs):
            return {"stdout": provider_error + "\n", "stderr": "", "returncode": 0, "timed_out": False,
                    "elapsed_ms": 1, "observation_complete": True}

        with tempfile.TemporaryDirectory() as td, mock.patch.object(tr, "run_argv_with_timeout", side_effect=failed_provider):
            trace_dir = Path(td) / "trace"
            result = tr.run_query(DEMO_MANIFEST, "ordinary chat", False, 12, None, trace_dir=trace_dir)
            artifacts = [
                json.loads((trace_dir / name).read_text(encoding="utf-8"))
                for name in ("metrics.json", "metadata.json")
            ]
        self.assertFalse(result["observation_complete"])
        self.assertFalse(result["pass"])
        self.assertEqual(result["usage_normalized"], {"source": "missing"})
        self.assertEqual(result["cost_normalized"], {"source": "missing"})
        self.assertIn("Invalid model", result["provider_error"])
        for artifact in artifacts:
            self.assertEqual(artifact["usage_normalized"], {"source": "missing"})
            self.assertEqual(artifact["cost_normalized"], {"source": "missing"})
            measurements = artifact["telemetry"]["measurements"]
            self.assertEqual(measurements["total_tokens"]["availability"], "unavailable")
            self.assertEqual(measurements["cost"]["availability"], "unavailable")

    def test_pi_adapter_propagates_json_provider_error_as_incomplete(self):
        provider_error = json.dumps({
            "type": "agent_end", "willRetry": False,
            "messages": [{"stopReason": "error", "errorMessage": "provider rejected model"}],
        })
        run = {"stdout": provider_error, "stderr": "", "returncode": 0, "timed_out": False,
               "elapsed_ms": 1, "observation_complete": True}
        with tempfile.TemporaryDirectory() as td, mock.patch.object(tm.PiAdapter, "_run_argv", staticmethod(lambda *args, **kwargs: run)):
            workspace = Path(td) / "workspace"
            workspace.mkdir()
            result = tm.PiAdapter().invoke("ordinary chat", None, workspace, 12)
        self.assertFalse(result["observation_complete"])
        self.assertEqual(result["provider_error"], "provider rejected model")

    def test_pi_timeout_with_parseable_partial_trace_is_not_telemetry_complete(self):
        def timed_out(*args, **kwargs):
            return {"stdout": json.dumps({"type": "command", "command": "partial"}) + "\n",
                    "stderr": "timeout", "returncode": 124, "timed_out": True,
                    "elapsed_ms": 10, "observation_complete": False}

        with tempfile.TemporaryDirectory() as td, mock.patch.object(tr, "run_argv_with_timeout", side_effect=timed_out):
            trace_dir = Path(td) / "trace"
            tr.run_query(DEMO_MANIFEST, "ordinary chat", False, 1, None, trace_dir=trace_dir)
            meta = json.loads((trace_dir / "metadata.json").read_text(encoding="utf-8"))
        self.assertFalse(meta["observation_complete"])
        self.assertEqual(meta["telemetry"]["measurements"]["commands"]["availability"], "unavailable")


class StubMatrixOfflineTests(unittest.TestCase):
    def test_demo_manifest_has_both_polarities(self):
        rows = demo_trigger_rows()
        self.assertEqual(len(rows), 2)
        self.assertEqual({r["should_trigger"] for r in rows}, {True, False})

    def test_stub_matrix_passes_both_polarities_per_model(self):
        report = tm.run_matrix(DEMO_MANIFEST, demo_trigger_rows(), agents=["stub"],
                               models=["haiku", "sonnet", "opus"], runs_per_query=2,
                               timeout=30, workers=2)
        self.assertEqual(report["evidence_class"], "raw_autonomous_trigger_measurement")
        self.assertTrue(report["skill_tree_hash"])
        self.assertEqual(len(report["matrix"]), 3)   # one cell per model
        for cell in report["matrix"]:
            s = cell["summary"]
            self.assertEqual((s["should_trigger"]["passed"], s["should_trigger"]["total"]), (2, 2), cell["model"])
            self.assertEqual((s["should_not_trigger"]["passed"], s["should_not_trigger"]["total"]), (2, 2), cell["model"])
            self.assertEqual(s["incomplete_observations"], 0)
        self.assertEqual(report["summary"]["pass_rate"], 1.0)

    def test_trace_runs_are_written_for_matrix_agents(self):
        with tempfile.TemporaryDirectory() as td:
            trace_root = Path(td) / "traces"
            report = tm.run_matrix(DEMO_MANIFEST, demo_trigger_rows()[:1], agents=["stub"],
                                   models=["offline"], runs_per_query=1,
                                   timeout=30, workers=1, trace_runs=trace_root)
            trace_dir = Path(report["results"][0]["trace_dir"])
            self.assertTrue((trace_dir / "trace.jsonl").is_file())
            self.assertTrue((trace_dir / "events.json").is_file())
            self.assertTrue((trace_dir / "metrics.json").is_file())
            meta = json.loads((trace_dir / "metadata.json").read_text(encoding="utf-8"))
            self.assertEqual(meta["provider"], "stub")
            self.assertEqual(meta["population"], "trigger")
            self.assertEqual(meta["telemetry"]["population"], "trigger")
            self.assertEqual(meta["measurement"], "raw_measurement")
            self.assertEqual(report["results"][0]["measurement"], "raw_measurement")
            self.assertTrue(trace_dir.is_relative_to(trace_root))

    def test_trace_runs_use_unique_matrix_root_per_invocation(self):
        with tempfile.TemporaryDirectory() as td, mock.patch.object(tm.time, "time", return_value=1234567890):
            trace_root = Path(td) / "traces"
            first = tm.run_matrix(DEMO_MANIFEST, demo_trigger_rows()[:1], agents=["stub"],
                                  models=["offline"], runs_per_query=2,
                                  timeout=30, workers=1, trace_runs=trace_root)
            second = tm.run_matrix(DEMO_MANIFEST, demo_trigger_rows()[:1], agents=["stub"],
                                   models=["offline"], runs_per_query=1,
                                   timeout=30, workers=1, trace_runs=trace_root)
        first_roots = {Path(r["trace_dir"]).relative_to(trace_root).parts[0] for r in first["results"]}
        second_roots = {Path(r["trace_dir"]).relative_to(trace_root).parts[0] for r in second["results"]}
        self.assertEqual(len(first_roots), 1)
        self.assertEqual(len(second_roots), 1)
        self.assertNotEqual(first_roots, second_roots)

    def test_trace_model_segment_is_path_safe(self):
        with tempfile.TemporaryDirectory() as td:
            trace_root = Path(td) / "traces"
            report = tm.run_matrix(DEMO_MANIFEST, demo_trigger_rows()[:1], agents=["stub"],
                                   models=["../bad/model"], runs_per_query=1,
                                   timeout=30, workers=1, trace_runs=trace_root)
            trace_dir = Path(report["results"][0]["trace_dir"])
        self.assertTrue(trace_dir.is_relative_to(trace_root))
        parts = trace_dir.relative_to(trace_root).parts
        self.assertNotIn("..", parts)
        self.assertIn("bad-model", parts)

    def test_baseline_provenance_records_skill_tree_hash(self):
        report = tm.run_matrix(DEMO_MANIFEST, demo_trigger_rows()[:1], agents=["stub"],
                               models=[None], runs_per_query=1, timeout=30, workers=1)
        self.assertEqual(report["provenance"], {"mode": "baseline", "skill_tree_hash": report["skill_tree_hash"]})

    def test_demo_manifest_contains_documented_discovery_ablation(self):
        manifest = tm.load_manifest(DEMO_MANIFEST)
        repo_root = tm.repo_root_for_manifest(DEMO_MANIFEST)
        with tempfile.TemporaryDirectory() as td:
            _, tree_hash, provenance = tm.trigger_tree_for_manifest(repo_root, manifest, Path(td), "weaker-description")
        self.assertEqual(provenance["id"], "weaker-description")
        self.assertEqual(provenance["population"], "trigger")
        self.assertEqual(tree_hash, provenance["parent_skill_hash"])
        self.assertNotIn("dir", provenance)
        self.assertNotIn("skill_files", provenance)

    def test_duplicate_agents_or_models_are_rejected(self):
        with self.assertRaises(SystemExit) as agent_ctx:
            tm.run_matrix(DEMO_MANIFEST, demo_trigger_rows()[:1], agents=["stub", "stub"],
                          models=[None], runs_per_query=1, timeout=30, workers=1)
        self.assertIn("duplicate --agent", str(agent_ctx.exception))
        with self.assertRaises(SystemExit) as model_ctx:
            tm.run_matrix(DEMO_MANIFEST, demo_trigger_rows()[:1], agents=["stub"],
                          models=["same", "same"], runs_per_query=1, timeout=30, workers=1)
        self.assertIn("duplicate --model", str(model_ctx.exception))

    def test_trace_write_failure_does_not_discard_observation(self):
        with tempfile.TemporaryDirectory() as td, mock.patch.object(tm, "write_trace_artifacts", side_effect=OSError("ENOSPC")):
            report = tm.run_matrix(DEMO_MANIFEST, demo_trigger_rows()[:1], agents=["stub"],
                                   models=[None], runs_per_query=1, timeout=30, workers=1,
                                   trace_runs=Path(td) / "traces")
        self.assertEqual(report["summary"]["total"], 1)
        self.assertEqual(report["summary"]["passed"], 1)
        self.assertIn("ENOSPC", report["results"][0]["trace_error"])

    def test_worker_exception_becomes_incomplete_row(self):
        class FailingAdapter(tm.AgentAdapter):
            name = "stub"

            def mount(self, tree_dir, workspace):
                raise OSError("disk full")

            def invoke(self, query, model, workspace, timeout):
                raise AssertionError("unreachable")

        old = tm.ADAPTERS["stub"]
        try:
            tm.ADAPTERS["stub"] = FailingAdapter
            report = tm.run_matrix(DEMO_MANIFEST, demo_trigger_rows()[:1], agents=["stub"],
                                   models=[None], runs_per_query=1, timeout=30, workers=1)
        finally:
            tm.ADAPTERS["stub"] = old
        self.assertEqual(report["summary"]["total"], 1)
        self.assertEqual(report["summary"]["passed"], 0)
        self.assertEqual(report["matrix"][0]["summary"]["incomplete_observations"], 1)
        self.assertIn("disk full", report["results"][0]["error"])

    def test_trace_redacts_workspace_credentials(self):
        class SecretEchoAdapter(tm.AgentAdapter):
            name = "secret"

            def mount(self, tree_dir, workspace):
                (workspace / ".codex").mkdir(parents=True)
                (workspace / ".codex" / "auth.json").write_text('{"token":"SECRET-TOKEN-12345"}', encoding="utf-8")
                return self._mount_tree(tree_dir, workspace / "skills")

            def invoke(self, query, model, workspace, timeout):
                skill = next((workspace / "skills").glob("*/SKILL.md"))
                stdout = json.dumps({
                    "type": "command",
                    "command": ["bash", "-lc", f"cat {skill}; echo SECRET-TOKEN-12345"],
                }) + "\n"
                return {"stdout": stdout, "stderr": "", "returncode": 0, "timed_out": False,
                        "elapsed_ms": 1, "observation_complete": True}

        with tempfile.TemporaryDirectory() as td:
            tree = tm.build_canonical_skill_tree(tm.repo_root_for_manifest(DEMO_MANIFEST), tm.load_manifest(DEMO_MANIFEST), Path(td) / "tree")
            trace_dir = Path(td) / "trace"
            row = tm.run_cell_query(SecretEchoAdapter(), tree, "q", True, None, 12, trace_dir)
            trace_text = (trace_dir / "trace.jsonl").read_text(encoding="utf-8")
            metadata_text = (trace_dir / "metadata.json").read_text(encoding="utf-8")
            metrics_text = (trace_dir / "metrics.json").read_text(encoding="utf-8")
        self.assertTrue(row["triggered"])
        self.assertNotIn("SECRET-TOKEN-12345", trace_text)
        self.assertNotIn("SECRET-TOKEN-12345", json.dumps(row))
        self.assertNotIn("SECRET-TOKEN-12345", metadata_text)
        self.assertNotIn("SECRET-TOKEN-12345", metrics_text)
        self.assertIn("[REDACTED]", trace_text)
        self.assertIn("[REDACTED]", json.dumps(row))

    def test_weakened_description_under_triggers_offline(self):
        """The loop's core signal, deterministic: strip the description of the
        words users actually type and the (stub) agent stops loading the skill
        on the should-fire query."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            skill_dir = root / "skills" / "demo"
            (skill_dir / "references").mkdir(parents=True)
            source = (ROOT / "examples" / "demo-skill" / "skills" / "demo" / "SKILL.md").read_text(encoding="utf-8")
            weakened = source.replace(
                "description: Demo skill for the Skill Eval Harness example. Use it to review a proposed change and label the severity of each finding.",
                "description: General assistance helper.")
            (skill_dir / "SKILL.md").write_text(weakened, encoding="utf-8")
            (skill_dir / "references" / "checklist.md").write_text("checklist\n", encoding="utf-8")
            evals = root / "evals"
            evals.mkdir()
            manifest_path = evals / "shared-benchmark.json"
            manifest_path.write_text(json.dumps({
                "version": 1, "skill_name": "demo-reviewer",
                "skill_paths": ["skills/demo/SKILL.md"],
                "variants": ["with_skill", "without_skill"], "cases": [],
            }), encoding="utf-8")
            rows = [r for r in demo_trigger_rows() if r["should_trigger"]]
            report = tm.run_matrix(manifest_path, rows, agents=["stub"], models=["haiku"],
                                   runs_per_query=1, timeout=30, workers=1)
            cell = report["matrix"][0]
            self.assertEqual(cell["summary"]["should_trigger"]["passed"], 0,
                             "a description without the user's words must stop triggering the stub")

    def test_unknown_agent_names_the_extension_seam(self):
        with self.assertRaises(SystemExit) as ctx:
            tm.run_matrix(DEMO_MANIFEST, demo_trigger_rows(), agents=["missing-agent"], models=None,
                          runs_per_query=1, timeout=30, workers=1)
        self.assertIn("AgentAdapter", str(ctx.exception))


class ClaudeDetectionTests(unittest.TestCase):
    """Canned claude -p stream-json fragments; no subprocess."""

    def _adapter(self):
        return tm.ClaudeAdapter()

    def test_skill_tool_use_by_name_is_trigger_evidence(self):
        stream = json.dumps({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "Skill", "input": {"skill": "demo-reviewer", "args": "..."}}]}})
        triggered, evidence = self._adapter().detect(stream, ["demo-reviewer"], [])
        self.assertTrue(triggered)
        self.assertIn("Skill tool invoked: demo-reviewer", evidence)

    def test_other_skills_and_plain_answers_are_not_evidence(self):
        stream = "\n".join([
            json.dumps({"type": "assistant", "message": {"content": [
                {"type": "tool_use", "name": "Skill", "input": {"skill": "code-review"}}]}}),
            json.dumps({"type": "assistant", "message": {"content": [
                {"type": "text", "text": "I would use the demo-reviewer skill here."}]}}),
        ])
        triggered, _ = self._adapter().detect(stream, ["demo-reviewer"], [])
        self.assertFalse(triggered, "a different skill firing, or the name in prose, is not load evidence")

    def test_reading_the_mounted_skill_md_is_fallback_evidence(self):
        mounted = Path("/tmp/trigger-x/.claude/skills/demo-reviewer/SKILL.md")
        stream = json.dumps({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "Read", "input": {"file_path": str(mounted)}}]}})
        triggered, _ = self._adapter().detect(stream, ["demo-reviewer"], [mounted])
        self.assertTrue(triggered)

    def test_max_turns_is_a_completed_observation_window(self):
        self.assertEqual(tm.ClaudeAdapter._result_subtype(
            json.dumps({"type": "result", "subtype": "error_max_turns"})), "error_max_turns")

    def test_mounted_skill_names_read_frontmatter(self):
        with tempfile.TemporaryDirectory() as td:
            skill_md = Path(td) / "some-dir" / "SKILL.md"
            skill_md.parent.mkdir()
            skill_md.write_text("---\nname: demo-reviewer\ndescription: x\n---\n", encoding="utf-8")
            self.assertEqual(tm.mounted_skill_names([skill_md]), ["demo-reviewer"])

    def test_claude_invoke_seeds_portable_auth_into_isolated_config(self):
        seen = {}

        def fake_run(argv, *, cwd, env, timeout):
            config_dir = Path(env["CLAUDE_CONFIG_DIR"])
            seen["config_dir"] = config_dir
            seen["credentials"] = (config_dir / ".credentials.json").read_text(encoding="utf-8")
            return {"stdout": "{}\n", "stderr": "", "returncode": 0, "timed_out": False,
                    "elapsed_ms": 1, "observation_complete": True}

        with tempfile.TemporaryDirectory() as td, mock.patch.object(tm.ClaudeAdapter, "_run_argv", staticmethod(fake_run)):
            root = Path(td)
            source = root / "user-claude"
            source.mkdir()
            (source / ".credentials.json").write_text('{"token":"t"}', encoding="utf-8")
            with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(source)}, clear=True):
                result = tm.ClaudeAdapter().invoke("q", "haiku", root / "run", 12)
        self.assertEqual(seen["credentials"], '{"token":"t"}')
        self.assertTrue(str(seen["config_dir"]).endswith(".trigger-config"))
        self.assertTrue(result["config_isolated"])
        self.assertNotIn("config_isolation_warning", result)

    def test_claude_invoke_preserves_nonportable_oauth_config(self):
        seen = {}

        def fake_run(argv, *, cwd, env, timeout):
            seen["config_dir"] = env.get("CLAUDE_CONFIG_DIR")
            return {"stdout": "{}\n", "stderr": "", "returncode": 0, "timed_out": False,
                    "elapsed_ms": 1, "observation_complete": True}

        with tempfile.TemporaryDirectory() as td, mock.patch.object(tm.ClaudeAdapter, "_run_argv", staticmethod(fake_run)):
            root = Path(td)
            source = root / "oauth-backed-claude"
            source.mkdir()
            workspace = root / "run"
            with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(source)}, clear=True):
                result = tm.ClaudeAdapter().invoke("q", "haiku", workspace, 12)
        self.assertEqual(seen["config_dir"], str(source))
        self.assertFalse((workspace / ".trigger-config").exists())
        self.assertFalse(result["config_isolated"])
        self.assertIn("personal config may influence", result["config_isolation_warning"])


class CodexAdapterTests(unittest.TestCase):
    """Codex trigger support without a live codex binary."""

    def test_codex_is_registered_and_declares_matrix_capability(self):
        self.assertIn("codex", tm.ADAPTERS)
        cap = tm.matrix_capabilities()["codex"]
        self.assertTrue(cap.autonomous_trigger)
        self.assertTrue(cap.trigger_ablation)
        parser = tm.build_arg_parser()
        agent_action = next(a for a in parser._actions if "--agent" in getattr(a, "option_strings", ()))
        self.assertIn("codex", agent_action.choices)

    def test_codex_uses_shared_path_evidence_detector(self):
        mounted = Path("/tmp/trigger-x/.codex/skills/demo-reviewer/SKILL.md")
        stream = json.dumps({"type": "command", "command": ["bash", "-lc", f"cat {mounted}"]})
        triggered, evidence = tm.CodexAdapter().detect(stream, ["demo-reviewer"], [mounted])
        self.assertTrue(triggered)
        self.assertTrue(evidence)
        prose = json.dumps({"type": "message", "content": "I would use demo-reviewer."})
        self.assertEqual(tm.CodexAdapter().detect(prose, ["demo-reviewer"], [mounted]), (False, []))

    def test_codex_invoke_appends_raw_query_model_and_external_skill_dir(self):
        seen = {}

        def fake_run(argv, *, cwd, env, timeout):
            seen.update({"argv": argv, "cwd": cwd, "env": env, "timeout": timeout})
            return {"stdout": "{}\n", "stderr": "", "returncode": 0, "timed_out": False,
                    "elapsed_ms": 1, "observation_complete": True}

        with mock.patch.object(tm.CodexAdapter, "_run_argv", staticmethod(fake_run)):
            with tempfile.TemporaryDirectory() as td, mock.patch.dict(os.environ, {"CODEX_HOME": str(Path(td) / "source-codex")}):
                workspace = Path(td) / "workspace"
                workspace.mkdir()
                result = tm.CodexAdapter(codex_cmd="codex exec --json").invoke("raw trigger query", "o4-mini", workspace, 12)
        self.assertEqual(seen["argv"][:3], ["codex", "exec", "--json"])
        self.assertIn("--add-dir", seen["argv"])
        skill_dir = Path(seen["argv"][seen["argv"].index("--add-dir") + 1])
        self.assertEqual(skill_dir, Path(seen["env"]["CODEX_HOME"]) / "skills")
        self.assertFalse(Path(seen["env"]["CODEX_HOME"]).is_relative_to(seen["cwd"]))
        self.assertEqual(seen["argv"][-3:], ["--model", "o4-mini", "raw trigger query"])
        self.assertEqual(seen["timeout"], 12)
        self.assertTrue(result["codex_home_outside_workdir"])
        self.assertIs(tm.CodexAdapter()._run_argv, tm.run_argv_with_timeout)

    def test_codex_invoke_seeds_auth_without_copying_user_skills(self):
        seen = {}

        def fake_run(argv, *, cwd, env, timeout):
            codex_home = Path(env["CODEX_HOME"])
            seen["auth"] = (codex_home / "auth.json").read_text(encoding="utf-8")
            seen["config"] = (codex_home / "config.toml").read_text(encoding="utf-8")
            seen["mounted_skills_survive"] = (codex_home / "skills" / "demo").is_dir()
            seen["user_skills_not_copied"] = not (codex_home / "skills" / "personal").exists()
            seen["workspace_auth_present"] = (Path(cwd) / ".codex" / "auth.json").exists()
            seen["workspace_config_present"] = (Path(cwd) / ".codex" / "config.toml").exists()
            return {"stdout": "{}\n", "stderr": "", "returncode": 0, "timed_out": False,
                    "elapsed_ms": 1, "observation_complete": True}

        with tempfile.TemporaryDirectory() as td, mock.patch.object(tm.CodexAdapter, "_run_argv", staticmethod(fake_run)):
            root = Path(td)
            source = root / "user-codex"
            (source / "skills" / "personal").mkdir(parents=True)
            (source / "auth.json").write_text('{"token":"t"}', encoding="utf-8")
            (source / "config.toml").write_text("model = 'm'\n", encoding="utf-8")
            workspace = root / "run"
            workspace.mkdir()
            tree = root / "tree"
            (tree / "demo").mkdir(parents=True)
            (tree / "demo" / "SKILL.md").write_text("---\nname: demo\n---\n", encoding="utf-8")
            adapter = tm.CodexAdapter(codex_cmd="codex exec --json")
            adapter.mount(tree, workspace)
            with mock.patch.dict(os.environ, {"CODEX_HOME": str(source)}):
                result = adapter.invoke("q", None, workspace, 12)
        self.assertEqual(seen["auth"], '{"token":"t"}')
        self.assertEqual(seen["config"], "model = 'm'\n")
        self.assertTrue(seen["mounted_skills_survive"])
        self.assertTrue(seen["user_skills_not_copied"])
        self.assertTrue(result["codex_home_outside_workdir"])
        self.assertFalse(seen["workspace_auth_present"])
        self.assertFalse(seen["workspace_config_present"])

    def test_run_cell_query_redacts_ambient_env_secrets(self):
        class LeakyAdapter(tm.AgentAdapter):
            name = "stub"

            def mount(self, tree_dir, workspace):
                return self._mount_tree(tree_dir, workspace / "skills")

            def invoke(self, query, model, workspace, timeout):
                secret = os.environ["MISTRAL_API_KEY"]
                return {"stdout": f"leaked {secret}\n", "stderr": f"err {secret}", "returncode": 0,
                        "timed_out": False, "elapsed_ms": 1, "observation_complete": True}

        with tempfile.TemporaryDirectory() as td, mock.patch.dict(os.environ, {"MISTRAL_API_KEY": "ambient-secret-token"}):
            tree = Path(td) / "tree"
            skill = tree / "demo"
            skill.mkdir(parents=True)
            (skill / "SKILL.md").write_text("---\nname: demo\n---\n", encoding="utf-8")
            row = tm.run_cell_query(LeakyAdapter(), tree, "q", False, None, 12, trace_dir=Path(td) / "trace")
            trace_text = (Path(row["trace_dir"]) / "trace.jsonl").read_text(encoding="utf-8")
        self.assertNotIn("ambient-secret-token", row["stderr"])
        self.assertEqual(row["stderr"], "err [REDACTED]")
        self.assertNotIn("ambient-secret-token", trace_text)

    def test_run_cell_query_requires_invoke_contract(self):
        class BrokenAdapter(tm.AgentAdapter):
            name = "broken"

            def mount(self, tree_dir, workspace):
                return self._mount_tree(tree_dir, workspace / "skills")

            def invoke(self, query, model, workspace, timeout):
                return {"stdout": "{}\n", "stderr": "", "returncode": 0, "timed_out": False, "elapsed_ms": 1}

        with tempfile.TemporaryDirectory() as td:
            tree = Path(td) / "tree"
            skill = tree / "demo"
            skill.mkdir(parents=True)
            (skill / "SKILL.md").write_text("---\nname: demo\n---\n", encoding="utf-8")
            with self.assertRaises(KeyError) as ctx:
                tm.run_cell_query(BrokenAdapter(), tree, "q", True, None, 12)
        self.assertIn("observation_complete", str(ctx.exception))

    def test_missing_capability_row_fails_before_any_runs(self):
        class UnregisteredAdapter(tm.AgentAdapter):
            name = "my-agent"

            def mount(self, tree_dir, workspace):
                raise AssertionError("mount should not run before capability validation")

            def invoke(self, query, model, workspace, timeout):
                raise AssertionError("invoke should not run before capability validation")

        old = dict(tm.ADAPTERS)
        try:
            tm.ADAPTERS["my-agent"] = UnregisteredAdapter
            with self.assertRaises(SystemExit) as ctx:
                tm.run_matrix(DEMO_MANIFEST, demo_trigger_rows()[:1], agents=["my-agent"],
                              models=[None], runs_per_query=1, timeout=30, workers=1)
        finally:
            tm.ADAPTERS.clear()
            tm.ADAPTERS.update(old)
        self.assertIn("AGENT_CAPABILITIES", str(ctx.exception))

    def test_codex_default_command_is_single_owner(self):
        self.assertEqual(tm.CodexAdapter().codex_cmd, tm.DEFAULT_CODEX_CMD)
        parser = tm.build_arg_parser()
        codex_action = next(a for a in parser._actions if "--codex-cmd" in getattr(a, "option_strings", ()))
        self.assertEqual(codex_action.default, tm.DEFAULT_CODEX_CMD)


class VibeAdapterTests(unittest.TestCase):
    """Mistral Vibe trigger support without a live API key."""

    def test_vibe_is_registered_and_declares_matrix_capability(self):
        self.assertIn("vibe", tm.ADAPTERS)
        cap = tm.matrix_capabilities()["vibe"]
        self.assertTrue(cap.autonomous_trigger)
        self.assertTrue(cap.trigger_ablation)
        parser = tm.build_arg_parser()
        agent_action = next(a for a in parser._actions if "--agent" in getattr(a, "option_strings", ()))
        self.assertIn("vibe", agent_action.choices)
        vibe_action = next(a for a in parser._actions if "--vibe-cmd" in getattr(a, "option_strings", ()))
        self.assertEqual(vibe_action.default, tm.VIBE_DEFAULT_CMD)

    def test_vibe_mounts_project_agent_skills(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            tree = root / "tree"
            skill = tree / "demo-root"
            skill.mkdir(parents=True)
            (skill / "SKILL.md").write_text("---\nname: demo-reviewer\n---\n", encoding="utf-8")
            copied = tm.VibeAdapter().mount(tree, root / "workspace")
        self.assertEqual(copied[0].parts[-4:], ("workspace", ".agents", "skills", "demo-root", "SKILL.md")[-4:])
        self.assertIn(".agents", str(copied[0]))

    def test_vibe_detects_native_skill_tool_call(self):
        stream = json.dumps({"role": "assistant", "tool_calls": [{"function": {"name": "skill", "arguments": json.dumps({"name": "demo-reviewer"})}}]})
        triggered, evidence = tm.VibeAdapter().detect(stream, ["demo-reviewer"], [])
        self.assertTrue(triggered)
        self.assertIn("Vibe skill tool invoked: demo-reviewer", evidence)
        other = json.dumps({"role": "assistant", "tool_calls": [{"function": {"name": "skill", "arguments": json.dumps({"name": "other"})}}]})
        self.assertEqual(tm.VibeAdapter().detect(other, ["demo-reviewer"], []), (False, []))

    def test_vibe_invoke_uses_isolated_home_model_env_and_prompt_arg(self):
        seen = {}

        def fake_run(argv, *, cwd, env, timeout, input_text=None):
            seen.update({"argv": argv, "cwd": cwd, "env": env, "timeout": timeout, "input_text": input_text})
            seen["vibe_home_inside_workdir"] = Path(env["VIBE_HOME"]).is_relative_to(Path(cwd))
            seen["workspace_vibe_env_present"] = (Path(cwd) / ".vibe-home" / ".env").exists()
            return {"stdout": json.dumps({"role": "assistant", "content": "ok"}) + "\n", "stderr": "", "returncode": 0,
                    "timed_out": False, "elapsed_ms": 1, "observation_complete": True}

        with tempfile.TemporaryDirectory() as td, mock.patch.object(tm.VibeAdapter, "_run_argv", staticmethod(fake_run)):
            workspace = Path(td) / "workspace"
            workspace.mkdir()
            result = tm.VibeAdapter(vibe_cmd=f"{sys.executable} fake_vibe.py", max_turns=4).invoke("raw trigger query", "mistral-small", workspace, 12)
        self.assertIn("--prompt", seen["argv"])
        self.assertEqual(seen["argv"][seen["argv"].index("--prompt") + 1], "raw trigger query")
        self.assertIn("--output", seen["argv"])
        self.assertIn("--workdir", seen["argv"])
        self.assertIn("--enabled-tools", seen["argv"])
        self.assertIn("skill", seen["argv"])
        self.assertEqual(seen["input_text"], "")
        self.assertEqual(seen["env"]["VIBE_ACTIVE_MODEL"], "mistral-small")
        self.assertFalse(seen["vibe_home_inside_workdir"])
        self.assertFalse(seen["workspace_vibe_env_present"])
        self.assertTrue(result["config_isolated"])
        self.assertTrue(result["vibe_home_outside_workdir"])


def _csv_env(name, default):
    raw = os.environ.get(name)
    if raw is None:
        return list(default)
    return [part.strip() or None for part in raw.split(",")]


def _live_invoke_smoke_agents():
    configured = [name for name in _csv_env("AGENT_INVOKE_SMOKE_AGENTS", []) if name]
    if configured:
        return [str(name) for name in configured if name]
    return [
        name for name in tm.ADAPTERS
        if name != "stub" and tm.require_agent_capabilities(name).autonomous_trigger
    ]


def _live_invoke_smoke_models(agent_name, adapter):
    upper = agent_name.upper().replace("-", "_")
    models = _csv_env(f"{upper}_INVOKE_SMOKE_MODELS", adapter.default_models)
    if os.environ.get(f"{upper}_INVOKE_SMOKE_MODEL") is not None:
        models = [os.environ.get(f"{upper}_INVOKE_SMOKE_MODEL") or None]
    return models


@unittest.skipUnless(os.environ.get("RUN_AGENT_INVOKE_SMOKE") == "1",
                     "cheap manual smoke: set RUN_AGENT_INVOKE_SMOKE=1 (needs live agent CLIs + credentials, spends tiny requests)")
class AgentInvokeSmokeTests(unittest.TestCase):
    def test_live_agents_complete_trivial_prompt_for_each_model(self):
        query = os.environ.get("AGENT_INVOKE_SMOKE_QUERY", "Reply with exactly: OK")
        timeout = int(os.environ.get("AGENT_INVOKE_SMOKE_TIMEOUT", "90"))
        manifest = tm.load_manifest(DEMO_MANIFEST)
        repo_root = tm.repo_root_for_manifest(DEMO_MANIFEST)
        results = []
        failures = []

        for agent_name in _live_invoke_smoke_agents():
            try:
                adapter = tm.adapter_instance(
                    agent_name,
                    claude_bin=os.environ.get("CLAUDE_INVOKE_SMOKE_BIN", "claude"),
                    codex_cmd=os.environ.get("CODEX_INVOKE_SMOKE_CMD", tm.DEFAULT_CODEX_CMD),
                    vibe_cmd=os.environ.get("VIBE_INVOKE_SMOKE_CMD", tm.VIBE_DEFAULT_CMD),
                    max_turns=int(os.environ.get("CLAUDE_INVOKE_SMOKE_MAX_TURNS", "1")),
                )
                models = _live_invoke_smoke_models(agent_name, adapter)
            except Exception as exc:
                failures.append(f"{agent_name}/(setup): {exc!r}")
                results.append({"agent": agent_name, "model": "(setup)", "ok": False, "error": repr(exc)})
                continue

            for model in models:
                model_label = model or "(default)"
                row = {"agent": agent_name, "model": model_label}
                try:
                    with tempfile.TemporaryDirectory(prefix=f"{agent_name}-invoke-smoke-") as td:
                        workspace = Path(td)
                        tree = tm.build_canonical_skill_tree(repo_root, manifest, workspace / "tree")
                        copied = adapter.mount(tree, workspace)
                        result = tm.validate_invoke_result(
                            agent_name,
                            adapter.invoke(query, model, workspace, timeout),
                        )
                    row.update({
                        "returncode": result["returncode"],
                        "timed_out": result["timed_out"],
                        "observation_complete": result["observation_complete"],
                        "elapsed_ms": result["elapsed_ms"],
                        "stdout_bytes": len(str(result["stdout"])),
                        "stdout_tail": str(result["stdout"])[-300:],
                        "stderr_tail": str(result["stderr"])[-300:],
                        "mounted_paths": len(copied),
                    })
                    ok = (
                        not result["timed_out"] and
                        result["returncode"] == 0 and
                        result["observation_complete"] and
                        bool(str(result["stdout"]).strip())
                    )
                    row["ok"] = ok
                    if not ok:
                        failures.append(
                            f"{agent_name}/{model_label}: returncode={result['returncode']} "
                            f"timed_out={result['timed_out']} observation_complete={result['observation_complete']} "
                            f"stdout_bytes={len(str(result['stdout']))} "
                            f"stdout_tail={str(result['stdout'])[-300:]!r} "
                            f"stderr_tail={str(result['stderr'])[-300:]!r}"
                        )
                except Exception as exc:
                    row.update({"ok": False, "error": repr(exc)})
                    failures.append(f"{agent_name}/{model_label}: {exc!r}")
                results.append(row)

        if not results:
            failures.append("no live agent/model smoke targets were selected")
        print(json.dumps({"cheap_invoke_smoke": results}, indent=2, sort_keys=True))
        self.assertFalse(failures, "\n".join(failures))


class AgentInvokeSmokeConfigTests(unittest.TestCase):
    def test_default_cheap_smoke_targets_every_live_supported_agent_and_model(self):
        clean_env = {
            "AGENT_INVOKE_SMOKE_AGENTS": "",
            "CLAUDE_INVOKE_SMOKE_MODELS": "",
            "CODEX_INVOKE_SMOKE_MODEL": "",
            "PI_INVOKE_SMOKE_MODEL": "",
            "VIBE_INVOKE_SMOKE_MODEL": "",
        }
        with mock.patch.dict(os.environ, clean_env, clear=False):
            for key in clean_env:
                os.environ.pop(key, None)
            agents = _live_invoke_smoke_agents()
            models = {
                name: _live_invoke_smoke_models(name, tm.adapter_instance(name))
                for name in agents
            }
        self.assertEqual(agents, ["claude", "codex", "pi", "vibe"])
        self.assertEqual(models["claude"], ["haiku", "sonnet", "opus"])
        self.assertEqual(models["codex"], [None])
        self.assertEqual(models["pi"], [None])
        self.assertEqual(models["vibe"], [None])

    def test_advertised_live_smoke_envs_are_consumed_by_tests(self):
        test_source = Path(__file__).read_text(encoding="utf-8")
        for agent, cap in AGENT_CAPABILITIES.items():
            if cap.live_smoke_env:
                self.assertIn(cap.live_smoke_env, test_source, agent)


@unittest.skipUnless(os.environ.get("RUN_TRIGGER_SMOKE") == "1",
                     "manual smoke: set RUN_TRIGGER_SMOKE=1 (needs claude CLI + credentials, spends tokens)")
class ClaudeMatrixSmokeTests(unittest.TestCase):
    def test_haiku_sonnet_opus_matrix_end_to_end(self):
        runs = int(os.environ.get("TRIGGER_SMOKE_RUNS", "1"))
        report = tm.run_matrix(DEMO_MANIFEST, demo_trigger_rows(), agents=["claude"],
                               models=["haiku", "sonnet", "opus"], runs_per_query=runs,
                               timeout=300, workers=3)
        tm.print_matrix(report["matrix"])
        self.assertEqual(len(report["matrix"]), 3)
        incomplete = [r for r in report["results"] if not r["observation_complete"]]
        self.assertFalse(incomplete, f"broken runs (crash/timeout), not trigger signal: {incomplete}")
        self.assertTrue(any(r["triggered"] for r in report["results"]),
                        "no model loaded the skill on any run — detection or mounting is broken")


@unittest.skipUnless(os.environ.get("RUN_CODEX_TRIGGER_SMOKE") == "1",
                     "manual smoke: set RUN_CODEX_TRIGGER_SMOKE=1 (needs codex CLI + credentials, spends tokens)")
class CodexMatrixSmokeTests(unittest.TestCase):
    def test_codex_matrix_end_to_end(self):
        runs = int(os.environ.get("CODEX_TRIGGER_SMOKE_RUNS", "1"))
        model = os.environ.get("CODEX_TRIGGER_SMOKE_MODEL")
        report = tm.run_matrix(DEMO_MANIFEST, demo_trigger_rows(), agents=["codex"],
                               models=[model] if model else [None], runs_per_query=runs,
                               timeout=300, workers=1)
        tm.print_matrix(report["matrix"])
        incomplete = [r for r in report["results"] if not r["observation_complete"]]
        self.assertFalse(incomplete, f"broken runs (crash/timeout), not trigger signal: {incomplete}")
        self.assertTrue(any(r["triggered"] for r in report["results"]),
                        "no Codex run loaded the skill — detection, auth, or mounting is broken")


@unittest.skipUnless(os.environ.get("RUN_PI_TRIGGER_SMOKE") == "1",
                     "manual smoke: set RUN_PI_TRIGGER_SMOKE=1 (needs Pi CLI + credentials, spends tokens)")
class PiMatrixSmokeTests(unittest.TestCase):
    def test_pi_matrix_end_to_end(self):
        runs = int(os.environ.get("PI_TRIGGER_SMOKE_RUNS", "1"))
        model = os.environ.get("PI_TRIGGER_SMOKE_MODEL")
        report = tm.run_matrix(DEMO_MANIFEST, demo_trigger_rows(), agents=["pi"],
                               models=[model] if model else [None], runs_per_query=runs,
                               timeout=300, workers=1)
        tm.print_matrix(report["matrix"])
        incomplete = [r for r in report["results"] if not r["observation_complete"]]
        self.assertFalse(incomplete, f"broken runs (crash/timeout), not trigger signal: {incomplete}")
        self.assertTrue(any(r["triggered"] for r in report["results"]),
                        "no Pi run loaded the skill — detection, auth, or mounting is broken")


@unittest.skipUnless(os.environ.get("RUN_VIBE_TRIGGER_SMOKE") == "1",
                     "manual smoke: set RUN_VIBE_TRIGGER_SMOKE=1 (needs vibe CLI + MISTRAL_API_KEY, spends tokens)")
class VibeMatrixSmokeTests(unittest.TestCase):
    def test_vibe_matrix_end_to_end(self):
        runs = int(os.environ.get("VIBE_TRIGGER_SMOKE_RUNS", "1"))
        model = os.environ.get("VIBE_TRIGGER_SMOKE_MODEL")
        report = tm.run_matrix(DEMO_MANIFEST, demo_trigger_rows(), agents=["vibe"],
                               models=[model] if model else [None], runs_per_query=runs,
                               timeout=300, workers=1,
                               vibe_cmd=os.environ.get("VIBE_TRIGGER_SMOKE_CMD", tm.VIBE_DEFAULT_CMD))
        tm.print_matrix(report["matrix"])
        incomplete = [r for r in report["results"] if not r["observation_complete"]]
        self.assertFalse(incomplete, f"broken runs (crash/timeout), not trigger signal: {incomplete}")
        self.assertTrue(any(r["triggered"] for r in report["results"]),
                        "no Vibe run loaded the skill — detection, auth, or mounting is broken")


if __name__ == "__main__":
    unittest.main()
