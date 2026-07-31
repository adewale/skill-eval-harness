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
and opus; RUN_CODEX_TRIGGER_SMOKE=1, RUN_PI_TRIGGER_SMOKE=1,
RUN_VIBE_TRIGGER_SMOKE=1, and RUN_AGY_TRIGGER_SMOKE=1 run the same trigger
path for those adapters:

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
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from helpers import skill_markdown

import run_pi_trigger_eval as tr
import run_trigger_matrix as tm
import skill_benchmark as sb
from agent_capabilities import AGENT_CAPABILITIES
from trigger_contracts import (
    InvocationOutcome,
    InvocationState,
    TriggerExpectation,
    TriggerObservation,
    TriggerRepetitionIdentity,
)

ROOT = Path(__file__).resolve().parents[1]
DEMO_MANIFEST = ROOT / "examples" / "demo-skill" / "evals" / "shared-benchmark.json"
AGY_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "agy"


def agy_fixture(name: str) -> str:
    """One checked-in agy protocol shape, shared with the runner tests so the
    trigger and answer paths are held against the same bytes."""
    return (AGY_FIXTURES / name).read_text(encoding="utf-8")


def demo_trigger_rows():
    manifest = tm.load_manifest(DEMO_MANIFEST)
    return tm.cases_from_manifest(manifest, "tune")


def completed_invocation(stdout: str) -> InvocationOutcome:
    return InvocationOutcome.from_process(stdout=stdout, stderr="", returncode=0, elapsed_ms=1)


