"""Runner adapters and their shared contracts: subagent seam, tool replay, failure markers, trigger detection, trace normalization.

Classes moved verbatim from the PR-named test files (test_audit_fixes,
test_roadmap_features, test_followup_features, test_external_review_gaps,
test_cbc) and test_skill_benchmark, which accreted by merge rather than by
subject; docstrings citing finding/roadmap ids are preserved.
"""
import argparse
import errno
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from helpers import (
    CONTAINS_APPROVED_CASE as CASE,
)
from helpers import (
    demo_manifest as base_manifest,
)
from helpers import (
    good_pr_manifest as _manifest,
)
from helpers import (
    make_eval_repo,
    stub_claude,
)
from helpers import (
    write_demo_manifest as write_manifest,
)
from helpers import (
    write_good_pr_skill as _skill,
)

import ablation_model as am
import run_pi_trigger_eval as tr
import runner_contracts as rc
import skill_benchmark as sb
import trace_contracts as tc

ROOT = Path(__file__).resolve().parents[1]


def make_tasks(root: Path) -> list[dict]:
    """Prepared rows over the demo manifest. Module-level so ToolReplayTests
    never instantiates SubagentRunnerTests to borrow it."""
    path = write_manifest(root, base_manifest())
    manifest = sb.validate_manifest(path)
    return sb.prepared_task_rows(path, manifest, split="tune")


