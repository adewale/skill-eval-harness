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
import functools
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from helpers import (
    CLAUDE_POST_RESULT_RECORDS,
    assert_dies,
    claude_streams_ending_after_result,
    run_cli,
)

import run_pi_trigger_eval as tr
import run_trigger_matrix as tm
import skill_benchmark as sb
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


def fake_trigger_claude(path: Path, probe: Path, *, invoke_project_skill: bool = False,
                        trailing_records: list[dict] | None = None) -> Path:
    """A fake `claude` for trigger runs. Its init event lists the skills Claude
    Code offers the model: one bundled skill, the project skills mounted in the
    working directory (by directory name, as Claude Code 2.1.269 lists them),
    the personal skills in its config dir, and an organisation skill when
    CLAUDE_CODE_SYNC_SKILLS is set. It records what it was offered and where it
    looked, and echoes any auth token it was given, as a leaky CLI would. With
    invoke_project_skill it calls the first project skill by that name;
    trailing_records are written after the `result` record."""
    path.write_text(f"""#!{sys.executable}
import json, os, sys
from pathlib import Path
config = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
skills = ["update-config"]
skills += sorted(p.name for p in (Path.cwd() / ".claude" / "skills").iterdir())
if (config / "skills").is_dir():
    skills += sorted(p.name for p in (config / "skills").iterdir())
if os.environ.get("CLAUDE_CODE_SYNC_SKILLS"):
    skills.append("org-synced-skill")
project = sorted(p.name for p in (Path.cwd() / ".claude" / "skills").iterdir())
with open({str(probe)!r}, "a", encoding="utf-8") as handle:
    handle.write(json.dumps({{"config": str(config), "cwd": os.getcwd(), "skills": skills,
                             "sync_skills": "CLAUDE_CODE_SYNC_SKILLS" in os.environ}}) + "\\n")
token = " ".join(os.environ.get(name, "") for name in
                 ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY")).strip()
sys.stderr.write("auth: " + token + "\\n")
records = [{{"type": "system", "subtype": "init", "session_id": "s", "skills": skills}}]
if {invoke_project_skill!r}:
    records += [{{"type": "assistant", "message": {{"role": "assistant", "content": [
                    {{"type": "tool_use", "id": "toolu_1", "name": "Skill", "input": {{"skill": project[0]}}}}]}}}},
                {{"type": "user", "message": {{"role": "user", "content": [
                    {{"type": "tool_result", "tool_use_id": "toolu_1", "content": "Launching skill"}}]}}}}]
records += [{{"type": "assistant", "message": {{"role": "assistant", "content": [
                {{"type": "text", "text": "Answered with " + token}}]}}}},
            {{"type": "result", "subtype": "success", "is_error": False,
              "result": "Answered.", "total_cost_usd": 0.001}}]
records += json.loads({json.dumps(trailing_records or [])!r})
for record in records:
    print(json.dumps(record))
""", encoding="utf-8")
    path.chmod(0o755)
    return path


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
                "home": sorted(path.name for path in config.iterdir()),
                "mounted": sorted(path.name for path in (config / "skills").iterdir()),
                # What Pi's read/grep/find/ls tools can reach from where it runs.
                "reachable": sorted(path.name for path in cwd.rglob("*")),
            })
            return completed_invocation(pi_stream(PI_STOP))

        with tempfile.TemporaryDirectory() as td:
            user_home = Path(td) / "user-pi"
            user_home.mkdir()
            (user_home / "auth.json").write_text('{"token": "user-token-123"}', encoding="utf-8")
            # Settings and a system prompt change behaviour; only auth is copied.
            (user_home / "settings.json").write_text('{"defaultThinkingLevel": "high"}', encoding="utf-8")
            (user_home / "AGENTS.md").write_text("Always load every skill.\n", encoding="utf-8")
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
        self.assertEqual(seen["home"], ["auth.json", "skills"])
        self.assertTrue(seen["mounted"])
        self.assertNotIn("auth.json", seen["reachable"])
        self.assertNotIn("SKILL.md", seen["reachable"])
        self.assertFalse(seen["config"].exists(), "the Pi home and its copied auth are removed")

    def test_an_agent_home_is_removed_when_the_mounted_tree_fails_its_hash_check(self):
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

    def test_an_agent_home_is_removed_when_the_agent_crashes(self):
        # The home outlives invoke() so its credentials can be scanned for
        # redaction; the cell still removes it when the agent process raises.
        def crash(plan):
            homes.append(Path(dict(plan.environment or {})[home_var]))
            raise RuntimeError("provider unavailable")

        for adapter_cls, home_var in ((tm.PiAdapter, "PI_CODING_AGENT_DIR"),
                                      (tm.CodexAdapter, "CODEX_HOME")):
            homes = []
            with self.subTest(agent=adapter_cls.name), tempfile.TemporaryDirectory() as td, \
                 mock.patch.object(adapter_cls, "_run_argv", staticmethod(crash)):
                tree = Path(td) / "tree"
                (tree / "demo").mkdir(parents=True)
                (tree / "demo" / "SKILL.md").write_text("---\nname: demo\n---\n", encoding="utf-8")
                with self.assertRaisesRegex(RuntimeError, "provider unavailable"):
                    tm.observe_cell_query(adapter_cls(), tree, "q", True, None, 5,
                                          metadata={"skill_tree_hash": sb.skill_tree_hash(tree)})
                self.assertEqual(len(homes), 1)
                self.assertFalse(homes[0].exists())

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

    def test_a_token_the_agent_refreshes_during_the_run_is_redacted(self):
        # Pi and Codex rewrite auth.json in their home when they refresh an
        # OAuth token. The refreshed token exists only in the run's copy of the
        # home, never in the user's source auth, and the model echoes it.
        refreshed = "refreshed-token-written-during-the-run"
        home_vars = {"pi": "PI_CODING_AGENT_DIR", "codex": "CODEX_HOME"}

        def refreshing(agent):
            def run(plan):
                home = Path(dict(plan.environment or {})[home_vars[agent]])
                (home / "auth.json").write_text(json.dumps({"access_token": refreshed}),
                                                encoding="utf-8")
                if agent == "pi":
                    return completed_invocation(pi_stream({"type": "agent_end", "messages": [
                        {"role": "assistant", "stopReason": "stop",
                         "content": [{"type": "text", "text": f"token {refreshed}"}]}]}))
                return completed_invocation(pi_stream(
                    {"type": "thread.started", "thread_id": "t"}, {"type": "turn.started"},
                    {"type": "item.completed", "item": {"id": "item_0", "type": "agent_message",
                                                        "text": f"token {refreshed}"}},
                    {"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 1}}))
            return run

        rows = [{"query_id": "negative", "query": "ordinary chat", "should_trigger": False}]
        for agent, adapter_cls in (("pi", tm.PiAdapter), ("codex", tm.CodexAdapter)):
            with self.subTest(agent=agent), tempfile.TemporaryDirectory() as td:
                source = Path(td) / "user-home"
                source.mkdir()
                (source / "auth.json").write_text(json.dumps({"access_token": "token-before-refresh"}),
                                                  encoding="utf-8")
                traces = Path(td) / "traces"
                with mock.patch.dict(os.environ, {home_vars[agent]: str(source)}), \
                     mock.patch.object(adapter_cls, "_run_argv", staticmethod(refreshing(agent))):
                    report = tm.run_matrix(DEMO_MANIFEST, rows, agents=[agent], models=[None],
                                           runs_per_query=1, timeout=30, workers=1,
                                           trace_runs=traces)
                written = {str(path.relative_to(traces)): path.read_text(encoding="utf-8")
                           for path in traces.rglob("*") if path.is_file()}
                written["report"] = json.dumps(report)
                self.assertTrue(report["results"][0]["observation_complete"])
                for name, text in written.items():
                    with self.subTest(artifact=name):
                        self.assertNotIn(refreshed, text)
                self.assertTrue(any(name.endswith("trace.jsonl") and "[REDACTED]" in text
                                    for name, text in written.items()))

    def test_a_pi_cli_baseline_pairs_with_a_matrix_ablation_at_default_settings(self):
        # skill-pi-trigger-eval is the matrix with the Pi adapter; left at their
        # defaults, the two entry points must run one experimental protocol.
        def stops(plan):
            return completed_invocation(pi_stream(PI_STOP))

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            eval_set = write_rows(root, [{"query_id": "negative", "query": "ordinary chat",
                                          "should_trigger": False}])
            common = ["--eval-set", str(eval_set), "--runs-per-query", "1", "--workers", "1"]
            code, baseline = run_pi_cli(common, stops, root / "baseline.json")
            self.assertEqual(code, 0)
            argv = ["skill-trigger-matrix", str(DEMO_MANIFEST), "--agent", "pi", *common,
                    "--ablation", "weaker-description", "--out", str(root / "ablation.json")]
            with mock.patch.object(sys, "argv", argv), pi_runs(stops), \
                 contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(tm.main(), 0)
            code, stdout, stderr = run_cli("trigger-compare", "--baseline", root / "baseline.json",
                                           "--ablation", root / "ablation.json")
        self.assertEqual(code, 0, stderr)
        self.assertEqual(json.loads(stdout)["provenance"]["reasons"], [])
        self.assertEqual(baseline["protocol"]["timeout_seconds"], 240)


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
        self.assertTrue(any(part.startswith("bad-model-") for part in parts), parts)

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

    def test_models_that_sanitise_alike_keep_separate_trace_directories(self):
        models = ["vendor/model-a", "vendor:model-a"]
        with tempfile.TemporaryDirectory() as td:
            report = tm.run_matrix(DEMO_MANIFEST, demo_trigger_rows()[:1], agents=["stub"],
                                   models=models, runs_per_query=1, timeout=30, workers=1,
                                   trace_runs=Path(td) / "traces")
            recorded = {
                row["model"]: json.loads((Path(row["trace_dir"]) / "metadata.json")
                                         .read_text(encoding="utf-8"))["model"]
                for row in report["results"]}
            trace_dirs = {row["trace_dir"] for row in report["results"]}
        self.assertEqual(len(trace_dirs), 2)
        self.assertEqual(recorded, {model: model for model in models})


class TriggerCliStatusTests(unittest.TestCase):
    def test_matrix_cli_exits_nonzero_for_an_incomplete_report(self):
        # Only the Pi process boundary is replaced: the matrix itself decides
        # that crashed queries leave the measurement incomplete.
        def crash(plan):
            raise RuntimeError("provider unavailable")

        with tempfile.TemporaryDirectory() as td, pi_runs(crash), \
             contextlib.redirect_stdout(io.StringIO()), \
             mock.patch.object(sys, "argv", [
                 "skill-trigger-matrix", str(DEMO_MANIFEST), "--agent", "pi",
                 "--runs-per-query", "1", "--out", str(Path(td) / "report.json"),
             ]):
            self.assertEqual(tm.main(), 1)
            report = json.loads((Path(td) / "report.json").read_text(encoding="utf-8"))
        self.assertEqual(report["summary"]["measurement_status"], "incomplete")

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

    def test_an_empty_model_is_an_argument_error_not_a_traceback(self):
        def unreachable(plan):
            raise AssertionError("no agent may run with an empty model")

        with tempfile.TemporaryDirectory() as td:
            out = str(Path(td) / "report.json")
            for main, argv in (
                    (tm.main, ["skill-trigger-matrix", str(DEMO_MANIFEST), "--agent", "pi",
                               "--model", "", "--out", out]),
                    (tr.main, ["skill-pi-trigger-eval", str(DEMO_MANIFEST), "--model", "",
                               "--out", out])):
                with self.subTest(argv[0]):
                    with mock.patch.object(sys, "argv", argv), pi_runs(unreachable), \
                         self.assertRaises(SystemExit) as ctx:
                        main()
                    self.assertIn("must be None or a non-empty string", str(ctx.exception.code))


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
            result = tm.ClaudeAdapter().invoke("q", "haiku", Path(td) / "run", 1)
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
                result = tm.ClaudeAdapter().invoke("q", "haiku", Path(td) / "run", 1)
            self.assertIs(result.state, state)

    def test_mounted_skill_names_read_frontmatter(self):
        with tempfile.TemporaryDirectory() as td:
            skill_md = Path(td) / "some-dir" / "SKILL.md"
            skill_md.parent.mkdir()
            skill_md.write_text("---\nname: demo-reviewer\ndescription: x\n---\n", encoding="utf-8")
            self.assertEqual(tm.mounted_skill_names([skill_md]), ["demo-reviewer", "some-dir"])

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
        self.assertFalse(seen["config_dir"].is_relative_to(root / "run"))
        self.assertTrue(result.metadata["config_isolated"])
        self.assertNotIn("config_isolation_warning", result.metadata)

    def test_claude_invoke_preserves_nonportable_oauth_config(self):
        seen = {}

        def fake_run(plan):
            env = dict(plan.environment or {})
            seen["config_dir"] = env.get("CLAUDE_CONFIG_DIR")
            seen["sync_skills"] = env.get("CLAUDE_CODE_SYNC_SKILLS")
            return completed_invocation(json.dumps({"type": "result", "subtype": "success"}) + "\n")

        with tempfile.TemporaryDirectory() as td, mock.patch.object(tm.ClaudeAdapter, "_run_argv", staticmethod(fake_run)):
            root = Path(td)
            source = root / "oauth-backed-claude"
            source.mkdir()
            workspace = root / "run"
            env = {"CLAUDE_CONFIG_DIR": str(source), "CLAUDE_CODE_SYNC_SKILLS": "1"}
            with mock.patch.dict(os.environ, env, clear=True):
                result = tm.ClaudeAdapter().invoke("q", "haiku", workspace, 12)
        self.assertEqual(seen["config_dir"], str(source))
        self.assertFalse(tm.ClaudeAdapter._config_dir(workspace).exists())
        self.assertFalse(result.metadata["config_isolated"])
        self.assertIn("personal config may influence", result.metadata["config_isolation_warning"])
        # Not isolated: the run is left as the user's own CLI would see it, and
        # a stream with no init skill list is no evidence about competitors.
        self.assertEqual(seen["sync_skills"], "1")
        self.assertNotIn("competing_skills", result.metadata)

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
            result = tm.ClaudeAdapter().invoke("q", "haiku", Path(td) / "run", 1)
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
            result = tm.ClaudeAdapter().invoke("q", None, Path(td) / "run", 1)
        self.assertIs(result.state, InvocationState.COMPLETE)
        self.assertIsNone(result.provider_error)
        detection = self._adapter().detect(result, ["probe-plugin:tidy-commit"], [])
        self.assertEqual(detection.legacy_evidence, ["Skill tool invoked: probe-plugin:tidy-commit"])
        # Every recording that continues after `result` is a complete
        # observation too; the skills it invoked are read off the stream itself.
        for source, text in claude_streams_ending_after_result():
            invoked = [str(block["input"]["skill"])
                       for record in map(json.loads, filter(str.strip, text.splitlines()))
                       if record.get("type") == "assistant"
                       for block in record["message"]["content"]
                       if block.get("type") == "tool_use" and block.get("name") == "Skill"]
            with self.subTest(source=source), tempfile.TemporaryDirectory() as td, \
                 mock.patch.object(tm.ClaudeAdapter, "_run_argv", staticmethod(
                     lambda plan, text=text: completed_invocation(text))), \
                 mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}, clear=True):
                result = tm.ClaudeAdapter().invoke("q", None, Path(td) / "run", 1)
                self.assertIs(result.state, InvocationState.COMPLETE, result.provider_error)
                self.assertIsNone(result.provider_error)
                detection = self._adapter().detect(result, invoked, [])
                self.assertEqual(detection.legacy_evidence, [f"Skill tool invoked: {name}" for name in invoked][:5])

    def test_skill_tool_called_by_mounted_directory_name_is_trigger_evidence(self):
        # Claude Code 2.1.269 invokes a project skill by the directory it is
        # mounted under, `demo` for skills/demo/SKILL.md, not by its declared
        # name `demo-reviewer` (#85 recorded 0/3 should-fire on Haiku and Sonnet
        # for a skill a traced run showed being invoked). The flattened mount
        # key of earlier versions is not a name anything is mounted under now.
        def invoking(skill):
            records = [
                {"type": "system", "subtype": "init", "session_id": "s"},
                {"type": "assistant", "message": {"role": "assistant", "content": [
                    {"type": "tool_use", "id": "toolu_1", "name": "Skill", "input": {"skill": skill}}]}},
                {"type": "user", "message": {"role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": "toolu_1", "content": "Launching skill"}]}},
                {"type": "assistant", "message": {"role": "assistant", "content": [
                    {"type": "text", "text": "Reviewed."}]}},
                {"type": "result", "subtype": "success", "is_error": False, "result": "Reviewed."},
            ]
            stdout = "".join(json.dumps(record) + "\n" for record in records)
            return lambda plan: completed_invocation(stdout)

        should_fire = [row for row in demo_trigger_rows() if row["should_trigger"]][:1]
        for skill, triggered in (("demo", True), ("skills_demo_SKILL.md", False), ("other", False)):
            with self.subTest(skill=skill), \
                 mock.patch.object(tm.ClaudeAdapter, "_run_argv", staticmethod(invoking(skill))), \
                 mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}):
                report = tm.run_matrix(DEMO_MANIFEST, should_fire, agents=["claude"], models=["haiku"],
                                       runs_per_query=1, timeout=30, workers=1)
                (row,) = report["results"]
                self.assertEqual(report["summary"]["measurement_status"], "complete")
                self.assertIs(row["triggered"], triggered)
                self.assertEqual(row["evidence"], [f"Skill tool invoked: {skill}"] if triggered else [])

    def test_claude_auth_tokens_from_the_environment_are_redacted(self):
        # Claude Code authenticates from these variables too; a CLI that
        # prints one (a debug line, an auth error) must not write it out.
        tokens = {"CLAUDE_CODE_OAUTH_TOKEN": "oauth-token-from-setup-token",
                  "ANTHROPIC_AUTH_TOKEN": "bearer-token-for-a-gateway"}

        def leaky(plan):
            env = dict(plan.environment or {})
            echoed = " ".join(env[name] for name in tokens)
            stdout = json.dumps({"type": "result", "subtype": "success", "result": echoed}) + "\n"
            return InvocationOutcome.from_process(stdout=stdout, stderr=f"auth: {echoed}",
                                                  returncode=0, elapsed_ms=1)

        rows = [{"query_id": "negative", "query": "ordinary chat", "should_trigger": False}]
        with tempfile.TemporaryDirectory() as td, \
             mock.patch.object(tm.ClaudeAdapter, "_run_argv", staticmethod(leaky)), \
             mock.patch.dict(os.environ, {**tokens, "CLAUDE_CONFIG_DIR": str(Path(td) / "no-config")}):
            traces = Path(td) / "traces"
            report = tm.run_matrix(DEMO_MANIFEST, rows, agents=["claude"], models=["haiku"],
                                   runs_per_query=1, timeout=30, workers=1, trace_runs=traces)
            written = {str(path.relative_to(traces)): path.read_text(encoding="utf-8")
                       for path in traces.rglob("*") if path.is_file()}
        written["report"] = json.dumps(report)
        self.assertEqual(report["results"][0]["stderr"], "auth: [REDACTED] [REDACTED]")
        for name, text in written.items():
            for token in tokens.values():
                with self.subTest(artifact=name, token=token):
                    self.assertNotIn(token, text)

    def test_copied_claude_credentials_sit_outside_the_working_directory(self):
        # Claude runs with Read and Glob from its working directory, so the
        # config dir holding the copied OAuth credentials must not be in it.
        seen = {}

        def fake_run(plan):
            config, cwd = Path(dict(plan.environment or {})["CLAUDE_CONFIG_DIR"]), Path(plan.cwd)
            seen.update({
                "config": config, "cwd": cwd,
                "credentials": (config / ".credentials.json").read_text(encoding="utf-8"),
                "reachable": sorted(path.name for path in cwd.rglob("*")),
            })
            return completed_invocation(
                json.dumps({"type": "result", "subtype": "success", "result": "ok"}) + "\n")

        rows = [{"query_id": "negative", "query": "ordinary chat", "should_trigger": False}]
        with tempfile.TemporaryDirectory() as td, \
             mock.patch.object(tm.ClaudeAdapter, "_run_argv", staticmethod(fake_run)):
            source = Path(td) / "user-claude"
            source.mkdir()
            (source / ".credentials.json").write_text(
                '{"claudeAiOauth": {"accessToken": "user-oauth-access-token"}}', encoding="utf-8")
            paths = {}
            with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(source)}):
                for arm, ablation in (("baseline", None), ("ablation", "weaker-description")):
                    report = tm.run_matrix(DEMO_MANIFEST, rows, agents=["claude"], models=["haiku"],
                                           runs_per_query=1, timeout=30, workers=1, ablation=ablation)
                    paths[arm] = Path(td) / f"{arm}.json"
                    paths[arm].write_text(json.dumps(report), encoding="utf-8")
            code, stdout, stderr = run_cli("trigger-compare", "--baseline", paths["baseline"],
                                           "--ablation", paths["ablation"])
        self.assertIn("user-oauth-access-token", seen["credentials"])
        self.assertFalse(seen["config"].is_relative_to(seen["cwd"]))
        self.assertNotIn(".credentials.json", seen["reachable"])
        self.assertFalse(seen["config"].exists(), "the copied config is removed with the cell")
        self.assertEqual(report["results"][0]["protocol_observation"],
                         {"config_isolated": True, "claude_config_outside_workdir": True})
        # trigger-compare requires the same controls the adapter declares.
        self.assertEqual(code, 0, stderr)
        self.assertEqual(json.loads(stdout)["paired"]["blocked"], [])

    def test_env_auth_isolates_the_claude_config_so_trigger_compare_accepts_the_cells(self):
        # CI logs Claude in through an environment variable, with no credentials
        # file to copy. An empty config dir still authenticates then, so the run
        # is isolated: the user's personal skill and the organisation's synced
        # skills must not compete with the skill under test.
        for var in ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY"):
            with self.subTest(auth=var), tempfile.TemporaryDirectory() as td:
                root = Path(td)
                token = f"{var.lower()}-secret-value"
                personal = root / "user-claude"
                (personal / "skills" / "personal-helper").mkdir(parents=True)
                probe = root / "probe.jsonl"
                claude = fake_trigger_claude(root / "claude", probe)
                env = {"PATH": os.environ.get("PATH", ""), "HOME": str(root), var: token,
                       "CLAUDE_CONFIG_DIR": str(personal), "CLAUDE_CODE_SYNC_SKILLS": "1"}
                reports = {}
                with mock.patch.dict(os.environ, env, clear=True), \
                     contextlib.redirect_stdout(io.StringIO()):
                    for arm, extra in (("baseline", []), ("ablation", ["--ablation", "weaker-description"])):
                        reports[arm] = root / f"{arm}.json"
                        with mock.patch.object(sys, "argv", [
                                "skill-trigger-matrix", str(DEMO_MANIFEST), "--agent", "claude",
                                "--model", "haiku", "--runs-per-query", "1", "--workers", "1",
                                "--claude-bin", str(claude), "--trace-runs", str(root / "traces" / arm),
                                "--out", str(reports[arm]), *extra]):
                            self.assertEqual(tm.main(), 0)
                    code, stdout, stderr = run_cli("trigger-compare", "--baseline", reports["baseline"],
                                                   "--ablation", reports["ablation"])
                rows = [row for path in reports.values()
                        for row in json.loads(path.read_text(encoding="utf-8"))["results"]]
                runs = [json.loads(line) for line in probe.read_text(encoding="utf-8").splitlines()]
                artifacts = {str(path): path.read_text(encoding="utf-8")
                             for path in [*reports.values(), *(root / "traces").rglob("*")] if path.is_file()}
                self.assertTrue(rows)
                for row in rows:
                    self.assertEqual(row["protocol_observation"],
                                     {"config_isolated": True, "claude_config_outside_workdir": True})
                    self.assertNotIn("config_isolation_warning", row)
                    # Only the skill bundled with the CLI competed with the mounted one.
                    self.assertEqual(row.get("competing_skills"), ["update-config"])
                self.assertEqual(len(runs), len(rows))
                for run in runs:
                    config = Path(run["config"])
                    self.assertNotEqual(config, personal)
                    self.assertFalse(config.is_relative_to(Path(run["cwd"])))
                    self.assertFalse(config.exists(), "the isolated config is removed with the cell")
                    self.assertFalse(run["sync_skills"])
                self.assertEqual(code, 0, stderr)
                self.assertEqual(json.loads(stdout)["paired"]["blocked"], [])
                for name, text in artifacts.items():
                    self.assertNotIn(token, text, name)

    def test_the_model_is_offered_the_skill_under_its_own_directory_name(self):
        # A user who installs skills/demo/ sees a skill named `demo`; the trigger
        # run must offer the model that name, not the flattened manifest path.
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            probe = root / "probe.jsonl"
            claude = fake_trigger_claude(root / "claude", probe, invoke_project_skill=True)
            out = root / "report.json"
            with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}), \
                 contextlib.redirect_stdout(io.StringIO()), \
                 mock.patch.object(sys, "argv", [
                     "skill-trigger-matrix", str(DEMO_MANIFEST), "--agent", "claude", "--model", "haiku",
                     "--runs-per-query", "1", "--workers", "1", "--claude-bin", str(claude),
                     "--out", str(out)]):
                self.assertEqual(tm.main(), 0)
            report = json.loads(out.read_text(encoding="utf-8"))
            offered = [json.loads(line)["skills"] for line in probe.read_text(encoding="utf-8").splitlines()]
        self.assertTrue(offered)
        for skills in offered:
            self.assertEqual(skills, ["update-config", "demo"])
        for row in report["results"]:
            self.assertEqual(row["evidence"], ["Skill tool invoked: demo"])
            self.assertIs(row["trigger_evidence_observed"], True)

    def test_the_trigger_adapter_applies_the_answer_parsers_rule_after_the_result(self):
        # The matrix reads Claude's stream by the rule the answer parser and the
        # trace dialect share: metadata after the one `result` keeps the cell a
        # complete observation; a second result or a late turn leaves it
        # incomplete, so it cannot count as a trigger or a miss.
        should_fire = [row for row in demo_trigger_rows() if row["should_trigger"]][:1]
        for label, trailing, allowed in CLAUDE_POST_RESULT_RECORDS:
            with self.subTest(trailing=label), tempfile.TemporaryDirectory() as td, \
                 mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}):
                claude = fake_trigger_claude(Path(td) / "claude", Path(td) / "probe.jsonl",
                                             invoke_project_skill=True, trailing_records=[trailing])
                report = tm.run_matrix(DEMO_MANIFEST, should_fire, agents=["claude"], models=["haiku"],
                                       runs_per_query=1, timeout=30, workers=1, claude_bin=str(claude))
                row = report["results"][0]
                self.assertIs(row["observation_complete"], allowed, row.get("provider_error"))
                if allowed:
                    self.assertEqual((row["triggered"], row["evidence"]), (True, ["Skill tool invoked: demo"]))
                    self.assertEqual(report["summary"]["measurement_status"], "complete")
                else:
                    self.assertEqual(row["provider_error"],
                                     "Claude JSON stream must contain exactly one terminal result event, "
                                     "with no session content after it")
                    self.assertEqual(report["summary"]["measurement_status"], "incomplete")


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

    def test_codex_baseline_and_ablation_reports_pair_in_trigger_compare(self):
        # A fake `codex exec --json` that ends its turn without loading the skill.
        fake_codex = (
            "import json\n"
            "for record in ({'type': 'thread.started', 'thread_id': 't'}, {'type': 'turn.started'},\n"
            "               {'type': 'item.completed', 'item': {'id': 'i', 'type': 'agent_message', 'text': 'ok'}},\n"
            "               {'type': 'turn.completed', 'usage': {'input_tokens': 1, 'output_tokens': 1}}):\n"
            "    print(json.dumps(record))\n")
        rows = [{"query_id": "negative", "query": "ordinary chat", "should_trigger": False}]
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "fake_codex.py").write_text(fake_codex, encoding="utf-8")
            user_codex = root / "user-codex"
            user_codex.mkdir()
            (user_codex / "auth.json").write_text('{"token": "codex-user-token"}', encoding="utf-8")
            paths = {}
            with mock.patch.dict(os.environ, {"CODEX_HOME": str(user_codex)}):
                for arm, ablation in (("baseline", None), ("ablation", "weaker-description")):
                    report = tm.run_matrix(
                        DEMO_MANIFEST, rows, agents=["codex"], models=[None], runs_per_query=1,
                        timeout=30, workers=1, codex_cmd=f"{sys.executable} {root / 'fake_codex.py'}",
                        ablation=ablation)
                    self.assertEqual(report["summary"]["measurement_status"], "complete")
                    paths[arm] = root / f"{arm}.json"
                    paths[arm].write_text(json.dumps(report), encoding="utf-8")
            code, stdout, stderr = run_cli("trigger-compare", "--baseline", paths["baseline"],
                                           "--ablation", paths["ablation"])
        self.assertEqual(code, 0, stderr)
        compared = json.loads(stdout)
        self.assertEqual(compared["paired"]["blocked"], [])
        self.assertTrue(compared["provenance"]["verified"])


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

    def test_a_vibe_2_23_history_entry_stream_is_a_complete_cell_with_skill_tool_evidence(self):
        # Vibe 2.23 and later write public history entries (built from Vibe
        # 2.25.8's own code, tests/fixtures/vibe/README.md). A stream that
        # loads the mounted `demo` skill triggers; one that answers without a
        # tool is a complete observation that did not trigger.
        fixtures = ROOT / "tests" / "fixtures" / "vibe"
        rows = [{"query_id": "q", "query": "Review this pull request description.", "should_trigger": True}]
        for name, triggered, evidence in (
                ("streaming.2.25.8.skill-load.jsonl", True, ["Vibe skill tool invoked: demo"]),
                ("streaming.2.25.8.no-tools.jsonl", False, [])):
            with self.subTest(fixture=name), tempfile.TemporaryDirectory() as td:
                fake_vibe = Path(td) / "fake_vibe.py"
                fake_vibe.write_text(
                    f"import sys\nsys.stdout.write(open({str(fixtures / name)!r}, encoding='utf-8').read())\n",
                    encoding="utf-8")
                report = tm.run_matrix(DEMO_MANIFEST, rows, agents=["vibe"], models=[None],
                                       runs_per_query=1, timeout=30, workers=1,
                                       vibe_cmd=f"{sys.executable} {fake_vibe}")
                row = report["results"][0]
                self.assertIs(row["observation_complete"], True, row.get("provider_error"))
                self.assertEqual((row["triggered"], row["evidence"]), (triggered, evidence))


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
                        try:
                            copied = adapter.mount(tree, workspace)
                            result = tm.validate_invoke_result(
                                agent_name,
                                adapter.invoke(query, model, workspace, timeout),
                            )
                        finally:
                            adapter.release(workspace)
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
    """A Pi report declares the isolation controls it ran under, and only the
    controls the adapter requires today are accepted. Reports from before Pi's
    home moved out of its working directory carry an older harness identity
    and are refused before this check."""

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

    def test_any_other_control_set_is_refused(self):
        for required in ({"config_isolated": True}, {"pi_home_outside_workdir": True}):
            stderr = io.StringIO()
            with self.subTest(required=required), contextlib.redirect_stderr(stderr), \
                    self.assertRaises(SystemExit):
                self.validate(required)
            self.assertIn("must require", stderr.getvalue())

    def test_a_report_from_an_older_harness_identity_is_refused_by_name(self):
        identity = sb.trigger_harness_identity()
        payload = {key: value for key, value in identity.items() if key != "identity_sha256"}
        payload["schema_version"] = sb.TRIGGER_HARNESS_IDENTITY_VERSION - 1
        older = {**payload, "identity_sha256": sb.canonical_json_sha256(payload)}
        with self.assertRaisesRegex(ValueError, "identity v2; this harness reads v3. Regenerate"):
            sb.validate_trigger_harness_identity(older, "baseline")


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

    def test_a_baseline_that_contradicts_itself_is_rejected_by_its_guard(self):
        def baseline(rows=None, **report):
            return trigger_report(
                self._baseline_rows() if rows is None else rows, **report)

        def changed(mutate, *, rehash_protocol=False):
            def make():
                report = baseline()
                mutate(report)
                if rehash_protocol:
                    report["protocol_sha256"] = sb.canonical_json_sha256(report["protocol"])
                    for row in report["results"]:
                        row["protocol_sha256"] = report["protocol_sha256"]
                return report
            return make

        def omit_identity_module(report):
            identity = report["protocol"]["harness_identity"]
            self.assertIn("trigger_reporting.py", identity["modules"])
            identity["modules"].pop("trigger_reporting.py")
            identity["identity_sha256"] = sb.canonical_json_sha256(
                {key: value for key, value in identity.items() if key != "identity_sha256"})

        rejected = {
            # label: (the baseline report, the guard's message)
            "a declared repetition is missing": (
                lambda: baseline([trigger_row("short", True, triggered=True, run_number=1)]),
                "--baseline has incomplete repetition identities for (stub, None, short): expected [1, 2], got [1]"),
            "a declared cell has no results": (
                changed(lambda r: r.update(results=[row for row in r["results"]
                                                    if row["query_id"] != self.QUERIES[0]])),
                "--baseline has incomplete repetition identities for (stub, None, query 1): expected [1, 2], got []"),
            "a repetition is recorded twice": (
                lambda: baseline([trigger_row("duplicate", True, triggered=True, run_number=n)
                                  for n in (1, 1, 2)]),
                "--baseline duplicates repetition 1 for (stub, None, duplicate)"),
            "the design omits a result cell": (
                changed(lambda r: r.update(design=r["design"][1:])),
                "--baseline results row 1 is not present in the declared design"),
            "the protocol declares other repetitions": (
                changed(lambda r: r["protocol"].update(runs_per_query=999), rehash_protocol=True),
                "--baseline protocol runs_per_query disagrees with its report"),
            "the protocol ran an agent the design never declared": (
                changed(lambda r: r["protocol"]["adapters"][0].update(
                    agent="other", trace_dialect="other"), rehash_protocol=True),
                "--baseline protocol agent/model design disagrees with its report"),
            "the harness identity omits a module": (
                changed(omit_identity_module, rehash_protocol=True),
                "harness_identity must identify exactly"),
            "cosmetic aliases of one query": (
                lambda: baseline([trigger_row("identical prompt" + " " * n, True, triggered=True,
                                              query_id=f"q{n}") for n in range(6)],
                                 runs_per_query=1),
                "--baseline design canonical query aliases must share one query ID and polarity"),
            "a row records another tree": (
                changed(lambda r: r["results"][0].update(skill_tree_hash="sha256:other")),
                "--baseline results row 1: skill_tree_hash disagrees with its report"),
            "a row's pass contradicts its observation": (
                changed(lambda r: r["results"][0].update({"pass": True, "triggered": False})),
                "--baseline results row 1: persisted triggered flag disagrees with the typed observation"),
            "the baseline declares an ablation": (
                lambda: baseline(ablation="drop-description", provenance=ABLATION_PROVENANCE),
                "--baseline must be an unablated trigger run (it declares an ablation)"),
            "an answer report": (
                changed(lambda r: r.update(evidence_class="answer")),
                "--baseline is not a skill-trigger-matrix report"),
        }
        for label, (make_baseline, message) in rejected.items():
            with self.subTest(label):
                ablation = trigger_report(
                    self._ablation_rows(), ablation="drop-description",
                    provenance=ABLATION_PROVENANCE, tree_hash=EDIT_HASH)
                compare = functools.partial(sb.build_trigger_comparison, make_baseline(), ablation)
                assert_dies(self, compare, message)

    def test_reports_from_the_direct_script_entry_point_pair(self):
        # examples/demo-skill/README.md runs `python3 ../../run_trigger_matrix.py`,
        # where the adapters are defined in `__main__`.
        with tempfile.TemporaryDirectory() as td:
            paths = {}
            for arm, extra in (("baseline", []), ("ablation", ["--ablation", "weaker-description"])):
                paths[arm] = Path(td) / f"{arm}.json"
                subprocess.run(
                    [sys.executable, str(ROOT / "run_trigger_matrix.py"),
                     "evals/shared-benchmark.json", "--agent", "stub", "--runs-per-query", "1",
                     *extra, "--out", str(paths[arm])],
                    cwd=DEMO_MANIFEST.parents[1], check=True, capture_output=True)
            adapter = json.loads(paths["baseline"].read_text(encoding="utf-8"))["protocol"]["adapters"][0]
            code, stdout, stderr = run_cli("trigger-compare", "--baseline", paths["baseline"],
                                           "--ablation", paths["ablation"])
        self.assertEqual(code, 0, stderr)
        self.assertTrue(json.loads(stdout)["provenance"]["verified"])
        self.assertEqual(adapter["adapter"], "run_trigger_matrix.StubAdapter")


if __name__ == "__main__":
    unittest.main()