class TriggerRowBoundaryTests(unittest.TestCase):
    def test_eval_set_requires_real_boolean_should_trigger(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "rows.json"
            path.write_text(json.dumps([{"query": "review this", "should_trigger": "false"}]), encoding="utf-8")
            args = SimpleNamespace(eval_set=str(path), split="tune")
            with self.assertRaises(SystemExit) as ctx:
                tm.eval_rows_from_args(args, DEMO_MANIFEST)
        self.assertIn("should_trigger must be true or false", str(ctx.exception))

    def test_protocol_producers_reject_nonpositive_concurrency_limits(self):
        for field, mutation in (
            ("timeout_seconds", {"timeout": 0, "runs_per_query": 1, "workers": 1}),
            ("runs_per_query", {"timeout": 1, "runs_per_query": 0, "workers": 1}),
            ("workers", {"timeout": 1, "runs_per_query": 1, "workers": 0}),
            ("workers", {"timeout": 1, "runs_per_query": 1, "workers": False}),
        ):
            with self.subTest(producer="pi", field=field), \
                 self.assertRaisesRegex(ValueError, field):
                tr.pi_trigger_protocol(model=None, **mutation)
            with self.subTest(producer="matrix", field=field), \
                 self.assertRaisesRegex(ValueError, field):
                tm.trigger_protocol([], None, **mutation)

    def test_matrix_rejects_zero_workers_before_constructing_an_executor(self):
        with self.assertRaisesRegex(SystemExit, "workers must be a positive integer"):
            tm.run_matrix(
                DEMO_MANIFEST, demo_trigger_rows()[:1], agents=["stub"],
                models=[None], runs_per_query=1, timeout=30, workers=0,
            )

    def test_protocol_producers_reject_ambiguous_model_identities(self):
        for model in ("", "   ", False):
            with self.subTest(producer="pi", model=model), self.assertRaises(ValueError):
                tr.pi_trigger_protocol(
                    timeout=1, runs_per_query=1, workers=1, model=model)
            with self.subTest(producer="matrix", model=model), self.assertRaises(ValueError):
                tm.trigger_protocol(
                    [tm.AgentAdapter()], [model],
                    timeout=1, runs_per_query=1, workers=1)

    def test_eval_set_preserves_false_boolean(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "rows.json"
            path.write_text(json.dumps({"evals": [{"query": "hello", "should_trigger": False}]}), encoding="utf-8")
            args = SimpleNamespace(eval_set=str(path), split="tune")
            rows = tm.eval_rows_from_args(args, DEMO_MANIFEST)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["query"], "hello")
        self.assertIs(rows[0]["should_trigger"], False)
        self.assertRegex(rows[0]["query_id"], r"^query-[0-9a-f]{64}$")

    def test_duplicate_query_id_is_rejected_before_runs_are_scheduled(self):
        rows = [
            {"query_id": "same", "query": "one", "should_trigger": True},
            {"query_id": "same", "query": "two", "should_trigger": False},
        ]
        with self.assertRaisesRegex(SystemExit, "conflicting queries"):
            tr.validate_trigger_rows(rows, "fixture")

    def test_exact_duplicate_query_id_is_rejected_before_runs_are_scheduled(self):
        rows = [
            {"query_id": "same", "query": "one", "should_trigger": True},
            {"query_id": "same", "query": "one", "should_trigger": True},
        ]
        with self.assertRaisesRegex(SystemExit, "duplicate query_id"):
            tr.validate_trigger_rows(rows, "fixture")

    def test_distinct_ids_cannot_alias_the_same_authored_query(self):
        rows = [
            {"query_id": "first", "query": "one", "should_trigger": True},
            {"query_id": "second", "query": "one", "should_trigger": True},
        ]
        with self.assertRaisesRegex(SystemExit, "alias the same query"):
            tr.validate_trigger_rows(rows, "fixture")

    def test_cosmetic_query_variants_are_one_inference_identity(self):
        rows = [
            {"query_id": "first", "query": "Caf\u00e9   prompt", "should_trigger": True},
            {"query_id": "second", "query": "  CAFE\u0301 prompt\t", "should_trigger": True},
        ]
        with self.assertRaisesRegex(SystemExit, "alias the same query"):
            tr.validate_trigger_rows(rows, "fixture")

    def test_conflicting_id_aliases_are_rejected(self):
        rows = [{"id": "first", "query_id": "second",
                 "query": "one", "should_trigger": True}]
        with self.assertRaisesRegex(SystemExit, "conflicting query_id and id"):
            tr.validate_trigger_rows(rows, "fixture")

    def test_eval_set_rejects_evals_and_queries_aliases_together(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "rows.json"
            row = {"query": "one", "should_trigger": True}
            path.write_text(json.dumps({"evals": [row], "queries": [row]}), encoding="utf-8")
            args = SimpleNamespace(eval_set=str(path), split="tune")
            with self.assertRaisesRegex(SystemExit, "exactly one of evals or queries"):
                tr.eval_rows_from_args(args, DEMO_MANIFEST)

    def test_pi_trigger_runner_invokes_pi_from_isolated_workspace(self):
        seen = {}

        def fake_run(argv, *, cwd, env, timeout):
            seen.update({"argv": argv, "cwd": str(cwd), "config_dir": env["PI_CODING_AGENT_DIR"], "timeout": timeout})
            return InvocationOutcome.from_process(
                stdout=json.dumps({"type": "agent_end", "messages": [{"stopReason": "stop"}]}) + "\n",
                stderr="", returncode=0, elapsed_ms=1,
            )

        with mock.patch.object(tr, "invoke_argv_with_timeout", side_effect=fake_run):
            result = tr.run_query(DEMO_MANIFEST, "ordinary chat", False, 12, None)
        self.assertTrue(result["pass"])
        self.assertEqual(seen["cwd"], seen["config_dir"])
        self.assertIn("pi-trigger-", seen["cwd"])
        self.assertNotEqual(Path(seen["cwd"]).resolve(), ROOT.resolve())

    def test_pi_ablation_row_names_edited_tree_and_repetition(self):
        def fake_run(*args, **kwargs):
            return InvocationOutcome.from_process(
                stdout=json.dumps({
                    "type": "agent_end",
                    "messages": [{"role": "assistant", "stopReason": "stop"}],
                }) + "\n",
                stderr="", returncode=0, elapsed_ms=1,
            )

        identity = TriggerRepetitionIdentity("pi-query", 2)
        with mock.patch.object(tr, "invoke_argv_with_timeout", side_effect=fake_run):
            result = tr.run_query(
                DEMO_MANIFEST, "ordinary chat", False, 12, None,
                ablation="weaker-description", identity=identity)
        self.assertEqual(result["skill_tree_hash"], result["ablation"]["skill_hash"])
        self.assertNotEqual(result["skill_tree_hash"],
                            result["ablation"]["parent_skill_hash"])
        self.assertEqual((result["query_id"], result["run_number"]),
                         ("pi-query", 2))

    def test_pi_main_reports_are_accepted_by_trigger_comparer(self):
        terminal = InvocationOutcome.from_process(
            stdout=json.dumps({
                "type": "agent_end",
                "messages": [{"role": "assistant", "stopReason": "stop"}],
            }) + "\n",
            stderr="", returncode=0, elapsed_ms=1,
        )
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            eval_set = root / "rows.json"
            eval_set.write_text(json.dumps([
                {"query_id": "negative", "query": "ordinary chat",
                 "should_trigger": False},
            ]), encoding="utf-8")
            reports = []
            for ablation in (None, "weaker-description"):
                out = root / ("ablation.json" if ablation else "baseline.json")
                argv = ["skill-pi-trigger-eval", str(DEMO_MANIFEST),
                        "--eval-set", str(eval_set), "--runs-per-query", "1",
                        "--workers", "1", "--timeout", "12", "--out", str(out)]
                if ablation:
                    argv.extend(["--ablation", ablation])
                with mock.patch.object(sys, "argv", argv), \
                     mock.patch.object(tr, "invoke_argv_with_timeout", return_value=terminal), \
                     mock.patch("builtins.print"):
                    self.assertEqual(tr.main(), 0)
                reports.append(json.loads(out.read_text(encoding="utf-8")))
        compared = sb.build_trigger_comparison(reports[0], reports[1])
        self.assertTrue(compared["provenance"]["verified"])
        self.assertEqual(compared["paired"]["blocked"], [])

    def test_pi_main_excludes_incomplete_runs_from_pass_rate_denominator(self):
        timed_out = InvocationOutcome.from_process(
            stdout=json.dumps({"type": "agent_start"}) + "\n",
            stderr="timeout", returncode=124, elapsed_ms=1,
        )
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            eval_set = root / "rows.json"
            out = root / "report.json"
            eval_set.write_text(json.dumps([
                {"query_id": "negative", "query": "ordinary chat",
                 "should_trigger": False},
            ]), encoding="utf-8")
            argv = ["skill-pi-trigger-eval", str(DEMO_MANIFEST),
                    "--eval-set", str(eval_set), "--runs-per-query", "1",
                    "--workers", "1", "--timeout", "1", "--out", str(out)]
            with mock.patch.object(sys, "argv", argv), \
                 mock.patch.object(tr, "invoke_argv_with_timeout", return_value=timed_out), \
                 mock.patch("builtins.print"):
                self.assertEqual(tr.main(), 1)
            summary = json.loads(out.read_text(encoding="utf-8"))["summary"]
        self.assertEqual(summary["measurement_status"], "incomplete")
        self.assertEqual(
            (summary["complete"], summary["incomplete"], summary["total"]),
            (0, 1, 1),
        )
        self.assertNotIn("pass_rate", summary)
        self.assertNotIn("observed_pass_rate", summary)


    def test_pi_json_provider_error_cannot_pass_a_negative_trigger(self):
        provider_error = json.dumps({
            "type": "agent_end", "willRetry": False,
            "messages": [{"role": "assistant", "content": [], "stopReason": "error",
                          "errorMessage": "Mistral API error (400): Invalid model",
                          "usage": {"input": 7, "output": 2, "totalTokens": 9,
                                    "cost": {"total": 0.009}}}],
        })

        def failed_provider(*args, **kwargs):
            return InvocationOutcome.from_process(
                stdout=provider_error + "\n", stderr="", returncode=0, elapsed_ms=1,
            )

        with tempfile.TemporaryDirectory() as td, \
             mock.patch.object(tr, "invoke_argv_with_timeout", side_effect=failed_provider), \
             mock.patch.object(tr.PiStream, "parse", wraps=tr.PiStream.parse) as parse_stream:
            trace_dir = Path(td) / "trace"
            result = tr.run_query(DEMO_MANIFEST, "ordinary chat", False, 12, None, trace_dir=trace_dir)
            artifacts = [
                json.loads((trace_dir / name).read_text(encoding="utf-8"))
                for name in ("metrics.json", "metadata.json")
            ]
        self.assertEqual(parse_stream.call_count, 1)
        self.assertFalse(result["observation_complete"])
        self.assertIsNone(result["pass"])
        self.assertIsNone(result["triggered"])
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
        self.assertFalse(result.observation_complete)
        self.assertEqual(result.provider_error, "provider rejected model")

    def test_pi_matrix_detection_and_telemetry_share_one_parsed_stream(self):
        def successful_pi(*args, cwd, **kwargs):
            skill = Path(cwd) / ".pi-config" / "skills" / "demo" / "SKILL.md"
            assistant = {"role": "assistant", "stopReason": "stop",
                         "usage": {"input": 4, "output": 1, "totalTokens": 5}}
            stdout = "\n".join([
                json.dumps({"type": "tool_execution_start", "toolName": "read", "args": {"path": str(skill)}}),
                json.dumps({"type": "tool_execution_end", "toolName": "read", "args": {"path": str(skill)}, "result": "ok"}),
                json.dumps({"type": "agent_end", "messages": [assistant]}),
            ]) + "\n"
            return InvocationOutcome.from_process(
                stdout=stdout, stderr="", returncode=0, elapsed_ms=1,
            )

        with tempfile.TemporaryDirectory() as td, \
             mock.patch.object(tm.PiAdapter, "_run_argv", staticmethod(successful_pi)), \
             mock.patch.object(tm.PiStream, "parse", wraps=tm.PiStream.parse) as parse_stream:
            tree = Path(td) / "tree"
            skill = tree / "demo"
            skill.mkdir(parents=True)
            (skill / "SKILL.md").write_text("---\nname: demo\n---\n", encoding="utf-8")
            row = tm.run_cell_query(
                tm.PiAdapter(), tree, "review this", True, None, 12,
                metadata={"skill_tree_hash": sb.skill_tree_hash(tree)},
            )
        self.assertEqual(parse_stream.call_count, 1)
        self.assertTrue(row["triggered"])
        self.assertEqual(row["usage_normalized"]["total_tokens"], 5)

    def test_pi_timeout_with_parseable_partial_trace_is_not_telemetry_complete(self):
        def timed_out(*args, **kwargs):
            return InvocationOutcome.from_process(
                stdout=json.dumps({"type": "command", "command": "partial"}) + "\n",
                stderr="timeout", returncode=124, elapsed_ms=10,
            )

        with tempfile.TemporaryDirectory() as td, mock.patch.object(tr, "invoke_argv_with_timeout", side_effect=timed_out):
            trace_dir = Path(td) / "trace"
            tr.run_query(DEMO_MANIFEST, "ordinary chat", False, 1, None, trace_dir=trace_dir)
            meta = json.loads((trace_dir / "metadata.json").read_text(encoding="utf-8"))
        self.assertFalse(meta["observation_complete"])
        self.assertEqual(meta["telemetry"]["measurements"]["commands"]["availability"], "unavailable")


class StubMatrixOfflineTests(unittest.TestCase):
    def test_every_matrix_adapter_has_an_explicit_trace_dialect(self):
        self.assertLessEqual(set(tm.ADAPTERS), set(sb.TRACE_DIALECTS))

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
        for row in report["results"]:
            self.assertEqual(row["usage_normalized"], {"source": "not_applicable"})
            self.assertEqual(row["cost_normalized"], {"source": "not_applicable"})
            self.assertIsInstance(row["elapsed_ms"], int)

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
        self.assertEqual(tree_hash, provenance["skill_hash"])
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
        self.assertEqual(report["summary"]["complete"], 0)
        self.assertEqual(report["summary"]["incomplete"], 1)
        self.assertEqual(report["summary"]["passed"], 0)
        self.assertNotIn("pass_rate", report["summary"])
        self.assertEqual(report["matrix"][0]["summary"]["incomplete_observations"], 1)
        self.assertEqual(report["matrix"][0]["queries"][0]["complete"], 0)
        self.assertNotIn("trigger_rate", report["matrix"][0]["queries"][0])
        self.assertIsNone(report["results"][0]["pass"])
        self.assertIsNone(report["results"][0]["triggered"])
        self.assertIn("disk full", report["results"][0]["error"])

        with mock.patch("sys.stdout") as stdout:
            tm.print_matrix(report["matrix"])
        rendered = " ".join(
            str(call.args[0]) for call in stdout.write.call_args_list if call.args
        )
        self.assertIn("INCOMPLETE", rendered)

    def test_trace_redacts_workspace_credentials(self):
        class SecretEchoAdapter(tm.AgentAdapter):
            name = "generic"

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
            row = tm.run_cell_query(
                SecretEchoAdapter(), tree, "q", True, None, 12, trace_dir,
                metadata={"skill_tree_hash": sb.skill_tree_hash(tree)},
            )
            trace_text = (trace_dir / "trace.jsonl").read_text(encoding="utf-8")
            metadata_text = (trace_dir / "metadata.json").read_text(encoding="utf-8")
            metrics_text = (trace_dir / "metrics.json").read_text(encoding="utf-8")
        self.assertFalse(row["triggered"])
        self.assertNotIn("SECRET-TOKEN-12345", trace_text)
        self.assertNotIn("SECRET-TOKEN-12345", json.dumps(row))
        self.assertNotIn("SECRET-TOKEN-12345", metadata_text)
        self.assertNotIn("SECRET-TOKEN-12345", metrics_text)
        self.assertIn("[REDACTED]", trace_text)

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


class TriggerCliStatusTests(unittest.TestCase):
    def test_matrix_cli_exits_nonzero_for_an_incomplete_report(self):
        report = {
            "summary": {"measurement_status": "incomplete"},
            "matrix": [],
            "results": [],
        }
        with tempfile.TemporaryDirectory() as td, \
             mock.patch.object(tm, "run_matrix", return_value=report), \
             mock.patch.object(tm, "print_matrix"), \
             mock.patch.object(sys, "argv", [
                 "skill-trigger-matrix", str(DEMO_MANIFEST), "--agent", "stub",
                 "--out", str(Path(td) / "report.json"),
             ]):
            self.assertEqual(tm.main(), 1)

    def test_pi_cli_exits_nonzero_for_incomplete_observations(self):
        failed = TriggerObservation.harness_failure(
            agent="pi",
            model=None,
            query="q",
            expectation=TriggerExpectation.TRIGGER,
            error=RuntimeError("provider unavailable"),
            metadata={"skill_tree_hash": "sha256:test"},
        )
        with tempfile.TemporaryDirectory() as td, \
             mock.patch.object(tr, "observe_query", return_value=failed), \
             mock.patch.object(sys, "argv", [
                 "skill-pi-trigger-eval", str(DEMO_MANIFEST), "--runs-per-query", "1",
                 "--out", str(Path(td) / "report.json"),
             ]):
            self.assertEqual(tr.main(), 1)


class ClaudeDetectionTests(unittest.TestCase):
    """Canned claude -p stream-json fragments; no subprocess."""

    def _adapter(self):
        return tm.ClaudeAdapter()

    def test_skill_tool_use_by_name_is_trigger_evidence(self):
        stream = json.dumps({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "Skill", "input": {"skill": "demo-reviewer", "args": "..."}}]}})
        detection = self._adapter().detect(completed_invocation(stream), ["demo-reviewer"], [])
        self.assertTrue(detection.triggered)
        self.assertIn("Skill tool invoked: demo-reviewer", detection.legacy_evidence)

    def test_other_skills_and_plain_answers_are_not_evidence(self):
        stream = "\n".join([
            json.dumps({"type": "assistant", "message": {"content": [
                {"type": "tool_use", "name": "Skill", "input": {"skill": "code-review"}}]}}),
            json.dumps({"type": "assistant", "message": {"content": [
                {"type": "text", "text": "I would use the demo-reviewer skill here."}]}}),
        ])
        detection = self._adapter().detect(completed_invocation(stream), ["demo-reviewer"], [])
        self.assertFalse(detection.triggered, "a different skill firing, or the name in prose, is not load evidence")

    def test_reading_the_mounted_skill_md_is_fallback_evidence(self):
        mounted = Path("/tmp/trigger-x/.claude/skills/demo-reviewer/SKILL.md")
        stream = "\n".join([
            json.dumps({"type": "assistant", "message": {"content": [
                {"type": "tool_use", "id": "read-1", "name": "Read",
                 "input": {"file_path": str(mounted)}}]}}),
            json.dumps({"type": "user", "message": {"content": [
                {"type": "tool_result", "tool_use_id": "read-1", "content": "ok"}]}}),
            json.dumps({"type": "result", "subtype": "success", "result": "done"}),
        ])
        detection = self._adapter().detect(completed_invocation(stream), ["demo-reviewer"], [mounted])
        self.assertTrue(detection.triggered)

    def test_max_turns_is_a_completed_observation_window(self):
        self.assertEqual(tm.ClaudeAdapter._result_subtype(
            json.dumps({"type": "result", "subtype": "error_max_turns"})), "error_max_turns")

    def test_max_turns_subtype_does_not_reclassify_timeout_or_spawn_failure(self):
        stdout = json.dumps({"type": "result", "subtype": "error_max_turns"})
        for returncode, state in ((124, InvocationState.TIMED_OUT), (127, InvocationState.SPAWN_FAILED)):
            def fake_run(*args, _returncode=returncode, **kwargs):
                return InvocationOutcome.from_process(
                    stdout=stdout, stderr="failure", returncode=_returncode, elapsed_ms=3,
                )

            with self.subTest(returncode=returncode), \
                 tempfile.TemporaryDirectory() as td, \
                 mock.patch.object(tm.ClaudeAdapter, "_run_argv", staticmethod(fake_run)), \
                 mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}, clear=True):
                result = tm.ClaudeAdapter().invoke("q", "haiku", Path(td), 1)
            self.assertIs(result.state, state)

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
            return {"stdout": json.dumps({"type": "result", "subtype": "success"}) + "\n",
                    "stderr": "", "returncode": 0, "timed_out": False,
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
        self.assertTrue(result.metadata["config_isolated"])
        self.assertNotIn("config_isolation_warning", result.metadata)

    def test_claude_invoke_preserves_nonportable_oauth_config(self):
        seen = {}

        def fake_run(argv, *, cwd, env, timeout):
            seen["config_dir"] = env.get("CLAUDE_CONFIG_DIR")
            return {"stdout": json.dumps({"type": "result", "subtype": "success"}) + "\n",
                    "stderr": "", "returncode": 0, "timed_out": False,
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
        self.assertFalse(result.metadata["config_isolated"])
        self.assertIn("personal config may influence", result.metadata["config_isolation_warning"])

    def test_claude_malformed_stream_is_not_a_valid_negative_observation(self):
        def fake_run(*args, **kwargs):
            return InvocationOutcome.from_process(
                stdout="\n".join([
                    json.dumps({
                        "type": "assistant",
                        "message": {"content": {"type": "tool_use", "name": "Skill"}},
                    }),
                    json.dumps({"type": "result", "subtype": "success"}),
                ]) + "\n",
                stderr="", returncode=0, elapsed_ms=1,
            )

        with tempfile.TemporaryDirectory() as td, \
             mock.patch.object(tm.ClaudeAdapter, "_run_argv", staticmethod(fake_run)), \
             mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}, clear=True):
            result = tm.ClaudeAdapter().invoke("q", "haiku", Path(td), 1)
        self.assertIs(result.state, InvocationState.PROVIDER_FAILED)
        self.assertIn("protocol error", result.provider_error or "")


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
        detection = tm.CodexAdapter().detect(completed_invocation(stream), ["demo-reviewer"], [mounted])
        self.assertFalse(detection.triggered)
        self.assertFalse(detection.evidence)
        prose = json.dumps({"type": "message", "content": "I would use demo-reviewer."})
        self.assertFalse(tm.CodexAdapter().detect(completed_invocation(prose), ["demo-reviewer"], [mounted]).triggered)

    def test_codex_malformed_stream_is_not_a_valid_negative_observation(self):
        def fake_run(*args, **kwargs):
            return InvocationOutcome.from_process(
                stdout="not-json\n", stderr="", returncode=0, elapsed_ms=1,
            )
        with tempfile.TemporaryDirectory() as td, \
             mock.patch.object(tm.CodexAdapter, "_run_argv", staticmethod(fake_run)):
            workspace = Path(td) / "workspace"
            workspace.mkdir()
            result = tm.CodexAdapter().invoke("q", None, workspace, 1)
        self.assertIs(result.state, InvocationState.PROVIDER_FAILED)

    def test_codex_parseable_but_unterminated_stream_is_not_a_valid_negative_observation(self):
        def fake_run(*args, **kwargs):
            return InvocationOutcome.from_process(
                stdout="{}\n", stderr="", returncode=0, elapsed_ms=1,
            )
        with tempfile.TemporaryDirectory() as td, \
             mock.patch.object(tm.CodexAdapter, "_run_argv", staticmethod(fake_run)):
            workspace = Path(td) / "workspace"
            workspace.mkdir()
            result = tm.CodexAdapter().invoke("q", None, workspace, 1)
        self.assertIs(result.state, InvocationState.PROVIDER_FAILED)

    def test_codex_invoke_appends_raw_query_model_and_external_skill_dir(self):
        seen = {}

        def fake_run(argv, *, cwd, env, timeout):
            seen.update({"argv": argv, "cwd": cwd, "env": env, "timeout": timeout})
            return {"stdout": '{"type":"turn.completed"}\n', "stderr": "", "returncode": 0, "timed_out": False,
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
        self.assertTrue(result.metadata["codex_home_outside_workdir"])
        self.assertIs(tm.CodexAdapter()._run_argv, tm.invoke_argv_with_timeout)

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
            return {"stdout": '{"type":"turn.completed"}\n', "stderr": "", "returncode": 0, "timed_out": False,
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
        self.assertTrue(result.metadata["codex_home_outside_workdir"])
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
                        "timed_out": False, "elapsed_ms": 1, "observation_complete": True,
                        "debug": {"auth": secret}}

        with tempfile.TemporaryDirectory() as td, mock.patch.dict(os.environ, {"MISTRAL_API_KEY": "ambient-secret-token"}):
            tree = Path(td) / "tree"
            skill = tree / "demo"
            skill.mkdir(parents=True)
            (skill / "SKILL.md").write_text("---\nname: demo\n---\n", encoding="utf-8")
            row = tm.run_cell_query(
                LeakyAdapter(), tree, "q", False, None, 12,
                trace_dir=Path(td) / "trace",
                metadata={"skill_tree_hash": sb.skill_tree_hash(tree),
                          "external": {"token": "ambient-secret-token"}},
            )
            trace_text = (Path(row["trace_dir"]) / "trace.jsonl").read_text(encoding="utf-8")
            trace_metadata = json.loads((Path(row["trace_dir"]) / "metadata.json").read_text(encoding="utf-8"))
        self.assertNotIn("ambient-secret-token", row["stderr"])
        self.assertEqual(row["stderr"], "err [REDACTED]")
        self.assertEqual(row["debug"], {"auth": "[REDACTED]"})
        self.assertEqual(row["external"], {"token": "[REDACTED]"})
        self.assertEqual(trace_metadata["debug"], {"auth": "[REDACTED]"})
        self.assertEqual(trace_metadata["external"], {"token": "[REDACTED]"})
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
                tm.run_cell_query(
                    BrokenAdapter(), tree, "q", True, None, 12,
                    metadata={"skill_tree_hash": sb.skill_tree_hash(tree)},
                )
        self.assertIn("observation_complete", str(ctx.exception))

    def test_run_cell_query_rejects_mount_bytes_that_differ_from_scheduled_tree(self):
        class MutatingAdapter(tm.AgentAdapter):
            name = "stub"

            def mount(self, tree_dir, workspace):
                copied = self._mount_tree(tree_dir, workspace / "skills")
                copied[0].write_text("mutated after scheduling\n", encoding="utf-8")
                return copied

            def invoke(self, query, model, workspace, timeout):
                raise AssertionError("mismatched mounted bytes must fail before invocation")

        with tempfile.TemporaryDirectory() as td:
            tree = Path(td) / "tree"
            skill = tree / "demo"
            skill.mkdir(parents=True)
            (skill / "SKILL.md").write_text("---\nname: demo\n---\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "mounted skill tree hash"):
                tm.run_cell_query(
                    MutatingAdapter(), tree, "q", True, None, 12,
                    metadata={"skill_tree_hash": sb.skill_tree_hash(tree)},
                )

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

    def test_interpreter_wrapper_identity_binds_script_bytes(self):
        with tempfile.TemporaryDirectory() as td:
            script = Path(td) / "wrapper.py"
            script.write_text("print('first')\n", encoding="utf-8")
            first = tm.executable_identity(f"{sys.executable} {script}")
            script.write_text("print('second')\n", encoding="utf-8")
            second = tm.executable_identity(f"{sys.executable} {script}")
        self.assertNotEqual(first, second)
        self.assertIn(str(script.resolve()), first["argument_files"])


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
        stream = "\n".join([
            json.dumps({"role": "assistant", "tool_calls": [{
                "id": "call-1", "function": {"name": "skill",
                "arguments": json.dumps({"name": "demo-reviewer"})}}]}),
            json.dumps({"role": "tool", "tool_call_id": "call-1", "content": "loaded"}),
            json.dumps({"role": "assistant", "content": "done"}),
        ])
        detection = tm.VibeAdapter().detect(completed_invocation(stream), ["demo-reviewer"], [])
        self.assertTrue(detection.triggered)
        self.assertIn("Vibe skill tool invoked: demo-reviewer", detection.legacy_evidence)
        other = json.dumps({"role": "assistant", "tool_calls": [{"function": {"name": "skill", "arguments": json.dumps({"name": "other"})}}]})
        self.assertFalse(tm.VibeAdapter().detect(completed_invocation(other), ["demo-reviewer"], []).triggered)

    def test_vibe_malformed_stream_is_not_a_valid_negative_observation(self):
        def fake_run(*args, **kwargs):
            return InvocationOutcome.from_process(
                stdout="not-json\n", stderr="", returncode=0, elapsed_ms=1,
            )
        with tempfile.TemporaryDirectory() as td, \
             mock.patch.object(tm.VibeAdapter, "_run_argv", staticmethod(fake_run)):
            workspace = Path(td) / "workspace"
            workspace.mkdir()
            result = tm.VibeAdapter().invoke("q", None, workspace, 1)
        self.assertIs(result.state, InvocationState.PROVIDER_FAILED)

    def test_vibe_parseable_but_answerless_stream_is_not_a_valid_negative_observation(self):
        def fake_run(*args, **kwargs):
            return InvocationOutcome.from_process(
                stdout="{}\n", stderr="", returncode=0, elapsed_ms=1,
            )
        with tempfile.TemporaryDirectory() as td, \
             mock.patch.object(tm.VibeAdapter, "_run_argv", staticmethod(fake_run)):
            workspace = Path(td) / "workspace"
            workspace.mkdir()
            result = tm.VibeAdapter().invoke("q", None, workspace, 1)
        self.assertIs(result.state, InvocationState.PROVIDER_FAILED)

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
        self.assertTrue(result.metadata["config_isolated"])
        self.assertTrue(result.metadata["vibe_home_outside_workdir"])


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
                    agy_cmd=os.environ.get("AGY_INVOKE_SMOKE_CMD", tm.AGY_DEFAULT_CMD),
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
                        "returncode": result.returncode,
                        "timed_out": result.timed_out,
                        "observation_complete": result.observation_complete,
                        "elapsed_ms": result.elapsed_ms,
                        "stdout_bytes": len(result.stdout),
                        "stdout_tail": result.stdout[-300:],
                        "stderr_tail": result.stderr[-300:],
                        "mounted_paths": len(copied),
                    })
                    ok = (
                        not result.timed_out and
                        result.returncode == 0 and
                        result.observation_complete and
                        bool(result.stdout.strip())
                    )
                    row["ok"] = ok
                    if not ok:
                        failures.append(
                            f"{agent_name}/{model_label}: returncode={result.returncode} "
                            f"timed_out={result.timed_out} observation_complete={result.observation_complete} "
                            f"stdout_bytes={len(result.stdout)} "
                            f"stdout_tail={result.stdout[-300:]!r} "
                            f"stderr_tail={result.stderr[-300:]!r}"
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
            "AGY_INVOKE_SMOKE_MODEL": "",
        }
        with mock.patch.dict(os.environ, clean_env, clear=False):
            for key in clean_env:
                os.environ.pop(key, None)
            agents = _live_invoke_smoke_agents()
            models = {
                name: _live_invoke_smoke_models(name, tm.adapter_instance(name))
                for name in agents
            }
        self.assertEqual(agents, ["claude", "codex", "pi", "vibe", "agy"])
        self.assertEqual(models["claude"], ["haiku", "sonnet", "opus"])
        self.assertEqual(models["codex"], [None])
        self.assertEqual(models["pi"], [None])
        self.assertEqual(models["vibe"], [None])
        self.assertEqual(models["agy"], [None])

    def test_advertised_live_smoke_envs_are_consumed_by_tests(self):
        # Trigger smokes live here; the Jetty answer-path smoke has its own
        # module. An advertised env var must gate a real test somewhere.
        sources = [Path(__file__), Path(__file__).with_name("test_smoke_jetty.py")]
        test_source = "".join(p.read_text(encoding="utf-8") for p in sources)
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


