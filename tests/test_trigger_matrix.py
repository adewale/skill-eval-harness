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
from unittest import mock

import run_pi_trigger_eval as tr
import run_trigger_matrix as tm
import skill_benchmark as sb
from agent_capabilities import AGENT_CAPABILITIES
from trigger_contracts import (
    InvocationOutcome,
    InvocationState,
)

ROOT = Path(__file__).resolve().parents[1]
DEMO_MANIFEST = ROOT / "examples" / "demo-skill" / "evals" / "shared-benchmark.json"


def demo_trigger_rows():
    manifest = tm.load_manifest(DEMO_MANIFEST)
    return tm.cases_from_manifest(manifest, "tune")


def completed_invocation(stdout: str) -> InvocationOutcome:
    return InvocationOutcome.from_process(stdout=stdout, stderr="", returncode=0, elapsed_ms=1)


PI_STOP = {"type": "agent_end", "messages": [{"role": "assistant", "stopReason": "stop"}]}


def pi_stream(*events) -> str:
    return "".join(json.dumps(event) + "\n" for event in events)


def pi_runs(fake):
    """Replace the Pi process boundary the matrix's Pi adapter calls."""
    return mock.patch.object(tm.PiAdapter, "_run_argv", staticmethod(fake))


def write_rows(root: Path, rows) -> Path:
    path = root / "rows.json"
    path.write_text(json.dumps(rows), encoding="utf-8")
    return path


def run_pi_cli(extra_argv, fake, out: Path):
    """Run `skill-pi-trigger-eval` on the demo manifest; return (exit code, report)."""
    argv = ["skill-pi-trigger-eval", str(DEMO_MANIFEST), *extra_argv, "--out", str(out)]
    with mock.patch.object(sys, "argv", argv), pi_runs(fake), mock.patch("builtins.print"):
        code = tr.main()
    return code, json.loads(out.read_text(encoding="utf-8"))


def observe_pi(query, should_trigger, fake, *, trace_dir=None):
    """One Pi cell of the matrix on the demo skill's canonical tree."""
    manifest = tm.load_manifest(DEMO_MANIFEST)
    with tempfile.TemporaryDirectory() as td:
        tree, tree_hash, _ = tm.trigger_tree_for_manifest(
            sb.repo_root_for_manifest(DEMO_MANIFEST), manifest, Path(td), None)
        with pi_runs(fake):
            return tm.observe_cell_query(
                tm.PiAdapter(), tree, query, should_trigger, None, 12,
                trace_dir=trace_dir, metadata={"skill_tree_hash": tree_hash})


