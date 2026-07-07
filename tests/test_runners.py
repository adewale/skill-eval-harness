"""Runner adapters and their shared contracts: subagent seam, tool replay, failure markers, trigger detection, trace normalization.

Classes moved verbatim from the PR-named test files (test_audit_fixes,
test_roadmap_features, test_followup_features, test_external_review_gaps,
test_cbc) and test_skill_benchmark, which accreted by merge rather than by
subject; docstrings citing finding/roadmap ids are preserved.
"""
import argparse
import contextlib
import io
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import skill_benchmark as sb
import run_pi_trigger_eval as tr
import ablation_model as am
from helpers import (
    CODEX_CRASH_OUTPUT as CRASH,
    CONTAINS_APPROVED_CASE as CASE,
    demo_manifest as base_manifest,
    good_pr_manifest as _manifest,
    load_example_module,
    make_eval_repo,
    report_fixture,
    stub_claude,
    write_demo_manifest as write_manifest,
    write_good_pr_skill as _skill,
    write_run,
)

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

    def test_subagent_is_a_registered_workspace_builder(self):
        self.assertIn("subagent", sb.WORKSPACE_BUILDERS)


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
        records = [{"type": "usage", "usage": {"input_tokens": 100, "output_tokens": 40}}]
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
        self.assertEqual((am.CODEX_FAILURE, am.JETTY_FAILURE, am.CLAUDE_FAILURE, am.TIMEOUT_FAILURE), am.RUNNER_FAILURE_MARKERS)

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
        read_it = json.dumps({"type": "tool_use", "name": "Read", "input": {"file_path": "/ws/skills/root-0/SKILL.md"}})
        invoked, evidence = sb.detect_trigger(read_it, [sp])
        self.assertTrue(invoked)
        self.assertTrue(evidence)
        never = json.dumps({"type": "tool_use", "name": "Read", "input": {"file_path": "/ws/inputs/data.csv"}})
        self.assertEqual(sb.detect_trigger(never, [sp]), (False, []))   # mounted but unread => False

    def test_trigger_eval_uses_the_one_owner(self):
        import run_pi_trigger_eval as tr
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
                "import json, sys\n_ = sys.stdin.read()\n"
                "print(json.dumps({'role': 'assistant', 'content': 'token from codex',"
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
                "import json, sys\n_ = sys.stdin.read()\n"
                "print(json.dumps({'role': 'assistant', 'content': 'token from codex'}))\n",
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

    def test_write_runner_outcome_derives_none_answer_from_trace(self):
        # answer=None means "the answer is the final trace message" (the Codex seam).
        with tempfile.TemporaryDirectory() as td:
            base = Path(td) / "run"
            trace = json.dumps({"role": "assistant", "content": "derived answer"})
            outcome = am.RunnerOutcome(provider="codex", answer=None, returncode=0, trace_text=trace)
            sb.write_runner_outcome(base, outcome)
            self.assertEqual((base / "output.md").read_text(encoding="utf-8"), "derived answer")
            self.assertTrue((base / "trace.jsonl").exists())

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
            sb.write_runner_outcome(base, outcome)
            text = (base / "output.md").read_text(encoding="utf-8")
            self.assertNotIn("LEAKED FROM TRACE", text)
            self.assertTrue(text.lstrip().startswith(am.CLAUDE_FAILURE))
            self.assertFalse(am.execution_valid({"returncode": 0}, text))

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
            self.assertIn("no output produced", (base / "output.md").read_text(encoding="utf-8"))
            meta = json.loads((base / "metadata.json").read_text(encoding="utf-8"))
            self.assertEqual(meta["usage_normalized"], {"source": "missing"})
            self.assertEqual(meta["cost_normalized"], {"source": "missing"})
            self.assertEqual(json.loads((base / "metrics.json").read_text())["schema_version"], 2)


if __name__ == "__main__":
    unittest.main()