def trigger_row(query, should, *, triggered, complete=True, agent="stub", model=None,
                query_id=None, run_number=1):
    """One persisted trigger-matrix result row, valid under
    TriggerObservation.from_row's contract."""
    evidence = ["skills/demo/SKILL.md"] if (complete and triggered) else []
    return {
        "population": "trigger", "agent": agent, "model": model, "query": query,
        "query_id": query_id or query, "run_number": run_number,
        "should_trigger": should, "triggered": complete and triggered,
        "pass": complete and (triggered == should),
        "observation_complete": complete,
        "returncode": 0 if complete else 124, "timed_out": not complete,
        "elapsed_ms": 5, "completion_evidence": "normal_exit" if complete else None,
        "evidence": evidence,
        "evidence_typed": [{"kind": "mounted_path", "text": t} for t in evidence],
        "protocol_observation": {},
        "usage_normalized": {"source": "missing"}, "cost_normalized": {"source": "missing"},
        "stderr": "",
    }


BASE_HASH = "sha256:base-revision"
EDIT_HASH = "sha256:edited-revision"
ABLATION_PROVENANCE = {
    "id": "drop-description", "mode": "materialized", "population": "trigger",
    "skill_hash": EDIT_HASH, "parent_skill_hash": BASE_HASH,
    "components": [{"class": "discovery", "mechanism": "frontmatter_field",
                    "skill_root": "skills/demo", "target": {"field": "description"}}],
}