class TriggerRowBoundaryTests(unittest.TestCase):
    def test_eval_set_requires_real_boolean_should_trigger(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "rows.json"
            path.write_text(json.dumps([{"query": "review this", "should_trigger": "false"}]), encoding="utf-8")
            args = SimpleNamespace(eval_set=str(path), split="tune")
            with self.assertRaises(SystemExit) as ctx:
                tm.eval_rows_from_args(args, DEMO_MANIFEST)
        self.assertIn("should_trigger must be true or false", str(ctx.exception))

    def test_the_protocol_rejects_nonpositive_concurrency_limits(self):
        for field, mutation in (
            ("timeout_seconds", {"timeout": 0, "runs_per_query": 1, "workers": 1}),
            ("runs_per_query", {"timeout": 1, "runs_per_query": 0, "workers": 1}),
            ("workers", {"timeout": 1, "runs_per_query": 1, "workers": 0}),
            ("workers", {"timeout": 1, "runs_per_query": 1, "workers": False}),
        ):
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, field):
                tm.trigger_protocol([], None, **mutation)

    def test_matrix_rejects_zero_workers_before_constructing_an_executor(self):
        with self.assertRaisesRegex(SystemExit, "workers must be a positive integer"):
            tm.run_matrix(
                DEMO_MANIFEST, demo_trigger_rows()[:1], agents=["stub"],
                models=[None], runs_per_query=1, timeout=30, workers=0,
            )

    def test_the_protocol_rejects_ambiguous_model_identities(self):
        for model in ("", "   ", False):
            with self.subTest(model=model), self.assertRaises(ValueError):
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
            tm.validate_trigger_rows(rows, "fixture")

    def test_exact_duplicate_query_id_is_rejected_before_runs_are_scheduled(self):
        rows = [
            {"query_id": "same", "query": "one", "should_trigger": True},
            {"query_id": "same", "query": "one", "should_trigger": True},
        ]
        with self.assertRaisesRegex(SystemExit, "duplicate query_id"):
            tm.validate_trigger_rows(rows, "fixture")

    def test_distinct_ids_cannot_alias_the_same_authored_query(self):
        rows = [
            {"query_id": "first", "query": "one", "should_trigger": True},
            {"query_id": "second", "query": "one", "should_trigger": True},
        ]
        with self.assertRaisesRegex(SystemExit, "alias the same query"):
            tm.validate_trigger_rows(rows, "fixture")

    def test_cosmetic_query_variants_are_one_inference_identity(self):
        rows = [
            {"query_id": "first", "query": "Caf\u00e9   prompt", "should_trigger": True},
            {"query_id": "second", "query": "  CAFE\u0301 prompt\t", "should_trigger": True},
        ]
        with self.assertRaisesRegex(SystemExit, "alias the same query"):
            tm.validate_trigger_rows(rows, "fixture")

    def test_conflicting_id_aliases_are_rejected(self):
        rows = [{"id": "first", "query_id": "second",
                 "query": "one", "should_trigger": True}]
        with self.assertRaisesRegex(SystemExit, "conflicting query_id and id"):
            tm.validate_trigger_rows(rows, "fixture")

    def test_eval_set_rejects_evals_and_queries_aliases_together(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "rows.json"
            row = {"query": "one", "should_trigger": True}
            path.write_text(json.dumps({"evals": [row], "queries": [row]}), encoding="utf-8")
            args = SimpleNamespace(eval_set=str(path), split="tune")
            with self.assertRaisesRegex(SystemExit, "exactly one of evals or queries"):
                tm.eval_rows_from_args(args, DEMO_MANIFEST)

    def test_pi_cli_is_the_matrix_with_pi_home_outside_its_working_directory(self):
        seen = {}

        def fake_run(plan):
            config, cwd = Path(dict(plan.environment or {})["PI_CODING_AGENT_DIR"]), Path(plan.cwd)
            seen.update({
                "argv": list(plan.argv), "cwd": cwd, "config": config,
                "auth_copied": (config / "auth.json").is_file(),
                "mounted": sorted(path.name for path in (config / "skills").iterdir()),
                # What Pi's read/grep/find/ls tools can reach from where it runs.
                "reachable": sorted(path.name for path in cwd.rglob("*")),
            })
            return completed_invocation(pi_stream(PI_STOP))

        with tempfile.TemporaryDirectory() as td:
            user_home = Path(td) / "user-pi"
            user_home.mkdir()
            (user_home / "auth.json").write_text('{"token": "user-token-123"}', encoding="utf-8")
            eval_set = write_rows(Path(td), [{"query_id": "negative", "query": "ordinary chat",
                                              "should_trigger": False}])
            with mock.patch.dict(os.environ, {"PI_CODING_AGENT_DIR": str(user_home)}):
                code, report = run_pi_cli(
                    ["--eval-set", str(eval_set), "--runs-per-query", "1", "--workers", "1"],
                    fake_run, Path(td) / "report.json")
        self.assertEqual(code, 0)
        self.assertEqual(report["protocol"]["producer"], "skill-trigger-matrix")
        self.assertEqual([adapter["agent"] for adapter in report["protocol"]["adapters"]], ["pi"])
        self.assertEqual(report["results"][0]["protocol_observation"],
                         {"config_isolated": True, "pi_home_outside_workdir": True})
        self.assertEqual(seen["argv"][0], "pi")
        self.assertNotEqual(seen["cwd"].resolve(), ROOT.resolve())
        self.assertNotEqual(seen["config"], user_home)
        self.assertFalse(seen["config"].is_relative_to(seen["cwd"]))
        self.assertTrue(seen["auth_copied"])
        self.assertTrue(seen["mounted"])
        self.assertNotIn("auth.json", seen["reachable"])
        self.assertNotIn("SKILL.md", seen["reachable"])
        self.assertFalse(seen["config"].exists(), "the Pi home and its copied auth are removed")

    def test_an_agent_home_is_removed_even_when_the_mount_is_refused(self):
        for adapter in (tm.PiAdapter(), tm.CodexAdapter()):
            with self.subTest(agent=adapter.name), tempfile.TemporaryDirectory() as td:
                tree = Path(td) / "tree" / "demo"
                tree.mkdir(parents=True)
                (tree / "SKILL.md").write_text("---\nname: demo\n---\n", encoding="utf-8")
                created = []
                real_mount = type(adapter).mount

                def spy(self, tree_dir, workspace, _real=real_mount, _created=created):
                    _created.append(workspace)
                    return _real(self, tree_dir, workspace)

                with mock.patch.object(type(adapter), "mount", spy), \
                     self.assertRaisesRegex(ValueError, "does not match"):
                    tm.observe_cell_query(adapter, tree.parent, "q", True, None, 5,
                                          metadata={"skill_tree_hash": "0" * 64})
                home = (adapter._pi_home if adapter.name == "pi" else adapter._codex_home)(created[0])
                self.assertFalse(home.exists())

    def test_pi_ablation_report_names_the_edited_tree_on_every_repetition(self):
        with tempfile.TemporaryDirectory() as td:
            eval_set = write_rows(Path(td), [{"query_id": "pi-query", "query": "ordinary chat",
                                              "should_trigger": False}])
            code, report = run_pi_cli(
                ["--eval-set", str(eval_set), "--runs-per-query", "2", "--workers", "1",
                 "--ablation", "weaker-description"],
                lambda plan: completed_invocation(pi_stream(PI_STOP)), Path(td) / "report.json")
        self.assertEqual(code, 0)
        provenance = report["provenance"]
        self.assertEqual(report["skill_tree_hash"], provenance["skill_hash"])
        self.assertNotEqual(report["skill_tree_hash"], provenance["parent_skill_hash"])
        self.assertEqual(sorted((row["query_id"], row["run_number"]) for row in report["results"]),
                         [("pi-query", 1), ("pi-query", 2)])
        self.assertEqual({row["skill_tree_hash"] for row in report["results"]},
                         {report["skill_tree_hash"]})

    def test_pi_main_reports_are_accepted_by_trigger_comparer(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            eval_set = write_rows(root, [{"query_id": "negative", "query": "ordinary chat",
                                          "should_trigger": False}])
            reports = []
            for ablation in (None, "weaker-description"):
                extra = ["--eval-set", str(eval_set), "--runs-per-query", "1",
                         "--workers", "1", "--timeout", "12"]
                if ablation:
                    extra.extend(["--ablation", ablation])
                code, report = run_pi_cli(
                    extra, lambda plan: completed_invocation(pi_stream(PI_STOP)),
                    root / ("ablation.json" if ablation else "baseline.json"))
                self.assertEqual(code, 0)
                reports.append(report)
        compared = sb.build_trigger_comparison(reports[0], reports[1])
        self.assertTrue(compared["provenance"]["verified"])
        self.assertEqual(compared["paired"]["blocked"], [])

    def test_pi_main_excludes_incomplete_runs_from_pass_rate_denominator(self):
        timed_out = InvocationOutcome.from_process(
            stdout=json.dumps({"type": "agent_start"}) + "\n",
            stderr="timeout", returncode=124, elapsed_ms=1,
        )
        with tempfile.TemporaryDirectory() as td:
            eval_set = write_rows(Path(td), [{"query_id": "negative", "query": "ordinary chat",
                                              "should_trigger": False}])
            code, report = run_pi_cli(
                ["--eval-set", str(eval_set), "--runs-per-query", "1", "--workers", "1",
                 "--timeout", "1"],
                lambda plan: timed_out, Path(td) / "report.json")
        summary = report["summary"]
        self.assertEqual(code, 1)
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

        with tempfile.TemporaryDirectory() as td, \
             mock.patch.object(tm.PiStream, "parse", wraps=tm.PiStream.parse) as parse_stream:
            trace_dir = Path(td) / "trace"
            result = observe_pi(
                "ordinary chat", False,
                lambda plan: completed_invocation(provider_error + "\n"),
                trace_dir=trace_dir).as_row()
            artifacts = [
                json.loads((trace_dir / name).read_text(encoding="utf-8"))
                for name in ("metrics.json", "metadata.json")
            ]
        # Detection, telemetry and the trace artifacts share one parsed stream.
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

    def test_pi_runner_redacts_ambient_and_auth_secrets_before_writing(self):
        # One secret from the environment, one from the Pi auth the run copies;
        # the model echoes both into its stream and stderr.
        env_secret, auth_secret = "ambient-env-secret-123", "pi-auth-secret-456"

        def leaky_pi(plan):
            assistant = {"role": "assistant", "stopReason": "stop",
                         "content": [{"type": "text", "text": f"{env_secret} {auth_secret}"}]}
            return InvocationOutcome.from_process(
                stdout=json.dumps({"type": "agent_end", "messages": [assistant]}) + "\n",
                stderr=f"debug {env_secret} {auth_secret}", returncode=0, elapsed_ms=1)

        with tempfile.TemporaryDirectory() as td:
            pi_home = Path(td) / "pi-home"
            pi_home.mkdir()
            (pi_home / "auth.json").write_text(json.dumps({"token": auth_secret}), encoding="utf-8")
            trace_dir = Path(td) / "trace"
            with mock.patch.dict(os.environ, {"OPENAI_API_KEY": env_secret,
                                              "PI_CODING_AGENT_DIR": str(pi_home)}):
                row = observe_pi("ordinary chat", False, leaky_pi, trace_dir=trace_dir).as_row()
            written = {path.name: path.read_text(encoding="utf-8")
                       for path in trace_dir.iterdir()}
        written["row"] = json.dumps(row)
        self.assertIn("[REDACTED]", written["trace.jsonl"])
        self.assertIn("[REDACTED]", row["stderr"])
        for name, text in written.items():
            with self.subTest(artifact=name):
                self.assertNotIn(env_secret, text)
                self.assertNotIn(auth_secret, text)

    def test_pi_adapter_propagates_json_provider_error_as_incomplete(self):
        provider_error = json.dumps({
            "type": "agent_end", "willRetry": False,
            "messages": [{"stopReason": "error", "errorMessage": "provider rejected model"}],
        })
        run = completed_invocation(provider_error)
        with tempfile.TemporaryDirectory() as td, mock.patch.object(tm.PiAdapter, "_run_argv", staticmethod(lambda *args, **kwargs: run)):
            workspace = Path(td) / "workspace"
            workspace.mkdir()
            result = tm.PiAdapter().invoke("ordinary chat", None, workspace, 12)
        self.assertFalse(result.observation_complete)
        self.assertEqual(result.provider_error, "provider rejected model")

    def test_pi_matrix_detection_and_telemetry_share_one_parsed_stream(self):
        def successful_pi(plan):
            skill = Path(plan.environment["PI_CODING_AGENT_DIR"]) / "skills" / "demo" / "SKILL.md"
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
            row = tm.observe_cell_query(
                tm.PiAdapter(), tree, "review this", True, None, 12,
                metadata={"skill_tree_hash": sb.skill_tree_hash(tree)},
            ).as_row()
        self.assertEqual(parse_stream.call_count, 1)
        self.assertTrue(row["triggered"])
        self.assertEqual(row["usage_normalized"]["total_tokens"], 5)

    def test_pi_trace_artifacts_carry_the_detector_evidence_and_the_query(self):
        def reads_skill(plan):
            skill = Path(plan.environment["PI_CODING_AGENT_DIR"]) / "skills"
            mounted = next(skill.rglob("SKILL.md"))
            usage = {"input": 3, "output": 2, "totalTokens": 5}
            return completed_invocation(pi_stream(
                {"type": "tool_execution_start", "toolName": "read", "args": {"path": str(mounted)}},
                {"type": "tool_execution_end", "toolName": "read", "args": {"path": str(mounted)},
                 "result": "ok"},
                {"type": "agent_end", "messages": [{"role": "assistant", "stopReason": "stop",
                                                    "usage": usage}]}))

        with tempfile.TemporaryDirectory() as td:
            trace_dir = Path(td) / "trace"
            observe_pi("demo", True, reads_skill, trace_dir=trace_dir)
            metrics = json.loads((trace_dir / "metrics.json").read_text(encoding="utf-8"))
            meta = json.loads((trace_dir / "metadata.json").read_text(encoding="utf-8"))
        self.assertTrue(metrics["skill_invoked"])
        self.assertEqual(metrics["total_tokens"], 5)
        self.assertEqual((meta["query"], meta["should_trigger"], meta["pass"]), ("demo", True, True))

    def test_pi_timeout_with_parseable_partial_trace_is_not_telemetry_complete(self):
        def timed_out(plan):
            return InvocationOutcome.from_process(
                stdout=json.dumps({"type": "command", "command": "partial"}) + "\n",
                stderr="timeout", returncode=124, elapsed_ms=10,
            )

        with tempfile.TemporaryDirectory() as td:
            trace_dir = Path(td) / "trace"
            observe_pi("ordinary chat", False, timed_out, trace_dir=trace_dir)
            meta = json.loads((trace_dir / "metadata.json").read_text(encoding="utf-8"))
        self.assertFalse(meta["observation_complete"])
        self.assertEqual(meta["telemetry"]["measurements"]["commands"]["availability"], "unavailable")


class StubMatrixOfflineTests(unittest.TestCase):
    def test_every_matrix_adapter_has_an_explicit_trace_dialect(self):
        self.assertLessEqual(set(tm.ADAPTERS), set(sb.TRACE_DIALECTS))

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
            row = tm.observe_cell_query(
                SecretEchoAdapter(), tree, "q", True, None, 12, trace_dir,
                metadata={"skill_tree_hash": sb.skill_tree_hash(tree)},
            ).as_row()
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

    def test_a_crashed_pi_query_is_an_incomplete_row_not_a_crashed_run(self):
        def crash(plan):
            raise RuntimeError("provider unavailable")

        with tempfile.TemporaryDirectory() as td:
            code, report = run_pi_cli(["--runs-per-query", "1"], crash, Path(td) / "report.json")
        self.assertEqual(code, 1)
        self.assertEqual(report["summary"]["measurement_status"], "incomplete")
        self.assertTrue(report["results"])
        self.assertEqual({row["error"] for row in report["results"]},
                         {"RuntimeError: provider unavailable"})


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
        # Hitting --max-turns exits 1, but the model had its whole window to
        # load the skill, so a no-trigger here is a valid negative observation.
        stdout = "\n".join([
            json.dumps({"type": "assistant", "message": {"content": [
                {"type": "text", "text": "Still thinking."}]}}),
            json.dumps({"type": "result", "subtype": "error_max_turns", "is_error": True}),
        ]) + "\n"

        def fake_run(*args, **kwargs):
            return InvocationOutcome.from_process(
                stdout=stdout, stderr="", returncode=1, elapsed_ms=3)

        with tempfile.TemporaryDirectory() as td, \
             mock.patch.object(tm.ClaudeAdapter, "_run_argv", staticmethod(fake_run)), \
             mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}, clear=True):
            result = tm.ClaudeAdapter().invoke("q", "haiku", Path(td), 1)
        self.assertIs(result.state, InvocationState.COMPLETE)
        self.assertTrue(result.observation_complete)
        self.assertIsNone(result.provider_error)
        self.assertEqual(result.returncode, 1)

    def test_max_turns_subtype_does_not_reclassify_timeout_or_spawn_failure(self):
        stdout = json.dumps({"type": "result", "subtype": "error_max_turns"})
        for returncode, state in ((124, InvocationState.TIMED_OUT), (127, InvocationState.SPAWN_FAILED)):
            def fake_run(*args, _returncode=returncode, **kwargs):
                if _returncode == 124:
                    return InvocationOutcome.from_timeout(
                        stdout=stdout, stderr="failure", elapsed_ms=3)
                return InvocationOutcome.spawn_failed(
                    stdout=stdout, stderr="failure", elapsed_ms=3)

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

        def fake_run(plan):
            env = dict(plan.environment or {})
            config_dir = Path(env["CLAUDE_CONFIG_DIR"])
            seen["config_dir"] = config_dir
            seen["credentials"] = (config_dir / ".credentials.json").read_text(encoding="utf-8")
            return completed_invocation(json.dumps({"type": "result", "subtype": "success"}) + "\n")

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

        def fake_run(plan):
            env = dict(plan.environment or {})
            seen["config_dir"] = env.get("CLAUDE_CONFIG_DIR")
            return completed_invocation(json.dumps({"type": "result", "subtype": "success"}) + "\n")

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

    def test_a_recorded_claude_stream_is_a_complete_observation_with_skill_evidence(self):
        # Real Claude Code output (tests/fixtures/claude/README.md): system
        # events, thinking blocks and parent_tool_use_id: null that the
        # canned fragments above never carry must not fail the protocol checks.
        recorded = (Path(__file__).parent / "fixtures" / "claude"
                    / "stream-json.plugin-skill.jsonl").read_text(encoding="utf-8")

        def fake_run(*args, **kwargs):
            return InvocationOutcome.from_process(stdout=recorded, stderr="", returncode=0, elapsed_ms=1)

        with tempfile.TemporaryDirectory() as td, \
             mock.patch.object(tm.ClaudeAdapter, "_run_argv", staticmethod(fake_run)), \
             mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}, clear=True):
            result = tm.ClaudeAdapter().invoke("q", None, Path(td), 1)
        self.assertIs(result.state, InvocationState.COMPLETE)
        self.assertIsNone(result.provider_error)
        detection = self._adapter().detect(result, ["probe-plugin:tidy-commit"], [])
        self.assertEqual(detection.legacy_evidence, ["Skill tool invoked: probe-plugin:tidy-commit"])


