"""First-class Claude adapter: parse the `claude -p --output-format json`
envelope in one place, capture real cost/usage into metrics.json, and total it
in the benchmark report."""
import argparse
import json
import stat
import tempfile
import unittest
from pathlib import Path

from helpers import claude_stream_records as _canonical_stream_records
from helpers import make_eval_repo
from helpers import stub_claude as _stub_claude
from helpers import stub_claude_stream as _stub_claude_stream

import skill_benchmark as sb


def claude_stream_records(**overrides) -> list[dict]:
    """The shared canonical stream fixture (helpers.claude_stream_records) with
    this file's assertion-friendly envelope values pinned."""
    return _canonical_stream_records(answer="All tests pass.", cost=0.05,
                                     in_tok=39, out_tok=13, **overrides)


def stream_text(records: list[dict]) -> str:
    return "\n".join(json.dumps(r) for r in records) + "\n"


def _manifest(rp: Path, cases):
    # Shared builder: writes the demo skill AND the manifest.
    return make_eval_repo(rp.parent, skill_name="demo", cases=cases)


class ParseClaudeEnvelopeTests(unittest.TestCase):
    def test_parses_result_cost_and_usage(self):
        out = json.dumps({"result": "hello", "total_cost_usd": 0.04,
                          "usage": {"input_tokens": 3, "output_tokens": 7,
                                    "cache_read_input_tokens": 9}})
        p = sb.parse_claude_cli_json(out)
        self.assertEqual(p["answer"], "hello")
        self.assertEqual(p["cost_usd"], 0.04)
        self.assertEqual(p["usage"]["input_tokens"], 3)
        self.assertEqual(p["usage"]["output_tokens"], 7)
        self.assertEqual(p["usage"]["total_tokens"], 10)          # derived
        self.assertEqual(p["usage"]["cache_read_tokens"], 9)

    def test_non_envelope_is_diagnostic_not_answer_evidence(self):
        p = sb.parse_claude_cli_json("just raw text, not json")
        self.assertEqual(p["answer"], "")
        self.assertEqual(p["raw_response"], "just raw text, not json")
        self.assertIsNone(p["cost_usd"])
        self.assertIsNotNone(p["parse_error"])

    def test_non_string_result_is_protocol_error_not_answer(self):
        p = sb.parse_claude_cli_json(json.dumps({"result": {"message": "diagnostic"}}))
        self.assertEqual(p["answer"], "")
        self.assertEqual(p["parse_error"], "claude result must be a string")

    def test_tolerates_fenced_json_envelope(self):
        out = "```json\n" + json.dumps({"result": "x", "total_cost_usd": 0.01, "usage": {}}) + "\n```"
        p = sb.parse_claude_cli_json(out)
        self.assertEqual(p["answer"], "x")
        self.assertEqual(p["cost_usd"], 0.01)


class ParseClaudeStreamTests(unittest.TestCase):
    """`--output-format stream-json` resolves through the SAME parser: the
    terminal type:"result" event carries the envelope fields, so the runner and
    the judge keep one owner for Claude's wire format."""

    def test_stream_terminal_result_event_is_the_envelope(self):
        p = sb.parse_claude_cli_json(stream_text(claude_stream_records()))
        self.assertEqual(p["answer"], "All tests pass.")
        self.assertEqual(p["cost_usd"], 0.05)
        self.assertEqual(p["usage"]["input_tokens"], 39)
        self.assertEqual(p["usage"]["output_tokens"], 13)
        self.assertEqual(p["usage"]["total_tokens"], 52)
        self.assertIsNone(p["parse_error"])

    def test_stream_without_result_event_is_protocol_failure(self):
        # A stream that dies before its terminal result event is diagnostics,
        # never answer evidence — same rule as the single-envelope path.
        p = sb.parse_claude_cli_json(stream_text(claude_stream_records(result_event=False)))
        self.assertEqual(p["answer"], "")
        self.assertIsNotNone(p["parse_error"])

    def test_last_result_event_wins(self):
        # Claude emits exactly one result event; if a retrying wrapper ever
        # concatenates two streams, the LAST terminal event is authoritative.
        records = claude_stream_records() + claude_stream_records()
        records[-1] = {**records[-1], "result": "second attempt", "total_cost_usd": 0.09}
        p = sb.parse_claude_cli_json(stream_text(records))
        self.assertEqual(p["answer"], "second attempt")
        self.assertEqual(p["cost_usd"], 0.09)