TRIGGER_MANIFEST = {
    "skill_name": "demo",
    "skill_paths": ["skills/demo"],
    "ablations": [{
        "id": "drop-description", "population": "trigger",
        "components": [{"class": "discovery", "mechanism": "frontmatter_field",
                        "skill_root": "skills/demo", "target": {"field": "description"}}],
    }],
}


def trigger_report(rows, *, ablation=None, provenance=None, tree_hash=BASE_HASH,
                   runs_per_query=2):
    design = []
    seen = set()
    for row in rows:
        key = (row["agent"], row["model"], row["query_id"])
        if key not in seen:
            design.append({k: row[k] for k in (
                "agent", "model", "query_id", "query", "should_trigger")})
            seen.add(key)
    adapter_models = {}
    for row in rows:
        adapter_models.setdefault(row["agent"], [])
        if row["model"] not in adapter_models[row["agent"]]:
            adapter_models[row["agent"]].append(row["model"])
    protocol = {
        "schema_version": 1, "producer": "skill-trigger-matrix",
        "harness_identity": sb.trigger_harness_identity(),
        "timeout_seconds": 30, "runs_per_query": runs_per_query, "workers": 1,
        "adapters": [
            {"adapter": f"run_trigger_matrix.{agent.title()}Adapter", "agent": agent,
             "trace_dialect": agent,
             "implementation_sha256": "sha256:" + ("0" * 64),
             "producer_sha256": "sha256:" + ("1" * 64),
             "required_observations": {}, "models": models}
            for agent, models in sorted(adapter_models.items())
        ],
    }
    protocol_sha256 = sb.canonical_json_sha256(protocol)
    rows = [{**row, "skill_tree_hash": tree_hash,
             "protocol_sha256": protocol_sha256,
             "protocol_observation": row.get("protocol_observation", {})}
            for row in rows]
    return {"skill_name": "demo", "generated_at": 1,
            "evidence_class": tm.TRIGGER_MEASUREMENT_EVIDENCE_CLASS,
            "skill_tree_hash": tree_hash, "ablation": ablation,
            "provenance": provenance if provenance is not None else {"mode": "baseline", "skill_tree_hash": tree_hash},
            "manifest_identity": sb.trigger_manifest_identity(TRIGGER_MANIFEST),
            "protocol": protocol, "protocol_sha256": protocol_sha256,
            "runs_per_query": runs_per_query, "design": design, "results": rows}