class SubagentRunnerTests(unittest.TestCase):
    """2.7 — the built-in subagent runner writes the run-output contract."""

    def test_mock_subagent_writes_the_contract(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            tasks = make_tasks(root)
            runs = root / "runs"
            seen_prompts: list[str] = []

            def agent(*, prompt, workspace, model, tool_executor):
                seen_prompts.append(prompt)
                return {"answer": "alpha response", "usage": {"total_tokens": 42},
                        "trace": [{"type": "command", "command": "ls", "status": "completed"}]}

            rc = sb.run_subagent_tasks(tasks, runs, agent, model="sub-model")
            self.assertEqual(rc, 0)
            for variant in ["with_skill", "without_skill"]:
                base = runs / "case-1" / variant
                self.assertEqual((base / "output.md").read_text(encoding="utf-8"), "alpha response")
                meta = json.loads((base / "metadata.json").read_text(encoding="utf-8"))
                self.assertEqual(meta["provider"], "subagent")
                self.assertEqual(meta["model"], "sub-model")
                events = json.loads((base / "events.json").read_text(encoding="utf-8"))
                self.assertEqual(events["source"], "subagent")
                metrics = json.loads((base / "metrics.json").read_text(encoding="utf-8"))
                self.assertEqual(metrics["total_tokens"], 42)
                # The subagent now re-serializes its records to trace.jsonl (the
                # shared writer's raw-trace artifact), like every other runner.
                self.assertTrue((base / "trace.jsonl").exists())
        without_prompt = next(p for p in seen_prompts if "Do not use any skill" in p)
        self.assertNotIn("skills/", without_prompt)

    def test_subagent_failure_writes_failure_marker(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            tasks = make_tasks(root)[:1]

            def exploding(*, prompt, workspace, model, tool_executor):
                raise RuntimeError("backend down")

            sb.run_subagent_tasks(tasks, root / "runs", exploding)
            base = root / "runs" / "case-1" / "with_skill"
            self.assertIn(str(sb.CLAUDE_FAILURE), (base / "output.md").read_text(encoding="utf-8"))
            meta = json.loads((base / "metadata.json").read_text(encoding="utf-8"))
            self.assertEqual(meta["returncode"], 1)

    def test_multiturn_commits_each_turn_and_sums_explicit_deltas(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            tasks = make_tasks(root)[:1]
            tasks[0]["turns"] = ["first", "second"]

            def agent(*, prompt, workspace, model, tool_executor, history=None):
                n = len(history or []) + 1
                return {
                    "answer": f"answer-{n}",
                    "trace": [{
                        "type": "command", "command": f"step-{n}",
                        "status": "completed", "usage": {"total_tokens": 999},
                        "elapsed_ms": 999,
                    }],
                    "usage": {
                        "input_tokens": n + 1, "output_tokens": n,
                        "cost_usd": n / 10,
                    },
                    "elapsed_ms": n * 10,
                    "telemetry_scope": "turn_delta",
                }

            sb.run_subagent_tasks(tasks, root / "runs", agent)
            base = root / "runs" / "case-1" / "with_skill"
            metadata = json.loads((base / "metadata.json").read_text(encoding="utf-8"))
            metrics = json.loads((base / "metrics.json").read_text(encoding="utf-8"))
            root_trace = [json.loads(line) for line in
                          (base / "trace.jsonl").read_text(encoding="utf-8").splitlines()]

            self.assertEqual((base / "output.md").read_text(encoding="utf-8"), "answer-2")
            self.assertEqual(metadata["elapsed_ms"], 30)
            self.assertEqual(metrics["input_tokens"], 5)
            self.assertEqual(metrics["output_tokens"], 3)
            self.assertEqual(metrics["total_tokens"], 8)
            self.assertAlmostEqual(metrics["cost_usd"], 0.3)
            self.assertEqual([row["_subagent_turn"] for row in root_trace], [1, 2])
            self.assertTrue(all("usage" not in row and "elapsed_ms" not in row
                                for row in root_trace))
            summary = metadata["multi_turn_telemetry"]
            self.assertTrue(summary["run_complete"])
            for channel in ("elapsed", "usage", "cost", "trace"):
                self.assertEqual(summary[channel]["availability"], "complete")
            for n, answer in ((1, "answer-1"), (2, "answer-2")):
                turn = base / f"turn-{n}"
                self.assertEqual((turn / "output.md").read_text(encoding="utf-8"), answer)
                self.assertTrue((turn / "trace.jsonl").is_file())
                self.assertTrue((turn / sb.ARTIFACT_COMMIT_NAME).is_file())
                turn_meta = json.loads((turn / "metadata.json").read_text(encoding="utf-8"))
                self.assertEqual(turn_meta["returncode"], 0)
                self.assertEqual(turn_meta["billing_scope"], "turn")
                self.assertEqual(turn_meta["turn_number"], n)

    def test_multiturn_mixed_or_cumulative_telemetry_stays_partial(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            tasks = make_tasks(root)[:1]
            tasks[0]["turns"] = ["first", "second"]

            def agent(*, prompt, workspace, model, tool_executor, history=None):
                n = len(history or []) + 1
                return {
                    "answer": f"answer-{n}",
                    "trace": [{"type": "command", "command": f"step-{n}",
                               "status": "completed"}],
                    "usage": {"total_tokens": 5 if n == 1 else 9,
                              "cost_usd": 0.1 if n == 1 else 0.3},
                    "elapsed_ms": n * 10,
                    "telemetry_scope": ("turn_delta" if n == 1
                                        else "conversation_cumulative"),
                }

            sb.run_subagent_tasks(tasks, root / "runs", agent)
            base = root / "runs" / "case-1" / "with_skill"
            metadata = json.loads((base / "metadata.json").read_text(encoding="utf-8"))
            metrics = json.loads((base / "metrics.json").read_text(encoding="utf-8"))
            summary = metadata["multi_turn_telemetry"]

            self.assertTrue(summary["run_complete"])
            self.assertFalse(summary["delta_semantics_complete"])
            for channel in ("elapsed", "usage", "cost", "trace"):
                self.assertEqual(summary[channel]["availability"], "partial")
            self.assertEqual(summary["usage"]["observed_delta_totals"]["total_tokens"], 5)
            self.assertEqual(metadata["usage_normalized"]["source"], "missing")
            self.assertEqual(metadata["cost_normalized"]["source"], "missing")
            self.assertNotIn("total_tokens", metrics)
            self.assertNotIn("cost_usd", metrics)
            self.assertNotIn("elapsed_ms", metrics)
            self.assertFalse(metrics["trace_observation_complete"])
            # The unaggregated cumulative provider values remain inspectable.
            self.assertEqual(json.loads(
                (base / "turn-2" / "metrics.json").read_text(encoding="utf-8")
            )["total_tokens"], 9)

    def test_multiturn_unspecified_telemetry_is_unavailable_not_final_turn(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            tasks = make_tasks(root)[:1]
            tasks[0]["turns"] = ["first", "second"]

            def agent(*, prompt, workspace, model, tool_executor, history=None):
                n = len(history or []) + 1
                # These could be either turn deltas or conversation-cumulative
                # counters. Absence of an explicit scope makes either sum or
                # final-turn overwrite unsafe.
                return {
                    "answer": f"answer-{n}",
                    "trace": [{"type": "command", "command": f"step-{n}",
                               "status": "completed"}],
                    "usage": {"total_tokens": 10 * n, "cost_usd": n / 10},
                    "elapsed_ms": n * 10,
                }

            sb.run_subagent_tasks(tasks, root / "runs", agent)
            base = root / "runs" / "case-1" / "with_skill"
            metadata = json.loads((base / "metadata.json").read_text(encoding="utf-8"))
            metrics = json.loads((base / "metrics.json").read_text(encoding="utf-8"))
            summary = metadata["multi_turn_telemetry"]

            for channel in ("elapsed", "usage", "cost", "trace"):
                self.assertEqual(summary[channel]["availability"], "unavailable")
            self.assertEqual(metadata["usage_normalized"]["source"], "missing")
            self.assertEqual(metadata["cost_normalized"]["source"], "missing")
            self.assertNotIn("total_tokens", metrics)
            self.assertNotIn("elapsed_ms", metrics)
            self.assertEqual(json.loads(
                (base / "turn-1" / "metrics.json").read_text(encoding="utf-8")
            )["total_tokens"], 10)
            self.assertEqual(json.loads(
                (base / "turn-2" / "metrics.json").read_text(encoding="utf-8")
            )["total_tokens"], 20)

    def test_multiturn_failure_is_fatal_without_erasing_prior_turns(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            tasks = make_tasks(root)[:1]
            tasks[0]["turns"] = ["first", "second", "must-not-run"]
            calls: list[int] = []

            def agent(*, prompt, workspace, model, tool_executor, history=None):
                n = len(calls) + 1
                calls.append(n)
                common = {
                    "trace": [{"type": "command", "command": f"step-{n}",
                               "status": "completed" if n == 1 else "failed"}],
                    "usage": {"total_tokens": n, "cost_usd": n / 10},
                    "elapsed_ms": n * 10,
                    "telemetry_scope": "turn_delta",
                }
                return ({"answer": "first-answer", **common} if n == 1 else
                        {"answer": "ignored partial answer", "returncode": 7, **common})

            sb.run_subagent_tasks(tasks, root / "runs", agent)
            base = root / "runs" / "case-1" / "with_skill"
            metadata = json.loads((base / "metadata.json").read_text(encoding="utf-8"))
            metrics = json.loads((base / "metrics.json").read_text(encoding="utf-8"))

            self.assertEqual(calls, [1, 2])
            self.assertEqual((base / "turn-1" / "output.md").read_text(encoding="utf-8"),
                             "first-answer")
            self.assertIn("subagent turn 2 did not complete",
                          (base / "turn-2" / "output.md").read_text(encoding="utf-8"))
            self.assertFalse((base / "turn-3").exists())
            self.assertIn("subagent turn 2 did not complete",
                          (base / "output.md").read_text(encoding="utf-8"))
            self.assertEqual(metadata["returncode"], 7)
            summary = metadata["multi_turn_telemetry"]
            self.assertFalse(summary["run_complete"])
            self.assertEqual(summary["attempted_turns"], 2)
            self.assertEqual(summary["completed_turns"], 1)
            self.assertEqual(summary["usage"]["availability"], "partial")
            self.assertEqual(metadata["usage_normalized"]["source"], "missing")
            self.assertFalse(metrics["operation_observation_complete"])

    def test_agent_backends_are_registered_workspace_builders(self):
        for name in ("subagent", "codex", "claude", "vibe"):
            self.assertIn(name, sb.WORKSPACE_BUILDERS)


class ToolReplayTests(unittest.TestCase):
    """2.3 — record/replay of tool I/O for deterministic re-runs."""

    def agent_using_tools(self, replies: list):
        def agent(*, prompt, workspace, model, tool_executor):
            a = tool_executor("search", {"q": "alpha"})
            b = tool_executor("search", {"q": "beta"})
            replies.append((a, b))
            return {"answer": f"{a} then {b}"}
        return agent

    def test_record_then_replay_round_trip_is_identical(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            tasks = make_tasks(root)[:1]
            runs = root / "runs"
            live_calls = {"n": 0}

            def live(payload):
                live_calls["n"] += 1
                return f"live-{payload['q']}-{live_calls['n']}"

            sb.run_subagent_tasks(tasks, runs, self.agent_using_tools([]), live_tools={"search": live}, replay_mode="record")
            base = runs / "case-1" / "with_skill"
            first = (base / "output.md").read_text(encoding="utf-8")
            self.assertTrue((base / "tool-replay.json").is_file())
            self.assertEqual(live_calls["n"], 2)

            def poisoned(payload):
                raise AssertionError("replay must not hit the live tool")

            sb.run_subagent_tasks(tasks, runs, self.agent_using_tools([]), live_tools={"search": poisoned}, replay_mode="replay")
            second = (base / "output.md").read_text(encoding="utf-8")
        self.assertEqual(first, second)
        self.assertEqual(first, "live-alpha-1 then live-beta-2")

    def test_strict_errors_on_unrecorded_tool_call(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            tasks = make_tasks(root)[:1]
            runs = root / "runs"
            sb.run_subagent_tasks(tasks, runs, self.agent_using_tools([]), replay_mode="strict")
            output = (runs / "case-1" / "with_skill" / "output.md").read_text(encoding="utf-8")
        self.assertIn("tool replay miss", output)

    def test_auto_mode_records_then_replays(self):
        with tempfile.TemporaryDirectory() as td:
            store_path = Path(td) / "tool-replay.json"
            store = sb.ToolReplayStore(store_path, "auto")
            self.assertEqual(store.mode, "record")
            store.resolve("t", {"x": 1}, live=lambda p: "out")
            store.save()
            replayer = sb.ToolReplayStore(store_path, "auto")
            self.assertEqual(replayer.mode, "replay")
            self.assertEqual(replayer.resolve("t", {"x": 1}), "out")


class ClosedRunnerOutcomeTests(unittest.TestCase):
    def test_outcome_variants_make_contradictory_states_unconstructible(self):
        context = rc.OutcomeContext(provider=rc.Provider.CODEX, elapsed_ms=0)
        with self.assertRaises(ValueError):
            rc.Completed(context, answer="x", returncode=1)
        with self.assertRaises(ValueError):
            rc.TimedOut(context, returncode=0)
        with self.assertRaises(ValueError):
            rc.ProviderFailed(context, returncode=0)
        with self.assertRaises(ValueError):
            rc.SpawnFailed(context, reason="x", returncode=1)
        with self.assertRaises(ValueError):
            rc.RunnerOutcome(provider="codex", answer="", returncode=0, timed_out=True)
        with self.assertRaises(TypeError):
            rc.Completed(None, answer="x")  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            rc.Completed(context, answer="")

    def test_closed_outcomes_require_exact_integer_returncodes(self):
        context = rc.OutcomeContext(provider=rc.Provider.CODEX)
        for constructor, kwargs, returncode in (
            (rc.Completed, {"answer": "ok"}, False),
            (rc.Completed, {"answer": "ok"}, 0.0),
            (rc.TimedOut, {}, 124.0),
            (rc.SpawnFailed, {"reason": "spawn"}, 127.0),
        ):
            with self.subTest(constructor=constructor.__name__, returncode=returncode), \
                 self.assertRaises(TypeError):
                constructor(context, returncode=returncode, **kwargs)
        for returncode in (False, 0.0, 124.0, 127.0):
            with self.subTest(factory_returncode=returncode), self.assertRaises(TypeError):
                rc.RunnerOutcome(
                    provider="codex", answer="ok", returncode=returncode)
        with self.assertRaises(TypeError):
            rc.ProviderFailed(context, returncode=1.0)

    def test_context_rejects_unknown_provider_and_invalid_measurements(self):
        for kwargs in (
            {"provider": "unknown"}, {"provider": "codex", "elapsed_ms": -1},
            {"provider": "codex", "elapsed_ms": float("nan")},
            {"provider": "codex", "cost_usd": float("inf")},
            {"provider": "codex", "usage": {"input_tokens": -1}},
            {"provider": "codex", "usage": {"input_tokens": "unknown"}},
        ):
            with self.subTest(kwargs=kwargs), self.assertRaises((TypeError, ValueError)):
                rc.OutcomeContext(**kwargs)

    def test_context_rejects_derived_evidence_overrides(self):
        for field in ("metadata_extra", "metrics_extra"):
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, "derived evidence"):
                rc.OutcomeContext(
                    provider="codex",
                    **{field: {"trace_observation_complete": True}},
                )

    def test_context_rejects_lossy_or_non_json_evidence_mappings(self):
        for field in ("metadata_extra", "metrics_extra", "environment"):
            for value in (
                {1: "integer key", "1": "string key"},
                {"nested": {2: "integer key"}},
                {"measurement": float("nan")},
                {"measurement": float("inf")},
            ):
                with self.subTest(field=field, value=value), \
                     self.assertRaises((TypeError, ValueError)):
                    rc.OutcomeContext(provider="codex", **{field: value})
        for field in ("metadata_extra", "metrics_extra"):
            with self.subTest(factory_field=field), self.assertRaises(TypeError):
                rc.RunnerOutcome(
                    provider="codex", answer="ok", **{field: []})
        context = rc.OutcomeContext(provider="codex")
        with self.assertRaises(TypeError):
            context.enriched(metadata=[])

    def test_context_and_outcome_are_recursively_immutable(self):
        source = {"x": 1, "nested": {"value": 2}, "items": [{"value": 3}]}
        context = rc.OutcomeContext(provider="codex", metadata_extra=source)
        outcome = rc.Completed(context, answer="ok")
        source["nested"]["value"] = 9
        self.assertEqual(context.metadata_extra["nested"]["value"], 2)
        with self.assertRaises(TypeError):
            context.metadata_extra["x"] = 2  # type: ignore[index]
        with self.assertRaises(TypeError):
            context.metadata_extra["nested"]["value"] = 4  # type: ignore[index]
        with self.assertRaises(TypeError):
            context.metadata_extra["items"][0]["value"] = 4  # type: ignore[index]
        with self.assertRaises((AttributeError, TypeError)):
            outcome.answer = "changed"  # type: ignore[misc]


class TraceEventStateTests(unittest.TestCase):
    def test_status_parser_is_closed_and_positive(self):
        for raw, expected in (("done", tc.EventState.COMPLETED), ("running", tc.EventState.IN_PROGRESS),
                              ("failed", tc.EventState.FAILED), ("typo", tc.EventState.UNKNOWN),
                              (None, tc.EventState.UNKNOWN)):
            with self.subTest(raw=raw):
                self.assertIs(tc.parse_event_state(raw).state, expected)
        self.assertFalse(tc.event_is_completed({"type": "command"}))
        self.assertFalse(tc.event_is_completed({"type": "command", "status": "failed"}))
        self.assertFalse(tc.event_is_completed({"type": "command", "status": "typo"}))
        self.assertTrue(tc.event_is_completed({"type": "command", "status": "completed"}))

    def test_raw_terminal_kind_can_prove_completion_during_normalization(self):
        event = sb.normalize_trace_record({"type": "item.completed", "item": {"type": "command_execution", "command": "echo ok"}}, source="codex", index=1, line=1)
        self.assertEqual(event["status"], "completed")
        self.assertEqual(event["state_source"], "provider_event_kind")
        self.assertEqual(len(sb.command_events([event])), 1)

    def test_statusless_or_unknown_terminal_looking_kinds_do_not_count(self):
        records = [
            {"type": "command", "command": "echo no"},
            {"type": "tool_call", "toolName": "read"},
            {"type": "tool_typo_end", "toolName": "read"},
        ]
        events, metrics = sb.normalize_trace_records(records, source="generic")
        self.assertTrue(all(event["status"] == "unknown" for event in events["events"]))
        self.assertEqual(metrics["commands"], 0)
        self.assertEqual(metrics["tool_calls"], 0)

    def test_present_malformed_status_cannot_be_upgraded_by_terminal_kind(self):
        for raw in (None, False, 123, [], {}):
            with self.subTest(raw=raw):
                events, metrics = sb.normalize_trace_records(
                    [{"type": "command_end", "status": raw, "command": "echo no"}],
                    source="generic")
                self.assertEqual(events["events"][0]["status"], "unknown")
                self.assertEqual(metrics["commands"], 0)

    def test_unknown_and_failed_commands_do_not_satisfy_command_filters(self):
        events = [
            {"type": "command", "status": "unknown", "input_summary": "unsafe"},
            {"type": "command", "status": "failed", "input_summary": "unsafe"},
        ]
        self.assertEqual(sb.command_events(events), [])


class OTelNormalizationTests(unittest.TestCase):
    """2.4 — OTel GenAI semantic-convention attributes on normalized traces."""

    def test_command_event_carries_execute_tool_attributes(self):
        records = [{"type": "command", "command": "python -m pytest", "exit_code": 0}]
        events_doc, metrics = sb.normalize_trace_records(records, source="codex")
        self.assertEqual(events_doc["schema_version"], 2)
        self.assertEqual(metrics["schema_version"], 2)
        otel = events_doc["events"][0]["otel"]
        self.assertEqual(otel["gen_ai.operation.name"], "execute_tool")
        self.assertEqual(otel["gen_ai.tool.name"], "bash")
        self.assertIn("pytest", otel["gen_ai.tool.call.arguments"])
        self.assertEqual(otel["process.exit_code"], 0)

    def test_usage_lands_in_otel_metrics(self):
        usage = {"input_tokens": 100, "output_tokens": 40, "total_tokens": 140}
        records = [
            {"type": "usage", "usage": usage},
            {"type": "agent_end", "messages": [{"role": "assistant", "usage": usage}]},
        ]
        events_doc, metrics = sb.normalize_trace_records(records, source="pi")
        self.assertEqual(metrics["otel"], {"gen_ai.usage.input_tokens": 100, "gen_ai.usage.output_tokens": 40})
        self.assertEqual(events_doc["events"][0]["otel"]["gen_ai.usage.input_tokens"], 100)

    def test_message_and_error_attributes(self):
        records = [{"type": "agent_message", "content": "hello"}, {"type": "error", "message": "boom"}]
        events_doc, _ = sb.normalize_trace_records(records, source="generic")
        self.assertEqual(events_doc["events"][0]["otel"].get("gen_ai.operation.name"), "chat")
        self.assertIn("error.type", events_doc["events"][1]["otel"])

    def test_pre_bump_events_json_still_grades(self):
        # Backward compatibility: a version-1 events.json (no otel keys) grades.
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            (base / "output.md").write_text("done", encoding="utf-8")
            (base / "events.json").write_text(json.dumps({
                "schema_version": 1, "source": "old",
                "events": [{"type": "command", "command": "python -m pytest -q", "status": "completed"}],
            }), encoding="utf-8")
            r = sb.assertion_result({"type": "command_ran", "pattern": "pytest"}, "done", base / "output.md", run_base=base)
        self.assertTrue(r["passed"])


class D1_FailureMarkerOwnerTests(unittest.TestCase):
    """The failure-body prefixes that runners WRITE are the same constants the
    detector READS — so a renamed marker can't slip a crashed run past scoring."""

    def test_writer_constants_are_exactly_the_detector_markers(self):
        import ablation_model as am
        self.assertEqual((am.CODEX_FAILURE, am.JETTY_FAILURE, am.CLAUDE_FAILURE, am.VIBE_FAILURE, am.TIMEOUT_FAILURE), am.RUNNER_FAILURE_MARKERS)

    def test_each_formatted_failure_body_is_non_executable(self):
        import ablation_model as am
        for marker in am.RUNNER_FAILURE_MARKERS:
            self.assertFalse(am.execution_valid({}, f"{marker}: something broke]\n"))


class R3_WithoutSkillCarriesNoSkillTests(unittest.TestCase):
    """The no-skill arm's row carries no skill files at the source, so a future
    runner that mounts skill_paths unconditionally still cannot leak the skill."""

    def test_without_skill_row_has_empty_skill_paths(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); rp = root / "repo"; _skill(rp)
            p = _manifest(rp, [CASE])
            row = next(r for r in sb.prepared_task_rows(p, sb.validate_manifest(p)) if r["variant"] == "without_skill")
            self.assertEqual(row["skill_paths"], [])


class SharedSkillInvokedTests(unittest.TestCase):
    """skill_invoked is derived the SAME way for every runner: one detect_trigger
    owner in skill_benchmark that scans the model's event stream for a real skill
    read — not a 'mounted => invoked' fiat."""

    def test_detect_trigger_is_evidence_based(self):
        sp = Path("/ws/skills/root-0/SKILL.md")
        read_it = json.dumps({"type": "tool_use", "name": "Read",
                              "status": "completed",
                              "input": {"file_path": "/ws/skills/root-0/SKILL.md"}})
        invoked, evidence = sb.detect_trigger(read_it, [sp])
        self.assertTrue(invoked)
        self.assertTrue(evidence)
        never = json.dumps({"type": "tool_use", "name": "Read",
                            "status": "completed",
                            "input": {"file_path": "/ws/inputs/data.csv"}})
        self.assertEqual(sb.detect_trigger(never, [sp]), (False, []))   # mounted but unread => False

    def test_trigger_eval_uses_the_one_owner(self):
        self.assertIs(tr.detect_trigger, sb.detect_trigger)


class JettyReferencesUploadTests(unittest.TestCase):
    """with_skill uploads the full recursive skill surface (reference files included)
    even with no materialized ablations, so Jetty matches codex's dir mount."""

    def test_with_skill_uploads_references_without_ablations(self):
        import argparse
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); rp = root / "repo"; sd = rp / "skills" / "good-pr"; (sd / "references").mkdir(parents=True)
            (sd / "SKILL.md").write_text("---\nname: good-pr\ndescription: d. Use it.\n---\n\n# B\n\nSee [g](references/g.md).\n", encoding="utf-8")
            (sd / "references" / "g.md").write_text("guide\n", encoding="utf-8")
            (rp / "evals").mkdir()
            m = {"version": 1, "skill_name": "good-pr", "skill_paths": ["skills/good-pr/SKILL.md"],
                 "variants": ["with_skill", "without_skill"],
                 "cases": [{"id": "c", "split": "tune", "prompt": "x", "assertions": [{"name": "a", "type": "contains", "value": "x"}]}],
                 "ablations": []}
            p = rp / "evals" / "shared-benchmark.json"; p.write_text(json.dumps(m), encoding="utf-8")
            out = root / "jetty.jsonl"
            sb.export_jetty(argparse.Namespace(manifest=str(p), out=str(out)))
            payloads = [json.loads(l) for l in out.read_text(encoding="utf-8").splitlines()]
            ws = next(pl for pl in payloads if pl["harness"]["variant"] == "with_skill")
            hints = [f["remote_path_hint"] for f in ws["upload_plan"]["files"] if f["role"] == "skill"]
            self.assertTrue(any(h.endswith("references/g.md") for h in hints))   # the reference file is uploaded


class RunnerOutcomeContractTests(unittest.TestCase):
    """RunnerOutcome + write_runner_outcome: every answer runner returns the typed
    outcome and the ONE writer adapts it onto the run contract the same way, so a
    provider only does provider-specific parsing (the runner-outcome consolidation)."""

    # The run-contract files and the metadata keys every provider's run must carry
    # after going through write_runner_outcome — the shared shape the refactor pins.
    CONTRACT_FILES = {"output.md", "metadata.json", "events.json", "metrics.json"}
    SHARED_META_KEYS = {"provider", "model", "returncode", "timed_out", "elapsed_ms",
                        "stderr", "usage_normalized", "cost_normalized", "trace_source"}

    def _assert_pid_stopped(self, pid: int, timeout: float = 1.0) -> None:
        """Accept a reaped process or a Linux zombie as no longer running."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return

            # An orphan killed with its process group can remain in /proc as a
            # zombie until the runner's init process reaps it.  kill(pid, 0)
            # still succeeds for that harmless, non-running state.
            try:
                stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
            except (FileNotFoundError, PermissionError, OSError):
                pass
            else:
                fields_after_name = stat.rsplit(")", 1)[-1].split()
                if fields_after_name and fields_after_name[0] == "Z":
                    return
            time.sleep(0.01)
        self.fail(f"process {pid} is still running after {timeout:.1f}s")

    def _one_with_skill_task(self, root: Path) -> tuple[Path, str]:
        case = {"id": "c", "split": "tune", "prompt": "do it",
                "assertions": [{"name": "a", "type": "contains", "value": "token"}]}
        manifest = make_eval_repo(root, cases=[case])
        rows = [r for r in sb.prepared_task_rows(manifest, sb.validate_manifest(manifest)) if r["variant"] == "with_skill"]
        tasks = root / "tasks.jsonl"
        tasks.write_text(json.dumps(rows[0]) + "\n", encoding="utf-8")
        return tasks, rows[0]["run_dir"]

    def test_codex_and_claude_produce_the_same_contract_shape(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            tasks, run_dir = self._one_with_skill_task(root)

            fake_codex = root / "fake_codex.py"
            fake_codex.write_text(
                "import json, pathlib, sys\n_ = sys.stdin.read()\n"
                "assert '--output-last-message' in sys.argv\n"
                "pathlib.Path(sys.argv[sys.argv.index('--output-last-message') + 1]).write_text('token from codex')\n"
                "print(json.dumps({'role': 'assistant', 'content': 'trace from codex',"
                " 'usage': {'input_tokens': 4, 'output_tokens': 6}}))\n",
                encoding="utf-8")
            codex_runs = root / "codex-runs"
            sb.run_codex(SimpleNamespace(tasks=str(tasks), runs=str(codex_runs),
                                         codex_cmd=f"{sys.executable} {fake_codex}", timeout=30))

            claude_bin = stub_claude(root / "claude_stub.py", answer="token from claude")
            claude_runs = root / "claude-runs"
            sb.run_claude(argparse.Namespace(tasks=str(tasks), runs=str(claude_runs),
                                             model="claude-haiku-4-5-20251001", claude_bin=str(claude_bin), timeout=30))

            codex_base = codex_runs / run_dir
            claude_base = claude_runs / run_dir
            # Same set of contract files from both providers.
            for base in (codex_base, claude_base):
                self.assertTrue(self.CONTRACT_FILES.issubset({p.name for p in base.iterdir()}), base)
            codex_meta = json.loads((codex_base / "metadata.json").read_text(encoding="utf-8"))
            claude_meta = json.loads((claude_base / "metadata.json").read_text(encoding="utf-8"))
            # The shared metadata keys are present in BOTH, with the provider stamped.
            self.assertTrue(self.SHARED_META_KEYS.issubset(codex_meta), self.SHARED_META_KEYS - set(codex_meta))
            self.assertTrue(self.SHARED_META_KEYS.issubset(claude_meta), self.SHARED_META_KEYS - set(claude_meta))
            self.assertEqual(codex_meta["provider"], "codex")
            self.assertEqual(claude_meta["provider"], "claude")
            # Telemetry is an explicit block carrying the real normalized values,
            # not merely a present key — a regression dropping the numbers must fail.
            self.assertEqual(codex_meta["usage_normalized"]["total_tokens"], 10)   # 4+6 from the trace
            self.assertEqual(codex_meta["usage_normalized"]["source"], "trace_normalized")
            self.assertEqual(claude_meta["usage_normalized"]["total_tokens"], 33)  # 11+22 from the envelope
            self.assertEqual(claude_meta["usage_normalized"]["source"], "provider_reported")
            self.assertIn("source", codex_meta["cost_normalized"])
            self.assertIn("source", claude_meta["cost_normalized"])
            # The whole-writer consolidation means both providers land on the same
            # events/metrics schema version (2, the trace-normalizer's), even with
            # no trace — Claude's empty-trace run is not a schema-1 island.
            for base in (codex_base, claude_base):
                self.assertEqual(json.loads((base / "events.json").read_text())["schema_version"], 2)
                self.assertEqual(json.loads((base / "metrics.json").read_text())["schema_version"], 2)

    def test_run_agent_dispatches_registered_claude_and_codex_backends(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            tasks, run_dir = self._one_with_skill_task(root)
            fake_codex = root / "fake_codex.py"
            fake_codex.write_text(
                "import json, pathlib, sys\n_ = sys.stdin.read()\n"
                "assert '--output-last-message' in sys.argv\n"
                "assert '--ignore-user-config' in sys.argv and '--ignore-rules' in sys.argv\n"
                "codex_home = pathlib.Path(__import__('os').environ['CODEX_HOME'])\n"
                "assert codex_home.is_dir()\n"
                "assert not codex_home.is_relative_to(pathlib.Path.cwd())\n"
                "assert not (pathlib.Path.cwd() / '.codex' / 'auth.json').exists()\n"
                "pathlib.Path(sys.argv[sys.argv.index('--output-last-message') + 1]).write_text('token from codex')\n"
                "print(json.dumps({'role': 'assistant', 'content': 'trace from codex'}))\n",
                encoding="utf-8")
            codex_runs = root / "agent-codex"
            sb.run_agent(argparse.Namespace(agent="codex", tasks=str(tasks), runs=str(codex_runs), model="gpt-mini",
                                            codex_cmd=f"{sys.executable} {fake_codex}", claude_bin="claude", timeout=30))
            self.assertIn("token from codex", (codex_runs / run_dir / "output.md").read_text(encoding="utf-8"))
            self.assertEqual(json.loads((codex_runs / run_dir / "metadata.json").read_text(encoding="utf-8"))["model"], "gpt-mini")

            claude_bin = stub_claude(root / "claude_stub.py", answer="token from claude")
            claude_runs = root / "agent-claude"
            sb.run_agent(argparse.Namespace(agent="claude", tasks=str(tasks), runs=str(claude_runs), model="claude-haiku-4-5-20251001",
                                            codex_cmd="codex exec --json", claude_bin=str(claude_bin), timeout=30))
            self.assertIn("token from claude", (claude_runs / run_dir / "output.md").read_text(encoding="utf-8"))

    def test_codex_cleanup_race_preserves_artifacts_and_next_task(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            case = {"id": "c", "split": "tune", "prompt": "do it",
                    "assertions": [{"name": "a", "type": "contains", "value": "token"}]}
            manifest = make_eval_repo(root, cases=[case])
            rows = sb.prepared_task_rows(manifest, sb.validate_manifest(manifest))
            tasks = root / "tasks.jsonl"
            tasks.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
            fake_codex = root / "fake_codex.py"
            fake_codex.write_text(
                "import json, pathlib, sys\n_ = sys.stdin.read()\n"
                "pathlib.Path(sys.argv[sys.argv.index('--output-last-message') + 1]).write_text('token')\n"
                "print(json.dumps({'role': 'assistant', 'content': 'trace'}))\n",
                encoding="utf-8")

            real_rmtree = shutil.rmtree
            raced: set[Path] = set()

            def fail_each_codex_cleanup_once(path, *args, **kwargs):
                candidate = Path(path)
                if candidate.name.startswith("codex-invoke-") and candidate not in raced and not kwargs.get("ignore_errors"):
                    raced.add(candidate)
                    raise OSError(errno.ENOTEMPTY, "Directory not empty")
                return real_rmtree(path, *args, **kwargs)

            runs = root / "runs"
            with mock.patch.object(sb.shutil, "rmtree", side_effect=fail_each_codex_cleanup_once), \
                 mock.patch.object(sb.time, "sleep", return_value=None):
                self.assertEqual(sb.run_codex(SimpleNamespace(
                    tasks=str(tasks), runs=str(runs),
                    codex_cmd=f"{sys.executable} {fake_codex}", timeout=30)), 0)

            self.assertEqual(len(raced), 2)
            for row in rows:
                base = runs / row["run_dir"]
                self.assertEqual((base / "output.md").read_text(encoding="utf-8"), "token")
                self.assertTrue(sb.artifact_commit_valid(base))
                self.assertIn("trace", (base / "trace.jsonl").read_text(encoding="utf-8"))
                meta = json.loads((base / "metadata.json").read_text(encoding="utf-8"))
                self.assertIn("cleanup recovered after 2 attempts", meta["stderr"])
                environment = json.loads((base / "environment.json").read_text(encoding="utf-8"))
                self.assertEqual(environment["temporary_home_cleanup"]["status"], "removed")
                self.assertEqual(environment["temporary_home_cleanup"]["attempts"], 2)

    def test_codex_cleanup_fallback_is_bounded_and_preserves_result(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            fake_codex = root / "fake_codex.py"
            fake_codex.write_text(
                "import pathlib, sys\n_ = sys.stdin.read()\n"
                "pathlib.Path(sys.argv[sys.argv.index('--output-last-message') + 1]).write_text('token')\n"
                "sys.stderr.write('x' * 5000)\n",
                encoding="utf-8")
            real_rmtree = shutil.rmtree
            attempts = 0

            def fail_until_final_fallback(path, *args, **kwargs):
                nonlocal attempts
                candidate = Path(path)
                if candidate.name.startswith("codex-invoke-"):
                    if kwargs.get("ignore_errors"):
                        return real_rmtree(path, *args, **kwargs)
                    attempts += 1
                    raise OSError(errno.EBUSY, "Device or resource busy")
                return real_rmtree(path, *args, **kwargs)

            with mock.patch.object(sb.shutil, "rmtree", side_effect=fail_until_final_fallback), \
                 mock.patch.object(sb.time, "sleep", return_value=None):
                result = sb.codex_cli_invoke(
                    "prompt", codex_cmd=f"{sys.executable} {fake_codex}",
                    cwd=root / "workspace", timeout=30)

            self.assertEqual(result["answer"], "token")
            cleanup = result["environment"]["temporary_home_cleanup"]
            self.assertEqual(attempts, 1 + len(sb.CODEX_TEMP_CLEANUP_RETRY_DELAYS_S))
            self.assertEqual(cleanup["status"], "removed_after_fallback")
            self.assertTrue(cleanup["fallback_attempted"])
            self.assertIn("required the final fallback", result["stderr"])
            self.assertLessEqual(len(result["stderr"]), 4000)

    def test_codex_cleanup_retries_nested_file_not_found_while_root_exists(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "codex-invoke-test"
            (root / "codex-home" / ".tmp").mkdir(parents=True)
            real_rmtree = shutil.rmtree
            attempts = 0

            def lose_nested_entry_once(path, *args, **kwargs):
                nonlocal attempts
                attempts += 1
                if attempts == 1:
                    raise FileNotFoundError(errno.ENOENT, "missing nested entry", str(root / "codex-home" / ".tmp" / "gone"))
                return real_rmtree(path, *args, **kwargs)

            with mock.patch.object(sb.shutil, "rmtree", side_effect=lose_nested_entry_once), \
                 mock.patch.object(sb.time, "sleep", return_value=None):
                cleanup = sb.cleanup_codex_invoke_temp(root)

            self.assertEqual(cleanup["status"], "removed")
            self.assertEqual(cleanup["attempts"], 2)
            self.assertIn("recovered after 2 attempts", cleanup["warning"])
            self.assertFalse(root.exists())

    def test_codex_retained_home_is_observable_and_never_reused(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            case = {"id": "c", "split": "tune", "prompt": "do it",
                    "assertions": [{"name": "a", "type": "contains", "value": "token"}]}
            manifest = make_eval_repo(root, cases=[case])
            rows = sb.prepared_task_rows(manifest, sb.validate_manifest(manifest))
            tasks = root / "tasks.jsonl"
            tasks.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
            fake_codex = root / "fake_codex.py"
            fake_codex.write_text(
                "import json, pathlib, sys\n_ = sys.stdin.read()\n"
                "pathlib.Path(sys.argv[sys.argv.index('--output-last-message') + 1]).write_text('token')\n"
                "print(json.dumps({'role': 'assistant', 'content': 'trace'}))\n",
                encoding="utf-8")
            real_mkdtemp = tempfile.mkdtemp
            real_rmtree = shutil.rmtree
            invoke_temps: list[Path] = []

            def record_invoke_temp(*args, **kwargs):
                path = Path(real_mkdtemp(*args, **kwargs))
                if str(kwargs.get("prefix", "")).startswith("codex-invoke-"):
                    invoke_temps.append(path)
                return str(path)

            def retain_invoke_temp(path, *args, **kwargs):
                candidate = Path(path)
                if candidate.name.startswith("codex-invoke-"):
                    if kwargs.get("ignore_errors"):
                        return None
                    raise OSError(errno.EBUSY, "Device or resource busy")
                return real_rmtree(path, *args, **kwargs)

            runs = root / "runs"
            try:
                with mock.patch.object(sb.tempfile, "mkdtemp", side_effect=record_invoke_temp), \
                     mock.patch.object(sb.shutil, "rmtree", side_effect=retain_invoke_temp), \
                     mock.patch.object(sb.time, "sleep", return_value=None):
                    self.assertEqual(sb.run_codex(SimpleNamespace(
                        tasks=str(tasks), runs=str(runs),
                        codex_cmd=f"{sys.executable} {fake_codex}", timeout=30)), 0)

                self.assertEqual(len(invoke_temps), 2)
                self.assertEqual(len(set(invoke_temps)), 2)
                self.assertTrue(all(path.exists() for path in invoke_temps))
                for row in rows:
                    base = runs / row["run_dir"]
                    self.assertEqual((base / "output.md").read_text(encoding="utf-8"), "token")
                    self.assertTrue(sb.artifact_commit_valid(base))
                    self.assertIn("trace", (base / "trace.jsonl").read_text(encoding="utf-8"))
                    metadata = json.loads((base / "metadata.json").read_text(encoding="utf-8"))
                    self.assertIn("will not be reused", metadata["stderr"])
                    environment = json.loads((base / "environment.json").read_text(encoding="utf-8"))
                    self.assertEqual(environment["temporary_home_cleanup"]["status"], "retained")
                    self.assertIn("will not be reused", environment["temporary_home_cleanup"]["warning"])
            finally:
                for path in invoke_temps:
                    if path.exists():
                        real_rmtree(path)

    def test_codex_normal_cleanup_removes_temporary_home(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            fake_codex = root / "fake_codex.py"
            fake_codex.write_text(
                "import pathlib, sys\n_ = sys.stdin.read()\n"
                "pathlib.Path(sys.argv[sys.argv.index('--output-last-message') + 1]).write_text('token')\n",
                encoding="utf-8")
            real_mkdtemp = tempfile.mkdtemp
            created: list[Path] = []

            def record_codex_temp(*args, **kwargs):
                path = Path(real_mkdtemp(*args, **kwargs))
                if str(kwargs.get("prefix", "")).startswith("codex-invoke-"):
                    created.append(path)
                return str(path)

            with mock.patch.object(sb.tempfile, "mkdtemp", side_effect=record_codex_temp):
                result = sb.codex_cli_invoke(
                    "prompt", codex_cmd=f"{sys.executable} {fake_codex}",
                    cwd=root / "workspace", timeout=30)

            self.assertEqual(result["answer"], "token")
            self.assertEqual(result["environment"]["temporary_home_cleanup"]["status"], "removed")
            self.assertEqual(len(created), 1)
            self.assertFalse(created[0].exists())

    @unittest.skipUnless(hasattr(os, "killpg"), "process-group cleanup requires POSIX")
    def test_codex_stops_plugin_descendant_before_home_cleanup(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            pid_file = root / "plugin-child.pid"
            child_code = (
                "import os, pathlib, signal, time\n"
                "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
                "target = pathlib.Path(os.environ['CODEX_HOME']) / '.tmp' / 'plugins-clone-test' / 'plugins'\n"
                "while True:\n"
                " target.mkdir(parents=True, exist_ok=True)\n"
                " (target / 'active').write_text('x')\n"
                " time.sleep(0.005)\n"
            )
            fake_codex = root / "fake_codex_with_plugin_child.py"
            fake_codex.write_text(
                "import pathlib, subprocess, sys, time\n_ = sys.stdin.read()\n"
                f"child = subprocess.Popen([sys.executable, '-c', {child_code!r}], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True)\n"
                f"pathlib.Path({str(pid_file)!r}).write_text(str(child.pid))\n"
                "time.sleep(0.05)\n"
                "pathlib.Path(sys.argv[sys.argv.index('--output-last-message') + 1]).write_text('token')\n",
                encoding="utf-8")

            result = sb.codex_cli_invoke(
                "prompt", codex_cmd=f"{sys.executable} {fake_codex}",
                cwd=root / "workspace", timeout=30)

            self.assertEqual(result["answer"], "token")
            self.assertEqual(result["environment"]["process_group_cleanup"]["signal"], "SIGKILL")
            self.assertEqual(result["environment"]["temporary_home_cleanup"]["status"], "removed")
            child_pid = int(pid_file.read_text(encoding="utf-8"))
            self._assert_pid_stopped(child_pid)

    @unittest.skipUnless(hasattr(os, "killpg"), "process-group cleanup requires POSIX")
    def test_success_quiesces_pipe_holding_group_without_full_timeout(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            pid_file = root / "group-child.pid"
            fake_codex = root / "fake_codex.py"
            fake_codex.write_text(
                "import pathlib, subprocess, sys\n"
                "_ = sys.stdin.read()\n"
                "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n"
                f"pathlib.Path({str(pid_file)!r}).write_text(str(child.pid))\n"
                "pathlib.Path(sys.argv[sys.argv.index('--output-last-message') + 1]).write_text('token')\n",
                encoding="utf-8")
            started = time.monotonic()
            result = sb.codex_cli_invoke(
                "prompt", codex_cmd=f"{sys.executable} {fake_codex}",
                cwd=root / "workspace", timeout=2)
            elapsed = time.monotonic() - started

            self.assertEqual(result["answer"], "token")
            self.assertEqual(result["returncode"], 0)
            self.assertFalse(result["timed_out"])
            self.assertLess(elapsed, 1.0)
            self.assertEqual(result["environment"]["process_group_cleanup"]["signal"], "SIGKILL")
            child_pid = int(pid_file.read_text(encoding="utf-8"))
            self._assert_pid_stopped(child_pid)

    @unittest.skipUnless(hasattr(os, "killpg"), "process-group cleanup requires POSIX")
    def test_success_does_not_wait_for_escaped_child_capture_pipes(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            pid_file = root / "escaped-child.pid"
            fake_codex = root / "fake_codex.py"
            fake_codex.write_text(
                "import json, pathlib, subprocess, sys\n"
                "_ = sys.stdin.read()\n"
                "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'], start_new_session=True)\n"
                f"pathlib.Path({str(pid_file)!r}).write_text(str(child.pid))\n"
                "pathlib.Path(sys.argv[sys.argv.index('--output-last-message') + 1]).write_text('token')\n"
                "print(json.dumps({'role': 'assistant', 'content': 'trace'}))\n",
                encoding="utf-8")
            started = time.monotonic()
            result = sb.codex_cli_invoke(
                "prompt", codex_cmd=f"{sys.executable} {fake_codex}",
                cwd=root / "workspace", timeout=2)
            elapsed = time.monotonic() - started
            child_pid = int(pid_file.read_text(encoding="utf-8"))
            try:
                self.assertEqual(result["answer"], "token")
                self.assertIn("trace", result["trace_text"])
                self.assertEqual(result["returncode"], 0)
                self.assertFalse(result["timed_out"])
                self.assertLess(elapsed, 2.0)
                self.assertEqual(result["environment"]["process_group_cleanup"]["pipe_drain"], "closed")
            finally:
                try:
                    os.kill(child_pid, 9)
                except ProcessLookupError:
                    pass

    @unittest.skipUnless(hasattr(os, "killpg"), "process-group cleanup requires POSIX")
    def test_timeout_does_not_wait_forever_for_escaped_child_capture_pipes(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            pid_file = root / "escaped-child.pid"
            parent = root / "parent.py"
            parent.write_text(
                "import pathlib, subprocess, sys, time\n"
                "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'], start_new_session=True)\n"
                f"pathlib.Path({str(pid_file)!r}).write_text(str(child.pid))\n"
                "time.sleep(30)\n",
                encoding="utf-8")
            started = time.monotonic()
            outcome = sb.invoke_argv_with_timeout([sys.executable, str(parent)], cwd=root, timeout=1)
            elapsed = time.monotonic() - started
            child_pid = int(pid_file.read_text(encoding="utf-8"))
            try:
                self.assertTrue(outcome.timed_out)
                self.assertEqual(outcome.returncode, 124)
                self.assertLess(elapsed, 3.0)
                self.assertEqual(outcome.metadata["process_group_cleanup"]["pipe_drain"], "closed")
            finally:
                try:
                    os.kill(child_pid, 9)
                except ProcessLookupError:
                    pass

    def test_run_agent_writes_failure_artifact_when_native_command_is_missing(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            tasks, run_dir = self._one_with_skill_task(root)
            runs = root / "runs"
            sb.run_agent(argparse.Namespace(agent="codex", tasks=str(tasks), runs=str(runs), model="gpt-mini",
                                            codex_cmd=str(root / "missing-codex"), claude_bin="claude", timeout=30))
            base = runs / run_dir
            text = (base / "output.md").read_text(encoding="utf-8")
            meta = json.loads((base / "metadata.json").read_text(encoding="utf-8"))
            self.assertTrue(text.lstrip().startswith(sb.CODEX_FAILURE))
            self.assertEqual(meta["returncode"], 127)
            self.assertFalse(sb.execution_valid(meta, text))

    def test_trace_is_never_a_fallback_final_answer(self):
        trace = json.dumps({"role": "assistant", "content": "not a sidecar answer"})
        outcome = am.RunnerOutcome(provider="codex", answer=None, returncode=0, trace_text=trace)
        self.assertIsInstance(outcome, rc.ProviderFailed)
        with self.assertRaises(TypeError):
            rc.Completed(rc.OutcomeContext(provider="codex", trace_text=trace), answer=None)  # type: ignore[arg-type]
        with tempfile.TemporaryDirectory() as td:
            base = Path(td) / "run"
            sb.write_runner_outcome(base, outcome)
            meta = json.loads((base / "metadata.json").read_text(encoding="utf-8"))
            text = (base / "output.md").read_text(encoding="utf-8")
            self.assertEqual(meta["returncode"], 0)  # actual process exit is preserved
            self.assertFalse(meta["provider_response_complete"])
            self.assertNotIn(trace, text)
            self.assertFalse(sb.execution_valid(sb.read_metadata_base(base), text))

    def test_write_runner_outcome_encodes_timeout_uniformly(self):
        # One timeout encoding for every provider: timed_out + returncode 124, a
        # failure-marker body, and execution_valid() rejects it.
        with tempfile.TemporaryDirectory() as td:
            base = Path(td) / "run"
            outcome = am.RunnerOutcome(provider="claude", answer="", timed_out=True)
            sb.write_runner_outcome(base, outcome)
            meta = json.loads((base / "metadata.json").read_text(encoding="utf-8"))
            text = (base / "output.md").read_text(encoding="utf-8")
            self.assertTrue(meta["timed_out"])
            self.assertEqual(meta["returncode"], 124)
            self.assertTrue(text.lstrip().startswith(am.CLAUDE_FAILURE))
            self.assertNotIn("None", text)
            self.assertFalse(am.execution_valid(meta, text))

    def test_atomic_run_replacement_drops_stale_provider_artifacts(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td) / "run"
            sb.write_runner_outcome(
                base, am.RunnerOutcome(provider="subagent", answer="first", returncode=0,
                                       trace_text=json.dumps({"type": "usage", "usage": {"input_tokens": 1}})))
            self.assertTrue((base / "trace.jsonl").exists())
            (base / "grading.json").write_text("stale", encoding="utf-8")
            sb.write_runner_outcome(
                base, am.RunnerOutcome(provider="subagent", answer="second", returncode=0))
            self.assertEqual((base / "output.md").read_text(encoding="utf-8"), "second")
            self.assertFalse((base / "trace.jsonl").exists())
            self.assertFalse((base / "grading.json").exists())
            self.assertTrue(sb.read_metadata_base(base)["artifact_set_complete"])

    def test_atomic_run_replacement_restores_previous_commit_on_install_failure(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td) / "run"
            sb.write_runner_outcome(
                base, am.RunnerOutcome(provider="subagent", answer="old", returncode=0))
            real_replace = os.replace

            def fail_stage_install(src, dst):
                if ".artifact-stage-" in Path(src).name and Path(dst) == base:
                    raise OSError("simulated install failure")
                return real_replace(src, dst)

            with mock.patch.object(sb.os, "replace", side_effect=fail_stage_install), \
                 self.assertRaisesRegex(OSError, "install failure"):
                sb.write_runner_outcome(
                    base, am.RunnerOutcome(provider="subagent", answer="new", returncode=0))
            self.assertEqual((base / "output.md").read_text(encoding="utf-8"), "old")
            self.assertTrue(sb.read_metadata_base(base)["artifact_set_complete"])

    def test_artifact_commit_is_required_and_detects_post_commit_mutation(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td) / "run"
            sb.write_runner_outcome(
                base, am.RunnerOutcome(provider="subagent", answer="hi", returncode=0))
            text = (base / "output.md").read_text(encoding="utf-8")
            committed = sb.read_metadata_base(base)
            self.assertTrue(committed["artifact_set_complete"])
            self.assertTrue(am.execution_valid(committed, text))
            (base / "output.md").write_text("tampered", encoding="utf-8")
            tampered = sb.read_metadata_base(base)
            self.assertFalse(tampered["artifact_set_complete"])
            self.assertFalse(am.execution_valid(tampered, "tampered"))

    def test_write_runner_outcome_marks_missing_telemetry_explicit(self):
        # No provider usage/cost and no trace → explicit missing, never zero/absent.
        with tempfile.TemporaryDirectory() as td:
            base = Path(td) / "run"
            sb.write_runner_outcome(base, am.RunnerOutcome(provider="subagent", answer="hi", returncode=0))
            meta = json.loads((base / "metadata.json").read_text(encoding="utf-8"))
            self.assertEqual(meta["usage_normalized"], {"source": "missing"})
            self.assertEqual(meta["cost_normalized"], {"source": "missing"})

    def test_subagent_returncode_body_stays_error_shaped(self):
        # The subagent seam diagnoses via error/empty, not a returncode+stderr body:
        # diagnose_returncode=False keeps its historical body shape.
        with tempfile.TemporaryDirectory() as td:
            base = Path(td) / "run"
            outcome = am.RunnerOutcome(provider="subagent", answer="", returncode=3, diagnose_returncode=False)
            sb.write_runner_outcome(base, outcome)
            text = (base / "output.md").read_text(encoding="utf-8")
            self.assertIn("no output produced", text)
            self.assertNotIn("returncode=3", text)

    def test_empty_string_answer_is_never_reconstructed_from_trace(self):
        # The answer=None sentinel (Codex) is what derives from the trace; a string
        # answer — even "" — is used verbatim. This guards validity gating: an empty
        # answer that happens to carry a trace must become the failure marker, never
        # leak the trace's final message and slip past execution_valid().
        with tempfile.TemporaryDirectory() as td:
            base = Path(td) / "run"
            trace = json.dumps({"role": "assistant", "content": "LEAKED FROM TRACE"})
            outcome = am.RunnerOutcome(provider="subagent", answer="", returncode=0, trace_text=trace)
            self.assertIsInstance(outcome, rc.ProviderFailed)
            sb.write_runner_outcome(base, outcome)
            text = (base / "output.md").read_text(encoding="utf-8")
            self.assertNotIn("LEAKED FROM TRACE", text)
            self.assertTrue(text.lstrip().startswith(am.CLAUDE_FAILURE))
            meta = json.loads((base / "metadata.json").read_text(encoding="utf-8"))
            self.assertEqual(meta["returncode"], 0)  # protocol failure does not rewrite process evidence
            self.assertFalse(meta["provider_response_complete"])
            self.assertFalse(am.execution_valid(sb.read_metadata_base(base), text))

    def test_run_agent_dispatches_registered_vibe_backend(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            tasks, run_dir = self._one_with_skill_task(root)
            fake_vibe = root / "fake_vibe.py"
            fake_vibe.write_text(
                "import json, os, pathlib, sys\n"
                "prompt = sys.argv[sys.argv.index('--prompt') + 1]\n"
                "assert '--prompt' in sys.argv\n"
                "assert '--output' in sys.argv\n"
                "assert '--workdir' in sys.argv\n"
                "assert '--trust' in sys.argv\n"
                "assert os.environ.get('VIBE_ACTIVE_MODEL') == 'mistral-test'\n"
                "workdir = pathlib.Path(sys.argv[sys.argv.index('--workdir') + 1])\n"
                "vibe_home = pathlib.Path(os.environ['VIBE_HOME'])\n"
                "assert vibe_home.is_dir()\n"
                "assert not vibe_home.is_relative_to(workdir)\n"
                "assert not (workdir / '.vibe-home' / '.env').exists()\n"
                "assert 'Task prompt:' in prompt\n"
                "print(json.dumps({'role': 'assistant', 'content': 'token from vibe',"
                " 'usage': {'input_tokens': 5, 'output_tokens': 7}, 'cost_usd': 0.02}))\n",
                encoding="utf-8")
            runs = root / "vibe-runs"
            sb.run_agent(argparse.Namespace(agent="vibe", tasks=str(tasks), runs=str(runs), model="mistral-test",
                                            codex_cmd="codex exec --json", claude_bin="claude", vibe_cmd=f"{sys.executable} {fake_vibe}", timeout=30))
            base = runs / run_dir
            self.assertIn("token from vibe", (base / "output.md").read_text(encoding="utf-8"))
            meta = json.loads((base / "metadata.json").read_text(encoding="utf-8"))
            self.assertEqual(meta["provider"], "vibe")
            self.assertEqual(meta["model"], "mistral-test")
            self.assertEqual(meta["usage_normalized"]["total_tokens"], 12)
            self.assertEqual(meta["cost_normalized"]["total_cost"], 0.02)
            env = json.loads((base / "environment.json").read_text(encoding="utf-8"))
            self.assertTrue(env["config_isolated"])
            self.assertTrue(env["vibe_home_outside_workdir"])
            self.assertEqual(env["vibe_home"], "<isolated VIBE_HOME outside workdir>")
            self.assertIn("--prompt '<prompt>'", env["command"])
            self.assertNotIn("Task prompt:", env["command"])

    def test_vibe_success_without_usage_writes_explicit_missing_telemetry(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            tasks, run_dir = self._one_with_skill_task(root)
            fake_vibe = root / "fake_vibe_no_usage.py"
            fake_vibe.write_text(
                "import json\n"
                "print(json.dumps({'role': 'assistant', 'content': 'token from vibe'}))\n",
                encoding="utf-8")
            runs = root / "vibe-runs"
            sb.run_agent(argparse.Namespace(agent="vibe", tasks=str(tasks), runs=str(runs), model="mistral-test",
                                            codex_cmd="codex exec --json", claude_bin="claude", vibe_cmd=f"{sys.executable} {fake_vibe}", timeout=30))
            base = runs / run_dir
            meta = json.loads((base / "metadata.json").read_text(encoding="utf-8"))
            self.assertEqual(meta["provider"], "vibe")
            self.assertEqual(meta["usage_normalized"], {"source": "missing"})
            self.assertEqual(meta["cost_normalized"], {"source": "missing"})

    def test_vibe_home_seeding_copies_only_env_file_not_user_skills(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source = root / "source-vibe"
            (source / "skills" / "personal").mkdir(parents=True)
            (source / ".env").write_text("MISTRAL_API_KEY=secret\n", encoding="utf-8")
            with mock.patch.dict(os.environ, {"VIBE_HOME": str(source)}, clear=True):
                meta = sb.seed_vibe_home(root / "isolated-vibe")
            self.assertTrue(meta["vibe_env_file_copied"])
            self.assertEqual((root / "isolated-vibe" / ".env").read_text(encoding="utf-8"), "MISTRAL_API_KEY=secret\n")
            self.assertFalse((root / "isolated-vibe" / "skills").exists())

    def test_vibe_parser_handles_json_and_streaming_messages(self):
        json_text = json.dumps([
            {"role": "user", "content": "q"},
            {"role": "assistant", "content": "final", "usage": {"prompt_tokens": 1, "completion_tokens": 2}},
        ])
        messages = sb.parse_vibe_messages(json_text)
        self.assertEqual(sb.vibe_final_answer(messages), "final")
        usage, _ = sb.vibe_usage_and_cost(messages)
        self.assertEqual(sb.normalize_usage(usage, source="provider_reported")["total_tokens"], 3)
        streaming = json.dumps({"role": "assistant", "content": "streamed"}) + "\n"
        self.assertEqual(sb.vibe_final_answer(sb.parse_vibe_messages(streaming)), "streamed")
        self.assertEqual(sb.vibe_final_answer([{"role": "tool", "content": "trace only"}]), "")
        self.assertEqual(sb.vibe_final_answer([{"role": "assistant", "content": {"error": "bad shape"}}]), "")

    def test_vibe_missing_binary_uses_vibe_failure_marker(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            tasks, run_dir = self._one_with_skill_task(root)
            runs = root / "runs"
            sb.run_agent(argparse.Namespace(agent="vibe", tasks=str(tasks), runs=str(runs), model=None,
                                            codex_cmd="codex exec --json", claude_bin="claude", vibe_cmd=str(root / "missing-vibe"), timeout=30))
            base = runs / run_dir
            text = (base / "output.md").read_text(encoding="utf-8")
            meta = json.loads((base / "metadata.json").read_text(encoding="utf-8"))
            self.assertTrue(text.lstrip().startswith(sb.VIBE_FAILURE))
            self.assertEqual(meta["returncode"], 127)
            self.assertFalse(sb.execution_valid(meta, text))

    def test_codex_empty_output_writes_explicit_missing_telemetry(self):
        # The PR's headline consistency fix: an empty Codex run now goes through the
        # shared writer, so it gets explicit missing telemetry and schema-2 metrics
        # (was schema-1 metadata with no normalized blocks before the consolidation).
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            tasks, run_dir = self._one_with_skill_task(root)
            silent = root / "silent_codex.py"
            silent.write_text("import sys\n_ = sys.stdin.read()\n", encoding="utf-8")  # emits nothing
            runs = root / "runs"
            sb.run_codex(SimpleNamespace(tasks=str(tasks), runs=str(runs),
                                         codex_cmd=f"{sys.executable} {silent}", timeout=30))
            base = runs / run_dir
            self.assertIn("no final answer", (base / "output.md").read_text(encoding="utf-8"))
            meta = json.loads((base / "metadata.json").read_text(encoding="utf-8"))
            self.assertEqual(meta["usage_normalized"], {"source": "missing"})
            self.assertEqual(meta["cost_normalized"], {"source": "missing"})
            self.assertEqual(json.loads((base / "metrics.json").read_text())["schema_version"], 2)


class TraceDialectRegistryTests(unittest.TestCase):
    """ONE registry of per-provider trace semantics — how raw records flatten
    into normalizer-native (line, record) pairs, and how a stream's terminal
    usage/failure resolve — instead of two special-casing styles inside
    normalize_trace_records (a pi_stream parameter AND a claude flatten
    branch)."""

    def test_registered_sources_use_explicit_dialects(self):
        for source in ("generic", "stub", "subagent"):
            with self.subTest(source=source):
                self.assertIs(sb.trace_dialect_for(source), sb.GENERIC_TRACE_DIALECT)
        for source in ("codex", "JETTY", "vibe"):
            with self.subTest(source=source):
                self.assertIsNot(
                    sb.trace_dialect_for(source), sb.GENERIC_TRACE_DIALECT)

    def test_unknown_misspelled_and_non_string_sources_are_rejected(self):
        for source in ("unknown", "pi ", " vibes", "", None, 3):
            with self.subTest(source=source), self.assertRaises(ValueError):
                sb.trace_dialect_for(source)

    def test_generic_flatten_is_the_identity_with_line_numbers(self):
        records = [{"a": 1}, {"b": 2}]
        self.assertEqual(sb.GENERIC_TRACE_DIALECT.flatten(records), [(1, {"a": 1}), (2, {"b": 2})])
        self.assertEqual(sb.GENERIC_TRACE_DIALECT.flatten(records, record_lines=[3, 7]),
                         [(3, {"a": 1}), (7, {"b": 2})])
        with self.assertRaises(ValueError):
            sb.GENERIC_TRACE_DIALECT.flatten(records, record_lines=[3])

    def test_generic_dialect_has_no_terminal_stream_semantics(self):
        self.assertEqual(sb.GENERIC_TRACE_DIALECT.stream_semantics([], None), (None, None))

    def test_claude_dialect_owns_the_stream_flatten(self):
        self.assertIs(sb.TRACE_DIALECTS["claude"].flatten, sb.claude_stream_flat_records)
        self.assertIs(sb.trace_dialect_for("Claude"), sb.TRACE_DIALECTS["claude"])

    def test_claude_completed_tool_lifecycle_counts_exactly_once(self):
        records = [
            {"type": "assistant", "message": {"role": "assistant", "content": [
                {"type": "tool_use", "id": "call-1", "name": "Read",
                 "input": {"file_path": "/tmp/a"}},
            ]}},
            {"type": "user", "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "call-1", "content": "ok"},
            ]}},
        ]
        events, metrics = sb.normalize_trace_records(records, source="claude")
        lifecycle = [event for event in events["events"]
                     if event["type"] in {"tool_call", "file_read"}]
        self.assertEqual([event["status"] for event in lifecycle],
                         ["in_progress", "completed"])
        self.assertEqual(metrics["tool_calls"], 1)
        self.assertNotIn("trace_protocol_errors", metrics)

    def test_claude_dangling_and_unmatched_calls_are_protocol_invalid(self):
        for records, phrase in (
            ([{"type": "assistant", "message": {"content": [
                {"type": "tool_use", "id": "call-1", "name": "Read", "input": {}},
            ]}}], "no matching tool_result"),
            ([{"type": "user", "message": {"content": [
                {"type": "tool_result", "tool_use_id": "missing", "content": "x"},
            ]}}], "unmatched Claude tool_result"),
        ):
            with self.subTest(phrase=phrase):
                _, metrics = sb.normalize_trace_records(records, source="claude")
                self.assertTrue(any(phrase in error
                                    for error in metrics["trace_protocol_errors"]))

    def test_claude_malformed_message_and_lifecycle_fields_are_protocol_invalid(self):
        malformed = [
            {"type": "assistant", "message": {"content": {"type": "tool_use"}}},
            {"type": "assistant", "message": {"content": [
                {"type": "tool_use", "id": 1, "name": "Read", "input": {}},
                {"type": "tool_use", "id": "x", "name": "", "input": {}},
                {"type": "tool_use", "id": "y", "name": "Read", "input": []},
            ]}},
            {"type": "user", "message": {"content": [
                {"type": "tool_result", "tool_use_id": "x", "is_error": "false"},
            ]}},
        ]
        _, metrics = sb.normalize_trace_records(malformed, source="claude")
        self.assertGreaterEqual(len(metrics["trace_protocol_errors"]), 5)

    def test_claude_protocol_error_makes_trace_signal_unavailable(self):
        raw = json.dumps({
            "type": "assistant", "message": {"content": [
                {"type": "tool_use", "id": "dangling", "name": "Read", "input": {}},
            ]},
        })
        with tempfile.TemporaryDirectory() as td:
            run_dir = Path(td) / "run"
            sb.write_trace_artifacts(
                run_dir, raw, source="claude",
                process_observation_complete=True,
                provider_response_complete=True,
            )
            metrics = json.loads((run_dir / "metrics.json").read_text(encoding="utf-8"))
            passed, evidence = sb.process_or_efficiency_assertion_result(
                {"type": "tool_call", "tool": "WebSearch", "expected_no_call": True},
                run_dir, {},
            )
        self.assertFalse(metrics["trace_observation_complete"])
        self.assertFalse(metrics["operation_observation_complete"])
        self.assertFalse(passed)
        self.assertIn("trace_observation_incomplete", evidence)

    def test_pi_dialect_resolves_cumulative_terminal_usage(self):
        # The retry-then-success stream carries per-attempt usage; the dialect
        # must resolve the FINAL attempt's cumulative usage, not a sum of
        # attempts, and report no failure for a recovered stream.
        raw = (ROOT / "tests" / "fixtures" / "pi" / "retry-then-success.jsonl").read_text(encoding="utf-8")
        records, _ = sb.parse_trace_jsonl_text(raw)
        terminal_usage, failure = sb.TRACE_DIALECTS["pi"].stream_semantics(records, None)
        self.assertIsNone(failure)
        self.assertEqual(sb.usage_number(terminal_usage, "total_tokens"), 13.0)


if __name__ == "__main__":
    unittest.main()