class ClaudeStreamTraceNormalizationTests(unittest.TestCase):
    """The claude trace dialect flattens message content blocks into
    normalizer-native records: a tool_use OPENS a call (in progress), its
    tool_result COMPLETES it, and only the terminal result event carries usage.
    An orphaned call therefore counts zero, per the completed-events-only
    metrics contract."""

    def test_paired_tool_use_counts_once_and_orphan_counts_zero(self):
        events_doc, metrics = sb.normalize_trace_records(
            claude_stream_records(orphan_tool=True), source="claude")
        events = events_doc["events"]
        commands = [e for e in events if e.get("type") == "command"]
        completed_commands = [e for e in commands if e.get("status") == "completed"]
        self.assertEqual(len(completed_commands), 1)
        self.assertIn("npm test", completed_commands[0]["input_summary"])
        self.assertEqual(metrics["commands"], 1)
        # the orphaned Grep call never resolved: present as in_progress, uncounted
        grep_events = [e for e in events if e.get("name") == "Grep"]
        self.assertTrue(grep_events)
        self.assertTrue(all(e.get("status") == "in_progress" for e in grep_events))

    def test_unmatched_tool_result_is_error_evidence_not_a_completed_call(self):
        records = [{"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "missing", "content": "orphan"}]}}]
        events_doc, metrics = sb.normalize_trace_records(records, source="claude")
        self.assertEqual(metrics["tool_calls"], 0)
        self.assertEqual(metrics["errors"], 1)
        self.assertEqual(len(events_doc["events"]), 1)
        event = events_doc["events"][0]
        self.assertEqual((event["type"], event["status"]), ("error", "failed"))
        self.assertIn("unmatched Claude tool_result", event["input_summary"])
        self.assertEqual(event["raw_ref"], {"file": "trace.jsonl", "line": 1})
        self.assertEqual(event["raw_result_ref"], {"file": "trace.jsonl", "line": 1})

    def test_duplicate_open_tool_id_is_error_and_cannot_replace_first_call(self):
        records = [
            {"type": "assistant", "message": {"content": [
                {"type": "tool_use", "id": "same", "name": "Read",
                 "input": {"file_path": "first.txt"}}]}},
            {"type": "assistant", "message": {"content": [
                {"type": "tool_use", "id": "same", "name": "Write",
                 "input": {"file_path": "second.txt"}}]}},
            {"type": "user", "message": {"content": [
                {"type": "tool_result", "tool_use_id": "same", "content": "first"}]}},
        ]
        events_doc, metrics = sb.normalize_trace_records(records, source="claude")
        completed = [event for event in events_doc["events"]
                     if event.get("status") == "completed"]
        self.assertEqual(len(completed), 1)
        self.assertEqual(completed[0]["type"], "file_read")
        self.assertIn("first.txt", completed[0]["input_summary"])
        self.assertNotIn("second.txt", completed[0]["input_summary"])
        self.assertEqual(metrics["errors"], 1)
        self.assertTrue(any("duplicate open Claude tool_use" in event.get("input_summary", "")
                            for event in events_doc["events"]))

    def test_tool_use_without_id_is_error_not_an_open_call(self):
        records = [{"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "Read", "input": {"file_path": "x"}}]}}]
        events_doc, metrics = sb.normalize_trace_records(records, source="claude")
        self.assertEqual(metrics["tool_calls"], 0)
        self.assertEqual(metrics["errors"], 1)
        self.assertEqual(events_doc["events"][0]["status"], "failed")

    def test_completed_tool_id_cannot_be_reused_later(self):
        records = [
            {"type": "assistant", "message": {"content": [
                {"type": "tool_use", "id": "same", "name": "Read",
                 "input": {"file_path": "first.txt"}}]}},
            {"type": "user", "message": {"content": [
                {"type": "tool_result", "tool_use_id": "same", "content": "ok"}]}},
            {"type": "assistant", "message": {"content": [
                {"type": "tool_use", "id": "same", "name": "Write",
                 "input": {"file_path": "second.txt"}}]}},
        ]
        events_doc, metrics = sb.normalize_trace_records(records, source="claude")
        self.assertEqual(metrics["file_reads"], 1)
        self.assertEqual(metrics["errors"], 1)
        self.assertTrue(any("duplicate reused Claude tool_use" in event.get("input_summary", "")
                            for event in events_doc["events"]))

    def test_skill_md_read_is_skill_load_evidence(self):
        _, metrics = sb.normalize_trace_records(claude_stream_records(), source="claude")
        self.assertTrue(metrics["skill_invoked"])
        self.assertTrue(any("SKILL.md" in ev for ev in metrics["skill_invocation_evidence"]))

    def test_usage_is_counted_once_from_the_terminal_result_event(self):
        # Per-assistant-message usage is API-request-level and would double
        # count; only the cumulative terminal usage may feed token metrics.
        _, metrics = sb.normalize_trace_records(claude_stream_records(), source="claude")
        self.assertEqual(metrics["input_tokens"], 39)
        self.assertEqual(metrics["output_tokens"], 13)
        self.assertEqual(metrics["total_tokens"], 52)

    def test_raw_ref_points_at_the_original_stream_line(self):
        records = claude_stream_records()
        events_doc, _ = sb.normalize_trace_records(records, source="claude")
        completed = next(e for e in events_doc["events"]
                         if e.get("type") == "command" and e.get("status") == "completed")
        # The completed action points to its invocation arguments; the result is
        # retained separately so per-step judging receives both records.
        self.assertEqual(completed["raw_ref"], {"file": "trace.jsonl", "line": 2})
        self.assertEqual(completed["raw_result_ref"], {"file": "trace.jsonl", "line": 3})

    def test_physical_raw_refs_survive_filtered_jsonl_lines(self):
        records = claude_stream_records()[:3]
        trace = "\n[]\n" + stream_text(records)
        with tempfile.TemporaryDirectory() as td:
            events_doc, _ = sb.write_trace_artifacts(Path(td), trace, source="claude")
        completed = next(e for e in events_doc["events"]
                         if e.get("type") == "command" and e.get("status") == "completed")
        self.assertEqual(completed["raw_ref"]["line"], 4)
        self.assertEqual(completed["raw_result_ref"]["line"], 5)

    def test_file_tools_keep_file_taxonomy(self):
        records = [
            {"type": "assistant", "message": {"content": [
                {"type": "tool_use", "id": "r", "name": "Read", "input": {"file_path": "notes.md"}}]}},
            {"type": "user", "message": {"content": [
                {"type": "tool_result", "tool_use_id": "r", "content": "notes"}]}},
            {"type": "assistant", "message": {"content": [
                {"type": "tool_use", "id": "w", "name": "Write", "input": {"file_path": "out.md"}}]}},
            {"type": "user", "message": {"content": [
                {"type": "tool_result", "tool_use_id": "w", "content": "ok"}]}},
        ]
        events_doc, metrics = sb.normalize_trace_records(records, source="claude")
        completed_types = [e["type"] for e in events_doc["events"] if e["status"] == "completed"]
        self.assertEqual(completed_types, ["file_read", "file_write"])
        self.assertEqual((metrics["file_reads"], metrics["file_writes"]), (1, 1))

    def test_tool_result_error_is_preserved_and_counted(self):
        records = [
            {"type": "assistant", "message": {"content": [
                {"type": "tool_use", "id": "b", "name": "Bash", "input": {"command": "false"}}]}},
            {"type": "user", "message": {"content": [
                {"type": "tool_result", "tool_use_id": "b", "content": "failed", "is_error": True}]}},
        ]
        events_doc, metrics = sb.normalize_trace_records(records, source="claude")
        completed = next(e for e in events_doc["events"] if e["status"] == "completed")
        self.assertTrue(completed["is_error"])
        self.assertIn("error.type", completed["otel"])
        self.assertEqual(metrics["errors"], 1)

    def test_trajectory_step_has_untruncated_invocation_and_result(self):
        pattern = "x" * 2500 + "NEEDLE"
        records = [
            {"type": "assistant", "message": {"content": [
                {"type": "tool_use", "id": "g", "name": "Grep", "input": {"pattern": pattern}}]}},
            {"type": "user", "message": {"content": [
                {"type": "tool_result", "tool_use_id": "g", "content": "match"}]}},
        ]
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            events_doc, _ = sb.write_trace_artifacts(base, stream_text(records), source="claude")
            steps = sb.trajectory_steps(events_doc["events"], base)
        self.assertIn("NEEDLE", steps[0]["raw"])
        self.assertIn('"name": "Grep"', steps[0]["raw"])
        self.assertIn("match", steps[0]["raw_result"])

    def test_final_assistant_text_is_a_message_event(self):
        events_doc, _ = sb.normalize_trace_records(claude_stream_records(), source="claude")
        messages = [e for e in events_doc["events"] if e.get("type") == "message"]
        self.assertTrue(any("All tests pass." in (e.get("input_summary") or "") for e in messages))

    def test_generic_sources_keep_the_identity_flatten(self):
        records = [{"type": "command", "command": "ls", "status": "completed"}]
        events_doc, metrics = sb.normalize_trace_records(records, source="generic")
        self.assertEqual(len(events_doc["events"]), 1)
        self.assertEqual(events_doc["events"][0]["raw_ref"]["line"], 1)
        self.assertEqual(metrics["commands"], 1)


class RunClaudeAdapterTests(unittest.TestCase):
    def _run(self, td: Path, *, cost=0.0123, returncode=0, answer="STUB ANSWER token-XYZ"):
        rp = td / "repo"
        case = {"id": "c", "split": "tune", "prompt": "do it",
                "assertions": [{"name": "a", "type": "contains", "value": "token-XYZ"}]}
        p = _manifest(rp, [case])
        rows = [r for r in sb.prepared_task_rows(p, sb.validate_manifest(p)) if r["variant"] == "with_skill"]
        tasks = td / "tasks.jsonl"
        tasks.write_text("".join(json.dumps(r) + "\n" for r in rows))
        stub = _stub_claude(td / "claude_stub.py", cost=cost, returncode=returncode, answer=answer)
        runs = td / "runs"
        ns = argparse.Namespace(tasks=str(tasks), runs=str(runs),
                                model="claude-haiku-4-5-20251001", claude_bin=str(stub), timeout=60)
        sb.run_claude(ns)
        return p, runs, rows[0]["run_dir"]

    def test_writes_output_and_cost_metrics(self):
        with tempfile.TemporaryDirectory() as t:
            td = Path(t)
            _, runs, run_dir = self._run(td)
            base = runs / run_dir
            self.assertIn("token-XYZ", (base / "output.md").read_text())
            metrics = json.loads((base / "metrics.json").read_text())
            self.assertEqual(metrics["cost_usd"], 0.0123)
            self.assertEqual(metrics["input_tokens"], 11)
            self.assertEqual(metrics["output_tokens"], 22)
            self.assertEqual(metrics["total_tokens"], 33)
            meta = json.loads((base / "metadata.json").read_text())
            self.assertEqual(meta["provider"], "claude")
            self.assertEqual(meta["model"], "claude-haiku-4-5-20251001")

    def test_nonzero_returncode_marks_infra_failure(self):
        with tempfile.TemporaryDirectory() as t:
            td = Path(t)
            _, runs, run_dir = self._run(td, returncode=3)
            text = (runs / run_dir / "output.md").read_text()
            self.assertTrue(text.lstrip().startswith(sb.CLAUDE_FAILURE))
            # and it is recognized as a non-scorable infra failure
            self.assertFalse(sb.execution_valid({"returncode": 3}, text))

    def test_json_is_error_marks_infra_failure_even_with_zero_exit(self):
        with tempfile.TemporaryDirectory() as t:
            td = Path(t)
            stub = td / "quota_stub.py"
            stub.write_text(
                "#!/usr/bin/env python3\nimport json\n"
                "print(json.dumps({'type':'result','is_error':True,'api_error_status':429,"
                "'result':'limit reached','total_cost_usd':0,'usage':{}}))\n",
                encoding="utf-8")
            stub.chmod(stub.stat().st_mode | stat.S_IXUSR)
            _, runs, run_dir = self._run(td, cost=0.0, answer="unused")
            # Re-run the same prepared task through the quota-shaped stub.
            tasks = td / "tasks.jsonl"
            sb.run_claude(argparse.Namespace(tasks=str(tasks), runs=str(runs), model="claude-haiku-4-5-20251001", claude_bin=str(stub), timeout=60))
            base = runs / run_dir
            text = (base / "output.md").read_text()
            meta = json.loads((base / "metadata.json").read_text())
            self.assertEqual(meta["returncode"], 1)
            self.assertTrue(text.lstrip().startswith(sb.CLAUDE_FAILURE))
            self.assertFalse(sb.execution_valid(meta, text))

    def test_zero_exit_malformed_envelope_is_protocol_failure(self):
        with tempfile.TemporaryDirectory() as t:
            td = Path(t)
            malformed = td / "malformed_claude.py"
            malformed.write_text("#!/usr/bin/env python3\nprint('plain diagnostic, not an envelope')\n",
                                 encoding="utf-8")
            malformed.chmod(malformed.stat().st_mode | stat.S_IXUSR)
            _, runs, run_dir = self._run(td)
            sb.run_claude(argparse.Namespace(
                tasks=str(td / "tasks.jsonl"), runs=str(runs),
                model="claude-haiku-4-5-20251001", claude_bin=str(malformed), timeout=60))
            base = runs / run_dir
            text = (base / "output.md").read_text(encoding="utf-8")
            meta = sb.read_metadata_base(base)
            self.assertEqual(meta["returncode"], 0)
            self.assertTrue(meta["process_observation_complete"])
            self.assertFalse(meta["provider_response_complete"])
            self.assertTrue(meta["artifact_set_complete"])
            self.assertTrue(text.lstrip().startswith(sb.CLAUDE_FAILURE))
            self.assertNotIn("plain diagnostic", text)
            self.assertFalse(sb.execution_valid(meta, text))

    def test_benchmark_totals_cost(self):
        with tempfile.TemporaryDirectory() as t:
            td = Path(t)
            p, runs, _ = self._run(td, cost=0.02)
            report = sb.build_benchmark_report(p, runs, split="tune", variants_arg=["with_skill"])
            self.assertAlmostEqual(report["summary"]["with_skill"]["cost_usd_total"], 0.02, places=6)

    def test_stream_json_run_writes_a_real_trace(self):
        # The answer runner requests stream-json (the stub refuses otherwise),
        # so events.json carries the run's actual tool-use trajectory and
        # process assertions have evidence on Claude answer runs.
        with tempfile.TemporaryDirectory() as t:
            td = Path(t)
            rp = td / "repo"
            case = {"id": "c", "split": "tune", "prompt": "do it",
                    "assertions": [{"name": "a", "type": "contains", "value": "token-XYZ"}]}
            p = _manifest(rp, [case])
            rows = [r for r in sb.prepared_task_rows(p, sb.validate_manifest(p)) if r["variant"] == "with_skill"]
            tasks = td / "tasks.jsonl"
            tasks.write_text("".join(json.dumps(r) + "\n" for r in rows))
            stub = _stub_claude_stream(td / "claude_stream_stub.py", cost=0.031)
            runs = td / "runs"
            sb.run_claude(argparse.Namespace(tasks=str(tasks), runs=str(runs),
                                             model="claude-haiku-4-5-20251001",
                                             claude_bin=str(stub), timeout=60))
            base = runs / rows[0]["run_dir"]
            self.assertIn("token-XYZ", (base / "output.md").read_text())
            trace = (base / "trace.jsonl").read_text(encoding="utf-8")
            self.assertIn("tool_use", trace)   # raw provider stream is preserved verbatim
            events = json.loads((base / "events.json").read_text())["events"]
            completed_commands = [e for e in events
                                  if e.get("type") == "command" and e.get("status") == "completed"]
            self.assertEqual(len(completed_commands), 1)
            metrics = json.loads((base / "metrics.json").read_text())
            self.assertEqual(metrics["commands"], 1)
            self.assertTrue(metrics["skill_invoked"])
            # provider-reported usage/cost still win over trace-derived counts
            self.assertEqual(metrics["input_tokens"], 11)
            self.assertEqual(metrics["cost_usd"], 0.031)
            meta = sb.read_metadata_base(base)
            self.assertEqual(meta["usage_normalized"]["source"], "provider_reported")
            env = json.loads((base / "environment.json").read_text())
            self.assertIn("stream-json", env["command"])
            # the point of the change: a process assertion has evidence to grade
            passed, evidence = sb.process_or_efficiency_assertion_result(
                {"type": "command_ran", "pattern": "npm test"}, base, meta)
            self.assertTrue(passed, evidence)


class ClaudeJudgeAndPanelTests(unittest.TestCase):
    def test_native_claude_judge_stamps_model_and_cost(self):
        with tempfile.TemporaryDirectory() as t:
            td = Path(t)
            out = td / "output.md"; out.write_text("candidate answer")
            # stub emits a VERDICT json inside result (what a judge returns)
            stub = _stub_claude(td / "judge_stub.py",
                                answer=json.dumps({"passed": True, "score": 1}), cost=0.0051)
            task = {"judge_task_id": "c::with_skill::run-1::quality", "case_id": "c",
                    "variant": "with_skill", "run_number": 1, "output_path": str(out),
                    "assertion": {"name": "quality", "type": "judge", "threshold": 1},
                    "prompt": "grade it"}
            row = sb.run_one_judge_task(task, None, judge_model="claude-haiku-4-5-20251001",
                                        claude_bin=str(stub))
            self.assertTrue(row["passed"])
            self.assertEqual(row["judge_model"], "claude-haiku-4-5-20251001")
            self.assertEqual(row["cost_usd"], 0.0051)

    def test_panel_flags_magnitude_sensitivity(self):
        # good-pr: both judges positive, but Sonnet sees a much bigger lift
        haiku = {"summary": {"with_skill": {"mean_combined_pass_rate": 0.92},
                             "without_skill": {"mean_combined_pass_rate": 0.89}}}   # +0.03
        sonnet = {"summary": {"with_skill": {"mean_combined_pass_rate": 0.79},
                              "without_skill": {"mean_combined_pass_rate": 0.61}}}  # +0.18
        s = sb.judge_panel_sensitivity({"haiku": haiku, "sonnet": sonnet})
        self.assertFalse(s["sign_sensitive"])                 # both positive
        self.assertTrue(s["magnitude_sensitive"])             # spread 0.15 > 0.1
        self.assertTrue(s["judge_sensitive"])
        self.assertAlmostEqual(s["lift_by_judge"]["haiku"], 0.03, places=6)

    def test_panel_flags_sign_disagreement(self):
        a = {"summary": {"with_skill": {"mean_combined_pass_rate": 0.6},
                         "without_skill": {"mean_combined_pass_rate": 0.55}}}       # +0.05
        b = {"summary": {"with_skill": {"mean_combined_pass_rate": 0.5},
                         "without_skill": {"mean_combined_pass_rate": 0.55}}}       # -0.05
        s = sb.judge_panel_sensitivity({"a": a, "b": b})
        self.assertTrue(s["sign_sensitive"])
        self.assertTrue(s["judge_sensitive"])

    def test_panel_agreement_is_not_sensitive(self):
        a = {"summary": {"with_skill": {"mean_combined_pass_rate": 0.9},
                         "without_skill": {"mean_combined_pass_rate": 0.78}}}       # +0.12
        b = {"summary": {"with_skill": {"mean_combined_pass_rate": 0.8},
                         "without_skill": {"mean_combined_pass_rate": 0.68}}}       # +0.12
        s = sb.judge_panel_sensitivity({"a": a, "b": b})
        self.assertFalse(s["judge_sensitive"])


if __name__ == "__main__":
    unittest.main()