class TriggerComparisonTests(unittest.TestCase):
    """build_trigger_comparison pairs a baseline matrix run with an --ablation
    run of the SAME canonical revision, mirroring the answer path's
    causal-confirmation gate: provenance verified + coverage + an observed,
    sign-flip-significant pass-rate drop across queries."""

    QUERIES = [f"query {n}" for n in range(1, 7)]   # 6 paired deltas: exact p ~= 0.031

    def _baseline_rows(self):
        return [trigger_row(q, True, triggered=True, run_number=run_number)
                for q in self.QUERIES for run_number in range(1, 3)]

    def _ablation_rows(self, *, triggered=False):
        return [trigger_row(q, True, triggered=triggered, run_number=run_number)
                for q in self.QUERIES for run_number in range(1, 3)]

    def _compare(self, base_rows=None, abl_rows=None, *, provenance=None,
                 abl_hash=EDIT_HASH, base_runs_per_query=2,
                 abl_runs_per_query=None):
        baseline = trigger_report(
            base_rows if base_rows is not None else self._baseline_rows(),
            runs_per_query=base_runs_per_query)
        ablation = trigger_report(abl_rows if abl_rows is not None else self._ablation_rows(),
                                  ablation="drop-description",
                                  provenance=provenance if provenance is not None else ABLATION_PROVENANCE,
                                  tree_hash=abl_hash,
                                  runs_per_query=(abl_runs_per_query
                                                  if abl_runs_per_query is not None
                                                  else base_runs_per_query))
        return sb.build_trigger_comparison(baseline, ablation)

    def test_verified_significant_drop_confirms_causal(self):
        out = self._compare()
        self.assertEqual(out["population"], "trigger")
        self.assertEqual(out["evidence_class"], "confirmed_causal")
        self.assertTrue(out["provenance"]["verified"])
        self.assertEqual(out["summary"]["comparable"], 6)
        self.assertEqual(len(out["regressed_queries"]), 6)
        self.assertTrue(out["paired"]["significance"]["significant_at_0_05"])
        self.assertEqual(out["paired"]["comparable_queries"][0]["pass_delta"], -1.0)

    def test_comparer_keeps_sub_millionth_rate_deltas(self):
        epsilon = 1 / 3_000_000
        pass_rates = iter(
            value for _ in self.QUERIES for value in (1.0, 1.0 - epsilon)
        )
        trigger_rates = iter(
            value for _ in self.QUERIES for value in (1.0, 1.0 - epsilon)
        )
        with mock.patch.object(
            sb.CompleteTriggerCohort, "pass_rate",
            new_callable=mock.PropertyMock, side_effect=pass_rates,
        ) as pass_rate, mock.patch.object(
            sb.CompleteTriggerCohort, "trigger_rate",
            new_callable=mock.PropertyMock, side_effect=trigger_rates,
        ) as trigger_rate:
            out = self._compare()
        self.assertEqual(pass_rate.call_count, 2 * len(self.QUERIES))
        self.assertEqual(trigger_rate.call_count, 2 * len(self.QUERIES))
        self.assertLess(out["paired"]["comparable_queries"][0]["pass_delta"], 0)
        self.assertAlmostEqual(
            out["paired"]["comparable_queries"][0]["pass_delta"], -epsilon)
        self.assertLess(out["summary"]["mean_pass_delta"], 0)

    def test_no_drop_is_refuted(self):
        out = self._compare(abl_rows=self._ablation_rows(triggered=True))
        self.assertEqual(out["evidence_class"], "refuted")
        self.assertEqual(out["regressed_queries"], [])

    def test_observed_but_insignificant_drop_is_indeterminate(self):
        # one regressed query out of six cannot clear the sign-flip bar
        abl = [trigger_row(q, True, triggered=(q != "query 1"), run_number=run_number)
               for q in self.QUERIES for run_number in range(1, 3)]
        out = self._compare(abl_rows=abl)
        self.assertEqual(out["evidence_class"], "indeterminate")
        self.assertEqual(len(out["regressed_queries"]), 1)
        self.assertIn("not significant", out["note"])

    def test_significant_change_in_wrong_direction_is_refuted(self):
        queries = [f"direction {n}" for n in range(10)]
        baseline = []
        ablation = []
        for i, query in enumerate(queries):
            # One regression, nine improvements: the old two-sided gate called
            # this confirmed merely because at least one cell was negative.
            baseline.append(trigger_row(query, True, triggered=(i == 0)))
            ablation.append(trigger_row(query, True, triggered=(i != 0)))
        out = self._compare(base_rows=baseline, abl_rows=ablation,
                            base_runs_per_query=1)
        self.assertTrue(out["paired"]["significance"]["significant_at_0_05"])
        self.assertGreater(out["summary"]["mean_pass_delta"], 0)
        self.assertEqual(out["evidence_class"], "refuted")
        self.assertIn("aggregate mean pass delta is non-negative", out["note"])
        self.assertNotIn("not significant", out["note"])

    def test_models_do_not_multiply_one_query_into_six_units(self):
        baseline = [trigger_row("one query", True, triggered=True, model=f"m{n}")
                    for n in range(6)]
        ablation = [trigger_row("one query", True, triggered=False, model=f"m{n}")
                    for n in range(6)]
        out = self._compare(base_rows=baseline, abl_rows=ablation,
                            base_runs_per_query=1)
        self.assertEqual(out["summary"]["comparable_cells"], 6)
        self.assertEqual(out["paired"]["significance"]["n"], 1)
        self.assertEqual(out["evidence_class"], "indeterminate")

    def test_revision_mismatch_is_indeterminate_with_reason(self):
        provenance = {**ABLATION_PROVENANCE, "parent_skill_hash": "sha256:other-revision"}
        out = self._compare(provenance=provenance)
        self.assertEqual(out["evidence_class"], "indeterminate")
        self.assertFalse(out["provenance"]["verified"])
        self.assertTrue(any("different skill revision" in r for r in out["provenance"]["reasons"]))

    def test_baseline_provenance_must_attest_its_top_level_hash(self):
        baseline = trigger_report(self._baseline_rows())
        baseline["provenance"]["skill_tree_hash"] = "sha256:other"
        ablation = trigger_report(self._ablation_rows(), ablation="drop-description",
                                  provenance=ABLATION_PROVENANCE, tree_hash=EDIT_HASH)
        out = sb.build_trigger_comparison(baseline, ablation)
        self.assertEqual(out["evidence_class"], "indeterminate")
        self.assertTrue(any("baseline provenance" in reason
                            for reason in out["provenance"]["reasons"]))

    def test_skill_name_mismatch_is_indeterminate(self):
        baseline = trigger_report(self._baseline_rows())
        ablation = trigger_report(self._ablation_rows(), ablation="drop-description",
                                  provenance=ABLATION_PROVENANCE, tree_hash=EDIT_HASH)
        ablation["skill_name"] = "other"
        ablation["manifest_identity"] = sb.trigger_manifest_identity(
            {**TRIGGER_MANIFEST, "skill_name": "other"})
        out = sb.build_trigger_comparison(baseline, ablation)
        self.assertEqual(out["evidence_class"], "indeterminate")
        self.assertTrue(any("different skills" in reason
                            for reason in out["provenance"]["reasons"]))

    def test_top_level_ablation_id_must_match_provenance(self):
        provenance = {**ABLATION_PROVENANCE, "id": "some-other-ablation"}
        out = self._compare(provenance=provenance)
        self.assertEqual(out["evidence_class"], "indeterminate")
        self.assertFalse(out["provenance"]["verified"])
        self.assertTrue(any("does not match provenance id" in reason
                            for reason in out["provenance"]["reasons"]))

    def test_missing_and_incomplete_arms_are_blocked_pairs(self):
        base = self._baseline_rows() + [
            trigger_row("only baseline", True, triggered=True, run_number=n)
            for n in range(1, 3)
        ]
        abl = self._ablation_rows() + [
            trigger_row("timed out", True, triggered=False,
                        complete=(n == 1), run_number=n)
            for n in range(1, 3)
        ]
        base += [trigger_row("timed out", True, triggered=True, run_number=n)
                 for n in range(1, 3)]
        out = self._compare(base_rows=base, abl_rows=abl)
        reasons = {b["query"]: b["reason"] for b in out["paired"]["blocked"]}
        self.assertEqual(reasons["only baseline"], "missing_ablation_arm")
        self.assertEqual(reasons["timed out"], "ablation_observations_incomplete")
        self.assertEqual(out["summary"]["comparable"], 6)
        self.assertFalse(out["paired"]["significance"]["significant_at_0_05"])
        self.assertTrue(out["paired"]["observed_significance"]["significant_at_0_05"])
        self.assertEqual(out["evidence_class"], "indeterminate")
        self.assertIn("coverage incomplete", out["note"])

    def test_incomplete_repetition_is_blocked(self):
        base = [trigger_row("partial", True, triggered=True, run_number=n)
                for n in range(1, 3)]
        abl = [trigger_row("partial", True, triggered=False,
                           complete=(n == 1), run_number=n)
               for n in range(1, 3)]
        out = self._compare(base_rows=base, abl_rows=abl)
        self.assertEqual(out["paired"]["blocked"][0]["reason"],
                         "ablation_observations_incomplete")
        self.assertEqual(out["summary"]["comparable"], 0)
        self.assertEqual(out["evidence_class"], "indeterminate")

    def test_mismatched_declared_repetition_sets_are_blocked(self):
        base = [trigger_row("mismatched", True, triggered=True, run_number=n)
                for n in range(1, 3)]
        abl = [trigger_row("mismatched", True, triggered=False)]
        out = self._compare(base_rows=base, abl_rows=abl,
                            abl_runs_per_query=1)
        self.assertEqual(out["paired"]["blocked"][0]["reason"],
                         "repetition_count_mismatch")
        self.assertEqual(out["evidence_class"], "indeterminate")

    def test_declared_repetition_shortfall_is_rejected(self):
        short = [trigger_row("short", True, triggered=True, run_number=1)]
        with self.assertRaises(SystemExit):
            self._compare(base_rows=short, abl_rows=self._ablation_rows())

    def test_whole_declared_cell_missing_from_results_is_rejected(self):
        baseline = trigger_report(self._baseline_rows())
        baseline["results"] = [row for row in baseline["results"]
                               if row["query_id"] != self.QUERIES[0]]
        ablation = trigger_report(self._ablation_rows(), ablation="drop-description",
                                  provenance=ABLATION_PROVENANCE, tree_hash=EDIT_HASH)
        with self.assertRaises(SystemExit):
            sb.build_trigger_comparison(baseline, ablation)

    def test_duplicate_repetition_identity_is_rejected(self):
        duplicate = [trigger_row("duplicate", True, triggered=True, run_number=1),
                     trigger_row("duplicate", True, triggered=True, run_number=1),
                     trigger_row("duplicate", True, triggered=True, run_number=2)]
        with self.assertRaises(SystemExit):
            self._compare(base_rows=duplicate, abl_rows=self._ablation_rows())

    def test_design_cannot_omit_a_persisted_result_cell(self):
        baseline = trigger_report(self._baseline_rows())
        baseline["design"] = baseline["design"][1:]
        ablation = trigger_report(self._ablation_rows(), ablation="drop-description",
                                  provenance=ABLATION_PROVENANCE, tree_hash=EDIT_HASH)
        with self.assertRaises(SystemExit):
            sb.build_trigger_comparison(baseline, ablation)

    def test_manifest_declared_component_target_is_authoritative(self):
        wrong = {
            **ABLATION_PROVENANCE,
            "components": [{**ABLATION_PROVENANCE["components"][0],
                            "target": {"field": "name"}}],
        }
        out = self._compare(provenance=wrong)
        self.assertEqual(out["evidence_class"], "indeterminate")
        self.assertTrue(any("manifest-declared treatment" in reason
                            for reason in out["provenance"]["reasons"]))

    def test_invalid_skill_ablation_can_never_confirm_behavioral_causality(self):
        invalid_manifest = {
            **TRIGGER_MANIFEST,
            "ablations": [{**TRIGGER_MANIFEST["ablations"][0], "invalid_skill": True}],
        }
        invalid_provenance = {**ABLATION_PROVENANCE, "mode": "invalid_skill"}
        baseline = trigger_report(self._baseline_rows())
        ablation = trigger_report(self._ablation_rows(), ablation="drop-description",
                                  provenance=invalid_provenance, tree_hash=EDIT_HASH)
        identity = sb.trigger_manifest_identity(invalid_manifest)
        baseline["manifest_identity"] = identity
        ablation["manifest_identity"] = identity
        out = sb.build_trigger_comparison(baseline, ablation)
        self.assertEqual(out["evidence_class"], "indeterminate")
        self.assertIn("invalid-skill experiment", out["note"])

    def test_protocol_drift_is_indeterminate_even_when_rows_regress(self):
        baseline = trigger_report(self._baseline_rows())
        ablation = trigger_report(self._ablation_rows(), ablation="drop-description",
                                  provenance=ABLATION_PROVENANCE, tree_hash=EDIT_HASH)
        ablation["protocol"] = {**ablation["protocol"], "timeout_seconds": 31}
        ablation["protocol_sha256"] = sb.canonical_json_sha256(ablation["protocol"])
        for row in ablation["results"]:
            row["protocol_sha256"] = ablation["protocol_sha256"]
        out = sb.build_trigger_comparison(baseline, ablation)
        self.assertEqual(out["evidence_class"], "indeterminate")
        self.assertTrue(any("experimental protocols" in reason
                            for reason in out["provenance"]["reasons"]))

    def test_protocol_semantics_must_match_report_design(self):
        for mutation in (
            lambda protocol: protocol.update(runs_per_query=999),
            lambda protocol: protocol["adapters"][0].update(agent="not-the-row-agent"),
        ):
            with self.subTest(mutation=mutation):
                baseline = trigger_report(self._baseline_rows())
                mutation(baseline["protocol"])
                baseline["protocol_sha256"] = sb.canonical_json_sha256(baseline["protocol"])
                for row in baseline["results"]:
                    row["protocol_sha256"] = baseline["protocol_sha256"]
                ablation = trigger_report(
                    self._ablation_rows(), ablation="drop-description",
                    provenance=ABLATION_PROVENANCE, tree_hash=EDIT_HASH)
                with self.assertRaises(SystemExit):
                    sb.build_trigger_comparison(baseline, ablation)

    def test_protocol_dependency_identity_cannot_omit_a_module(self):
        baseline = trigger_report(self._baseline_rows())
        identity = baseline["protocol"]["harness_identity"]
        self.assertIn("trigger_reporting.py", identity["modules"])
        identity["modules"].pop("trigger_reporting.py")
        payload = {key: value for key, value in identity.items()
                   if key != "identity_sha256"}
        identity["identity_sha256"] = sb.canonical_json_sha256(payload)
        baseline["protocol_sha256"] = sb.canonical_json_sha256(baseline["protocol"])
        for row in baseline["results"]:
            row["protocol_sha256"] = baseline["protocol_sha256"]
        ablation = trigger_report(
            self._ablation_rows(), ablation="drop-description",
            provenance=ABLATION_PROVENANCE, tree_hash=EDIT_HASH)
        with self.assertRaises(SystemExit):
            sb.build_trigger_comparison(baseline, ablation)

    def test_observed_isolation_drift_blocks_the_cell(self):
        base = [trigger_row("isolation", True, triggered=True, run_number=n)
                for n in range(1, 3)]
        abl = [trigger_row("isolation", True, triggered=False, run_number=n)
               for n in range(1, 3)]
        for row in base:
            row["protocol_observation"] = {"config_isolated": True}
        for row in abl:
            row["protocol_observation"] = {"config_isolated": False}
        out = self._compare(base_rows=base, abl_rows=abl)
        self.assertEqual(out["paired"]["blocked"][0]["reason"],
                         "protocol_observation_unsafe")
        self.assertEqual(out["evidence_class"], "indeterminate")

    def test_matching_unsafe_isolation_cannot_confirm(self):
        base = self._baseline_rows()
        abl = self._ablation_rows()
        unsafe = {"config_isolated": False,
                  "config_isolation_warning": "personal config may influence this measurement"}
        for row in base + abl:
            row["protocol_observation"] = unsafe
        out = self._compare(base_rows=base, abl_rows=abl)
        self.assertEqual(out["evidence_class"], "indeterminate")
        self.assertTrue(out["paired"]["blocked"])
        self.assertTrue(all(item["reason"] == "protocol_observation_unsafe"
                            for item in out["paired"]["blocked"]))

    def test_cosmetic_query_aliases_cannot_manufacture_six_units(self):
        queries = ["identical prompt" + (" " * n) for n in range(6)]
        base = [trigger_row(query, True, triggered=True, query_id=f"q{n}")
                for n, query in enumerate(queries)]
        abl = [trigger_row(query, True, triggered=False, query_id=f"q{n}")
               for n, query in enumerate(queries)]
        with self.assertRaises(SystemExit):
            self._compare(base_rows=base, abl_rows=abl, base_runs_per_query=1)

    def test_negative_polarity_overtriggering_is_a_causal_regression(self):
        queries = [f"negative {n}" for n in range(1, 7)]
        baseline = [trigger_row(q, False, triggered=False, run_number=n)
                    for q in queries for n in range(1, 3)]
        ablation = [trigger_row(q, False, triggered=True, run_number=n)
                    for q in queries for n in range(1, 3)]
        out = self._compare(base_rows=baseline, abl_rows=ablation)
        self.assertEqual(out["evidence_class"], "confirmed_causal")
        self.assertTrue(all(row["should_trigger"] is False
                            for row in out["regressed_queries"]))

    def test_query_id_definition_mismatch_is_blocked(self):
        base = [trigger_row("baseline text", True, triggered=True,
                            query_id="shared", run_number=n) for n in range(1, 3)]
        abl = [trigger_row("different text", True, triggered=False,
                           query_id="shared", run_number=n) for n in range(1, 3)]
        out = self._compare(base_rows=base, abl_rows=abl)
        self.assertEqual(out["paired"]["blocked"][0]["reason"],
                         "query_definition_mismatch")
        self.assertEqual(out["evidence_class"], "indeterminate")

    def test_row_hash_must_match_report_hash(self):
        baseline = trigger_report(self._baseline_rows())
        baseline["results"][0]["skill_tree_hash"] = "sha256:other"
        ablation = trigger_report(self._ablation_rows(), ablation="drop-description",
                                  provenance=ABLATION_PROVENANCE, tree_hash=EDIT_HASH)
        with self.assertRaises(SystemExit):
            sb.build_trigger_comparison(baseline, ablation)

    def test_real_matrix_reports_feed_the_comparer_without_provenance_drift(self):
        baseline = tm.run_matrix(
            DEMO_MANIFEST, demo_trigger_rows(), agents=["stub"], models=["offline"],
            runs_per_query=1, timeout=30, workers=1)
        ablation = tm.run_matrix(
            DEMO_MANIFEST, demo_trigger_rows(), agents=["stub"], models=["offline"],
            runs_per_query=1, timeout=30, workers=1, ablation="weaker-description")
        out = sb.build_trigger_comparison(baseline, ablation)
        self.assertTrue(out["provenance"]["verified"])
        self.assertEqual(out["paired"]["blocked"], [])
        self.assertEqual(ablation["skill_tree_hash"],
                         ablation["provenance"]["skill_hash"])

    def test_malformed_row_is_rejected(self):
        bad = self._baseline_rows()
        bad[0] = {**bad[0], "pass": True, "triggered": False}   # contradicts derived pass
        with self.assertRaises(SystemExit):
            self._compare(base_rows=bad)

    def test_baseline_carrying_an_ablation_is_rejected(self):
        baseline = trigger_report(self._baseline_rows(), ablation="drop-description",
                                  provenance=ABLATION_PROVENANCE)
        ablation = trigger_report(self._ablation_rows(), ablation="drop-description",
                                  provenance=ABLATION_PROVENANCE, tree_hash=EDIT_HASH)
        with self.assertRaises(SystemExit):
            sb.build_trigger_comparison(baseline, ablation)

    def test_non_trigger_report_is_rejected(self):
        baseline = trigger_report(self._baseline_rows())
        baseline["evidence_class"] = "answer"
        with self.assertRaises(SystemExit):
            sb.build_trigger_comparison(baseline, trigger_report(self._ablation_rows(), ablation="x", provenance=ABLATION_PROVENANCE, tree_hash=EDIT_HASH))