class CodexAdapterTests(unittest.TestCase):
    """Codex trigger support without a live codex binary."""

    def test_codex_completed_read_of_the_mounted_skill_is_a_trigger(self):
        # `codex exec --json` reports a finished shell command as an
        # item.completed command_execution; reading the SKILL.md mounted under
        # the isolated $CODEX_HOME/skills and then ending the turn is load evidence.
        def fake_run(plan):
            argv = list(plan.argv)
            skills_dir = Path(argv[argv.index("--add-dir") + 1])
            skill_md = next(skills_dir.glob("*/SKILL.md"))
            command = f"bash -lc 'cat {skill_md}'"
            stream = [
                {"type": "thread.started", "thread_id": "t"},
                {"type": "turn.started"},
                {"type": "item.completed", "item": {
                    "id": "item_0", "type": "command_execution", "command": command,
                    "aggregated_output": skill_md.read_text(encoding="utf-8"),
                    "exit_code": 0, "status": "completed"}},
                {"type": "item.completed", "item": {
                    "id": "item_1", "type": "agent_message", "text": "Reviewed."}},
                {"type": "turn.completed", "usage": {"input_tokens": 10, "output_tokens": 5}},
            ]
            return InvocationOutcome.from_process(
                stdout="".join(json.dumps(record) + "\n" for record in stream),
                stderr="", returncode=0, elapsed_ms=1)

        should_fire = [row for row in demo_trigger_rows() if row["should_trigger"]]
        with mock.patch.object(tm.CodexAdapter, "_run_argv", staticmethod(fake_run)):
            report = tm.run_matrix(DEMO_MANIFEST, should_fire, agents=["codex"], models=[None],
                                   runs_per_query=1, timeout=30, workers=1)
        (row,) = report["results"]
        self.assertTrue(row["observation_complete"])
        self.assertTrue(row["triggered"])
        self.assertTrue(row["pass"])
        self.assertEqual(len(row["evidence"]), 1)
        self.assertRegex(row["evidence"][0], r"^bash -lc 'cat .*-codex-home/skills/.*/SKILL\.md'$")

    def test_codex_statusless_command_is_not_load_evidence(self):
        # Without a completion status the command may never have run.
        mounted = Path("/tmp/trigger-x/.codex/skills/demo-reviewer/SKILL.md")
        stream = json.dumps({"type": "command", "command": ["bash", "-lc", f"cat {mounted}"]})
        detection = tm.CodexAdapter().detect(completed_invocation(stream), ["demo-reviewer"], [mounted])
        self.assertFalse(detection.triggered)
        self.assertFalse(detection.evidence)

    def test_codex_skill_name_in_prose_is_not_load_evidence(self):
        mounted = Path("/tmp/trigger-x/.codex/skills/demo-reviewer/SKILL.md")
        prose = json.dumps({"type": "message", "content": "I would use demo-reviewer."})
        self.assertFalse(tm.CodexAdapter().detect(completed_invocation(prose), ["demo-reviewer"], [mounted]).triggered)

    def test_malformed_or_unterminated_streams_are_not_valid_negative_observations(self):
        # Parseable JSON is not enough: Codex must end its turn and Vibe must
        # end with an assistant answer before absence of evidence counts.
        for adapter_cls in (tm.CodexAdapter, tm.VibeAdapter):
            for stdout, reason in (("not-json\n", "is malformed"), ("{}\n", "JSON stream must")):
                def fake_run(*args, _stdout=stdout, **kwargs):
                    return InvocationOutcome.from_process(
                        stdout=_stdout, stderr="", returncode=0, elapsed_ms=1)

                with self.subTest(adapter=adapter_cls.name, stdout=stdout), \
                     tempfile.TemporaryDirectory() as td, \
                     mock.patch.object(adapter_cls, "_run_argv", staticmethod(fake_run)):
                    workspace = Path(td) / "workspace"
                    workspace.mkdir()
                    result = adapter_cls().invoke("q", None, workspace, 1)
                    self.assertIs(result.state, InvocationState.PROVIDER_FAILED)
                    self.assertIn(reason, result.provider_error or "")

    def test_codex_invoke_appends_raw_query_model_and_external_skill_dir(self):
        seen = {}

        def fake_run(plan):
            argv, cwd = list(plan.argv), plan.cwd
            env, timeout = dict(plan.environment or {}), int(plan.timeout_s)
            seen.update({"argv": argv, "cwd": cwd, "env": env, "timeout": timeout})
            return completed_invocation('{"type":"turn.completed"}\n')

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

    def test_codex_invoke_seeds_auth_without_copying_user_skills(self):
        seen = {}

        def fake_run(plan):
            cwd = plan.cwd
            env = dict(plan.environment or {})
            codex_home = Path(env["CODEX_HOME"])
            seen["auth"] = (codex_home / "auth.json").read_text(encoding="utf-8")
            seen["config"] = (codex_home / "config.toml").read_text(encoding="utf-8")
            seen["mounted_skills_survive"] = (codex_home / "skills" / "demo").is_dir()
            seen["user_skills_not_copied"] = not (codex_home / "skills" / "personal").exists()
            seen["workspace_auth_present"] = (Path(cwd) / ".codex" / "auth.json").exists()
            seen["workspace_config_present"] = (Path(cwd) / ".codex" / "config.toml").exists()
            return completed_invocation('{"type":"turn.completed"}\n')

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

    def test_cell_observation_redacts_ambient_env_secrets(self):
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
            row = tm.observe_cell_query(
                LeakyAdapter(), tree, "q", False, None, 12,
                trace_dir=Path(td) / "trace",
                metadata={"skill_tree_hash": sb.skill_tree_hash(tree),
                          "external": {"token": "ambient-secret-token"}},
            ).as_row()
            trace_text = (Path(row["trace_dir"]) / "trace.jsonl").read_text(encoding="utf-8")
            trace_metadata = json.loads((Path(row["trace_dir"]) / "metadata.json").read_text(encoding="utf-8"))
        self.assertNotIn("ambient-secret-token", row["stderr"])
        self.assertEqual(row["stderr"], "err [REDACTED]")
        self.assertEqual(row["debug"], {"auth": "[REDACTED]"})
        self.assertEqual(row["external"], {"token": "[REDACTED]"})
        self.assertEqual(trace_metadata["debug"], {"auth": "[REDACTED]"})
        self.assertEqual(trace_metadata["external"], {"token": "[REDACTED]"})
        self.assertNotIn("ambient-secret-token", trace_text)

    def test_cell_observation_requires_invoke_contract(self):
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
                tm.observe_cell_query(
                    BrokenAdapter(), tree, "q", True, None, 12,
                    metadata={"skill_tree_hash": sb.skill_tree_hash(tree)},
                )
        self.assertIn("observation_complete", str(ctx.exception))

    def test_cell_observation_rejects_mount_bytes_that_differ_from_scheduled_tree(self):
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
                tm.observe_cell_query(
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

    def test_vibe_cmd_flag_defaults_to_the_shared_vibe_command(self):
        parser = tm.build_arg_parser()
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

    def test_vibe_invoke_uses_isolated_home_model_env_and_prompt_arg(self):
        seen = {}

        def fake_run(plan):
            argv, cwd = list(plan.argv), plan.cwd
            env, timeout = dict(plan.environment or {}), int(plan.timeout_s)
            input_text = plan.input_text
            seen.update({"argv": argv, "cwd": cwd, "env": env, "timeout": timeout, "input_text": input_text})
            seen["vibe_home_inside_workdir"] = Path(env["VIBE_HOME"]).is_relative_to(Path(cwd))
            seen["workspace_vibe_env_present"] = (Path(cwd) / ".vibe-home" / ".env").exists()
            return completed_invocation(json.dumps({"role": "assistant", "content": "ok"}) + "\n")

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

    def test_each_advertised_live_smoke_env_enables_a_skipped_test(self):
        # Users are told to set a backend's live_smoke_env to run its live
        # smoke, so setting it must turn on a test that is skipped by default.
        advertised = {cap.live_smoke_env: agent for agent, cap in AGENT_CAPABILITIES.items()
                      if cap.live_smoke_env}

        def runnable_tests(path, environ):
            spec = importlib.util.spec_from_file_location(f"_smoke_gate_{path.stem}", path)
            module = importlib.util.module_from_spec(spec)
            with mock.patch.dict(os.environ, environ):
                spec.loader.exec_module(module)
            loader = unittest.TestLoader()   # unaffected by a -k name filter
            return {f"{cls.__name__}.{name}"
                    for cls in vars(module).values()
                    if isinstance(cls, type) and issubclass(cls, unittest.TestCase)
                    and not getattr(cls, "__unittest_skip__", False)
                    for name in loader.getTestCaseNames(cls)
                    if not getattr(getattr(cls, name), "__unittest_skip__", False)}

        unset = {name: "" for name in advertised}
        for env_name, agent in advertised.items():
            enabled = set()
            for path in sorted(Path(__file__).parent.glob("test_*.py")):
                if env_name in path.read_text(encoding="utf-8"):
                    enabled |= (runnable_tests(path, {**unset, env_name: "1"})
                                - runnable_tests(path, unset))
            with self.subTest(agent=agent, env=env_name):
                self.assertTrue(enabled, f"{env_name}=1 enables no test")


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


class PiProtocolRequirementTests(unittest.TestCase):
    """A Pi report declares the isolation controls it ran under. Reports made
    before Pi's home moved out of its working directory stay readable; any
    other control set is refused."""

    def protocol(self, required):
        adapter = tm.PiAdapter().protocol_parameters()
        return {"schema_version": 1, "producer": "skill-trigger-matrix",
                "harness_identity": sb.trigger_harness_identity(),
                "timeout_seconds": 30, "runs_per_query": 1, "workers": 1,
                "adapters": [{**adapter, "required_observations": required, "models": [None]}]}

    def validate(self, required):
        return sb._validated_trigger_protocol(
            self.protocol(required), label="report", runs_per_query=1,
            design_pairs={("pi", None)})

    def test_the_adapter_declares_its_home_outside_the_working_directory(self):
        declared = tm.PiAdapter().protocol_parameters()["required_observations"]
        self.assertEqual(self.validate(declared), {"pi": declared})

    def test_a_report_from_before_the_move_is_still_read(self):
        self.assertEqual(self.validate({"config_isolated": True}),
                         {"pi": {"config_isolated": True}})

    def test_any_other_control_set_is_refused(self):
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr), self.assertRaises(SystemExit):
            self.validate({"pi_home_outside_workdir": True})
        self.assertIn("must require", stderr.getvalue())


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


if __name__ == "__main__":
    unittest.main()