class AgyAdapterTests(unittest.TestCase):
    """Offline checks for the agy adapter. No CLI, no credentials, no tokens."""

    def test_skills_mount_where_agy_discovers_them(self):
        with tempfile.TemporaryDirectory() as td:
            tree = Path(td) / "tree"
            (tree / "demo").mkdir(parents=True)
            (tree / "demo" / "SKILL.md").write_text(skill_markdown(), encoding="utf-8")
            workspace = Path(td) / "ws"
            workspace.mkdir()
            copied = tm.AgyAdapter().mount(tree, workspace)
            self.assertTrue(copied, "mount returned no detection needles")
            self.assertTrue((workspace / ".agents" / "skills" / "demo" / "SKILL.md").exists(),
                            "agy discovers Agent Skills from .agents/skills")

    def test_invocation_attaches_the_workspace(self):
        # Without --add-dir/--new-project agy runs shell tools in its own
        # scratch directory, so a mounted skill is never reachable and every
        # run looks like a no-trigger. Pin the flags, not just the behaviour.
        argv = tm.build_agy_cli_argv(tm.AGY_DEFAULT_CMD, prompt="what version?",
                                     cwd=Path("/tmp/ws"), output="stream-json", model=None)
        self.assertIn("--add-dir", argv)
        self.assertEqual(argv[argv.index("--add-dir") + 1], "/tmp/ws")
        self.assertIn("--new-project", argv)
        self.assertEqual(argv[argv.index("--print") + 1], "what version?")

    def test_mounted_path_evidence_is_read_from_agy_tool_keys(self):
        # agy names its tool inputs AbsolutePath/CommandLine rather than
        # file_path/command. A detector that does not know those spellings sees
        # no evidence for a run that really did load the skill, and the matrix
        # reports 0% activation instead of a broken detector.
        mounted = Path("/ws/.agents/skills/demo/SKILL.md")
        stdout = "\n".join([
            json.dumps({"event": "step_update", "step_update": {
                "state": "DONE", "step_type": "tool", "tool_name": "view_file",
                "tool_info": {"name": "view_file", "parameters": {"AbsolutePath": str(mounted)}}}}),
            json.dumps({"event": "result", "result": {"status": "SUCCESS", "response": "done"}}),
        ])
        detection = tm.detect_trigger_detection(stdout, [mounted], source="agy")
        self.assertTrue(detection.triggered, "agy's AbsolutePath tool input carries the mounted skill path")

    def _agy_adapter_returning(self, stdout):
        class Fake(tm.AgyAdapter):
            _run_argv = staticmethod(lambda *a, **k: InvocationOutcome.from_process(
                stdout=stdout, stderr="", returncode=0, elapsed_ms=5))
        return Fake()

    def test_a_broken_stream_is_not_a_clean_no_trigger(self):
        # agy exits zero on a truncated stream. Left alone, a negative case
        # scores as a successful no-trigger and inflates the matrix, which is
        # indistinguishable in the summary table from a real result.
        for label, fixture in [("no terminal result", "stream-json-no-result.jsonl"),
                               ("unparsable line", "stream-json-bad-line.jsonl"),
                               ("failed status", "stream-json-failed-status.jsonl"),
                               ("non-string response", "stream-json-nonstring-response.jsonl")]:
            with self.subTest(stream=label), tempfile.TemporaryDirectory() as td:
                outcome = self._agy_adapter_returning(agy_fixture(fixture)).invoke("q", None, Path(td), 10)
                self.assertFalse(outcome.observation_complete, label)
                self.assertTrue(outcome.provider_error, label)

        with tempfile.TemporaryDirectory() as td:
            healthy = self._agy_adapter_returning(
                agy_fixture("stream-json-success.jsonl")).invoke("q", None, Path(td), 10)
        self.assertTrue(healthy.observation_complete)
        self.assertIsNone(healthy.provider_error)

    def test_a_trigger_run_reports_tools_it_could_not_classify(self):
        # The normalized stream this adapter hands back is what
        # write_trace_artifacts feeds to normalize_trace_records, so a trigger
        # run publishes the same tool_calls/file_reads/file_writes counts an
        # answer run does. An unclassified tool skews them here identically, so
        # the staleness of the classification must be just as visible on this
        # path — it is the same helper and the same metadata fields.
        stream = "\n".join([
            json.dumps({"event": "init", "init": {"model": "m", "cwd": "/ws",
                                                  "tools": ["run_command", "teleport_file"]}}),
            json.dumps({"event": "step_update", "step_update": {
                "state": "DONE", "step_type": "tool", "tool_name": "teleport_file",
                "tool_info": {"name": "teleport_file", "parameters": {"TargetFile": "/ws/x"}}}}),
            json.dumps({"event": "result", "result": {"status": "SUCCESS", "response": "ok"}})])
        with tempfile.TemporaryDirectory() as td:
            outcome = self._agy_adapter_returning(stream).invoke("q", None, Path(td), 10)
        self.assertEqual(outcome.metadata.get("unclassified_tools_used"), ("teleport_file",))
        self.assertEqual(outcome.metadata.get("unclassified_tools_advertised"), ("teleport_file",))
        # A fully classified run carries no gap fields, so their presence means something.
        with tempfile.TemporaryDirectory() as td:
            clean = self._agy_adapter_returning(
                agy_fixture("stream-json-success.jsonl")).invoke("q", None, Path(td), 10)
        self.assertNotIn("unclassified_tools_used", clean.metadata)
        self.assertNotIn("unclassified_tools_advertised", clean.metadata)

    def test_a_trigger_run_records_the_model_agy_resolved(self):
        # Without --model the row kept None and grouped under "default", losing
        # the identity agy names in its init event. The grid stays keyed on the
        # REQUESTED model — "no --model" is a real cell, and a spawn failure has
        # no stream to resolve a model from — so the measured identity is
        # recorded alongside rather than used to relabel the cell.
        stream = agy_fixture("stream-json-success.jsonl")
        with tempfile.TemporaryDirectory() as td:
            outcome = self._agy_adapter_returning(stream).invoke("q", None, Path(td), 10)
        self.assertEqual(outcome.metadata.get("resolved_model"), "gemini-3.1-pro-low")
        self.assertEqual(outcome.metadata.get("model_source"), "provider_reported")

        # An explicit request needs no discovery, so nothing is recorded.
        with tempfile.TemporaryDirectory() as td:
            explicit = self._agy_adapter_returning(stream).invoke("q", "gemini-explicit", Path(td), 10)
        self.assertIsNone(explicit.metadata.get("resolved_model"))
        self.assertIsNone(explicit.metadata.get("model_source"))

    def test_matrix_timeout_reaches_agy_print_mode(self):
        # agy applies its own five-minute --print-timeout regardless of the
        # matrix budget, so a longer --timeout is cut short without this.
        argv = tm.build_agy_cli_argv(tm.AGY_DEFAULT_CMD, prompt="q", cwd=Path("/ws"),
                                     output="stream-json", model=None, timeout=900)
        self.assertEqual(argv[argv.index("--print-timeout") + 1], "900s")

    def test_bad_command_prefix_fails_without_spawning(self):
        adapter = tm.AgyAdapter(agy_cmd='agy "unterminated')
        with tempfile.TemporaryDirectory() as td:
            outcome = adapter.invoke("q", None, Path(td), timeout=5)
        self.assertEqual(outcome.returncode, 127)
        self.assertFalse(outcome.metadata.get("config_isolated", True),
                         "agy has no config-home override; runs must not claim isolation")


@unittest.skipUnless(os.environ.get("RUN_AGY_TRIGGER_SMOKE") == "1",
                     "manual smoke: set RUN_AGY_TRIGGER_SMOKE=1 (needs agy CLI + credentials, spends tokens)")
class AgyMatrixSmokeTests(unittest.TestCase):
    def test_agy_matrix_end_to_end(self):
        runs = int(os.environ.get("AGY_TRIGGER_SMOKE_RUNS", "1"))
        model = os.environ.get("AGY_TRIGGER_SMOKE_MODEL")
        report = tm.run_matrix(DEMO_MANIFEST, demo_trigger_rows(), agents=["agy"],
                               models=[model] if model else [None], runs_per_query=runs,
                               timeout=300, workers=1,
                               agy_cmd=os.environ.get("AGY_TRIGGER_SMOKE_CMD", tm.AGY_DEFAULT_CMD))
        tm.print_matrix(report["matrix"])
        incomplete = [r for r in report["results"] if not r["observation_complete"]]
        self.assertFalse(incomplete, f"broken runs (crash/timeout), not trigger signal: {incomplete}")
        self.assertTrue(any(r["triggered"] for r in report["results"]),
                        "no agy run loaded the skill — detection, auth, or mounting is broken")


if __name__ == "__main__":
    unittest.main()
