"""Protocol laws for the Antigravity (`agy`) wire contract.

These tests encode the three defects PLAN.md names D1, D2 and D3.  They were
written before the parser existed and are expected to fail against the step-2
stub; each failure message names the invariant it protects rather than the
mechanism that happened to break.
"""
from __future__ import annotations

import json
import unittest
from pathlib import Path

from agy_contracts import (
    AGY_FILE_READ_TOOLS,
    AGY_SEARCH_TOOLS,
    AgyFileRead,
    AgySearch,
    AgySkillActivated,
    AgySkillNotActivated,
    AgySkillObservationUnavailable,
    AgyStream,
    AgyUsageAbsent,
    AgyUsageInvalid,
    AgyUsagePresent,
    observe_skill_activation,
)

FIXTURES = Path(__file__).parent / "fixtures" / "agy"

# The path a mounted demo skill occupies in the captured fixtures.
MOUNTED_SKILL = "/WORKSPACE/.agents/skills/demo/SKILL.md"


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _stream(*steps: dict[str, object]) -> str:
    """A well-formed stream carrying exactly the given step records.

    Everything around the steps is valid, so a test that fails is failing on
    the step under test rather than on the envelope.
    """
    records: list[dict[str, object]] = [
        {"event": "init", "conversation_id": "CONV",
         "init": {"model": "gemini-3.1-pro-low",
                  "tools": ["run_command", "view_file"]}}]
    records += [{"event": "step_update", "step_update": step}
                for step in steps]
    records.append(
        {"event": "result",
         "result": {"conversation_id": "CONV", "status": "SUCCESS",
                    "response": "done",
                    "usage": {"input_tokens": 10, "output_tokens": 5,
                              "total_tokens": 15}}})
    return "\n".join(json.dumps(record) for record in records)


class SearchIsNotActivation(unittest.TestCase):
    """D1 — searching for a skill is not the same as reading it."""

    def test_read_and_search_partitions_are_disjoint(self) -> None:
        overlap = set(AGY_FILE_READ_TOOLS) & set(AGY_SEARCH_TOOLS)
        self.assertEqual(
            overlap, set(),
            "a tool classified as both a file read and a search makes search "
            "intent indistinguishable from skill activation")

    def test_search_tools_are_not_classified_as_file_reads(self) -> None:
        stream = AgyStream.parse(fixture("stream-json-search-only.jsonl"))
        reads = [item for item in stream.tools if isinstance(item, AgyFileRead)]
        self.assertEqual(
            reads, [],
            "a run whose only skill-directory contact was grep_search and "
            "skill_search recorded a file read; search intent is being counted "
            "as a completed read")

    def test_search_only_run_produces_search_evidence(self) -> None:
        stream = AgyStream.parse(fixture("stream-json-search-only.jsonl"))
        searches = [item for item in stream.tools if isinstance(item, AgySearch)]
        self.assertEqual(
            [item.tool for item in searches], ["grep_search", "skill_search"],
            "completed search operations must still be observed, as searches")

    def test_search_only_run_is_not_recorded_as_activation(self) -> None:
        stream = AgyStream.parse(fixture("stream-json-search-only.jsonl"))
        observation = observe_skill_activation(stream, MOUNTED_SKILL)
        self.assertNotIsInstance(
            observation, AgySkillActivated,
            "a run that only searched for the skill was recorded as having "
            "activated it, which inflates the trigger matrix")

    def test_search_only_run_is_not_a_clean_negative_either(self) -> None:
        stream = AgyStream.parse(fixture("stream-json-search-only.jsonl"))
        observation = observe_skill_activation(stream, MOUNTED_SKILL)
        self.assertNotIsInstance(
            observation, AgySkillNotActivated,
            "a run that searched the skill directory without opening it is an "
            "incomplete observation, not proof the skill did not trigger")
        self.assertIsInstance(observation, AgySkillObservationUnavailable)

    def test_completed_read_of_the_mounted_skill_is_activation(self) -> None:
        stream = AgyStream.parse(fixture("stream-json-success.jsonl"))
        observation = observe_skill_activation(stream, MOUNTED_SKILL)
        self.assertIsInstance(
            observation, AgySkillActivated,
            "a completed view_file of the exact mounted SKILL.md path is the "
            "one thing that does count as activation")

    def test_a_run_that_used_tools_but_not_the_skill_is_a_clean_negative(
            self) -> None:
        # The other half of D1: "unknown" must not be scored as "did not
        # trigger", but neither may a clean negative be discarded as unknown.
        # Every ACTIVE record was held open forever, so any run that used any
        # tool at all before declining to read the skill became unavailable --
        # which deflates the trigger matrix exactly as counting a search as a
        # read inflates it.
        stream = AgyStream.parse(_stream(
            {"step_index": 4, "state": "ACTIVE", "step_type": "tool",
             "tool_name": "run_command",
             "tool_info": {"parameters": {"CommandLine": "ls"}}},
            {"step_index": 4, "state": "DONE", "step_type": "tool",
             "tool_name": "run_command",
             "tool_info": {"parameters": {"CommandLine": "ls"},
                           "output": "x\n"}}))
        self.assertIsInstance(
            observe_skill_activation(stream, MOUNTED_SKILL),
            AgySkillNotActivated,
            "a run whose every tool step completed, and which never opened "
            "the mounted skill, is a clean negative observation")


class AbsentTelemetryIsNotZero(unittest.TestCase):
    """D2 — a run that never reached a model reported no tokens, not zero."""

    def test_auth_failure_usage_is_missing_not_zero(self) -> None:
        stream = AgyStream.parse(
            fixture("stream-json-auth-failure.jsonl"), returncode=1)
        self.assertNotIsInstance(
            stream.usage, AgyUsagePresent,
            "an authentication failure that never reached a model published "
            "provider-reported token counters; absent telemetry must not "
            "become a zero-valued measurement")
        self.assertIsInstance(stream.usage, (AgyUsageAbsent, AgyUsageInvalid))

    def test_all_zero_counters_are_treated_as_absent(self) -> None:
        stream = AgyStream.parse(
            fixture("stream-json-auth-failure.jsonl"), returncode=1)
        counters = getattr(stream.usage, "counters", {})
        self.assertEqual(
            counters, {},
            "zero-filled counters were carried through as if measured")

    def test_real_usage_is_still_present(self) -> None:
        stream = AgyStream.parse(fixture("stream-json-success.jsonl"))
        self.assertIsInstance(
            stream.usage, AgyUsagePresent,
            "a successful run with real token counts must still report them; "
            "the D2 guard must not suppress genuine telemetry")

    def test_missing_usage_block_is_absent(self) -> None:
        stream = AgyStream.parse(fixture("stream-json-no-usage.jsonl"))
        self.assertIsInstance(stream.usage, AgyUsageAbsent)


class ProviderErrorSurvivesNonzeroExit(unittest.TestCase):
    """D3 — the structured diagnosis is not thrown away by the exit code."""

    def test_auth_failure_error_string_is_preserved(self) -> None:
        stream = AgyStream.parse(
            fixture("stream-json-auth-failure.jsonl"), returncode=1)
        self.assertEqual(
            stream.provider_error, "authentication failed or timed out",
            "agy exits 1 on an authentication failure while still emitting a "
            "structured error; gating the provider error on a zero exit code "
            "discards the only diagnosis the run produced")

    def test_zero_exit_still_reports_provider_error(self) -> None:
        stream = AgyStream.parse(
            fixture("stream-json-auth-failure.jsonl"), returncode=0)
        self.assertEqual(stream.provider_error,
                         "authentication failed or timed out")

    def test_auth_failure_is_not_a_complete_observation(self) -> None:
        stream = AgyStream.parse(
            fixture("stream-json-auth-failure.jsonl"), returncode=1)
        self.assertFalse(
            stream.complete,
            "a run that failed authentication must never present as a "
            "complete observation")


class ModelIdentityUncertainty(unittest.TestCase):
    """Review requirement 7 — zero, one and several models stay distinct."""

    def test_single_reported_model_resolves(self) -> None:
        stream = AgyStream.parse(fixture("stream-json-success.jsonl"))
        self.assertEqual(stream.model.reported, ("gemini-3.1-pro-low",))
        self.assertEqual(stream.model.resolved, "gemini-3.1-pro-low")

    def test_two_reported_models_do_not_collapse_to_the_first(self) -> None:
        stream = AgyStream.parse(fixture("stream-json-multi-model.jsonl"))
        self.assertEqual(
            stream.model.reported,
            ("gemini-3.1-pro-low", "gemini-3.1-pro-high"),
            "every reported model identity must be retained")
        self.assertIsNone(
            stream.model.resolved,
            "a stream reporting two different models has no single resolved "
            "model; collapsing to the first invents certainty")

    def test_no_reported_model_does_not_borrow_the_requested_one(self) -> None:
        stream = AgyStream.parse(
            fixture("stream-json-auth-failure.jsonl"), returncode=1,
            requested_model="gemini-3.1-pro-high")
        self.assertEqual(stream.model.reported, ())
        self.assertIsNone(
            stream.model.resolved,
            "a run that reported no model must not be labelled with the model "
            "the harness asked for")
        self.assertEqual(stream.model.requested, "gemini-3.1-pro-high")


class MalformedStreamsFailClosed(unittest.TestCase):
    """Every wire fixture that is not a clean run becomes an explicit failure."""

    def assert_incomplete(self, stream: AgyStream) -> None:
        self.assertFalse(stream.complete)
        self.assertTrue(stream.protocol_error or stream.provider_error)

    def test_truncated_line_is_a_protocol_error(self) -> None:
        stream = AgyStream.parse(fixture("stream-json-bad-line.jsonl"))
        self.assertIsNotNone(stream.protocol_error)
        self.assertIn("line 2", str(stream.protocol_error))
        self.assert_incomplete(stream)

    def test_a_truncated_stream_is_not_a_clean_negative_observation(self) -> None:
        stream = AgyStream.parse(fixture("stream-json-bad-line.jsonl"))
        self.assertIsInstance(
            observe_skill_activation(stream, MOUNTED_SKILL),
            AgySkillObservationUnavailable,
            "a stream that did not parse cannot prove the skill went unread")

    def test_failed_status_becomes_a_provider_error(self) -> None:
        stream = AgyStream.parse(fixture("stream-json-failed-status.jsonl"))
        self.assertIsNotNone(stream.provider_error)
        self.assert_incomplete(stream)

    def test_malformed_step_payload_is_rejected(self) -> None:
        stream = AgyStream.parse(fixture("stream-json-malformed-step.jsonl"))
        self.assertIsNotNone(stream.protocol_error)

    def test_missing_result_event_is_rejected(self) -> None:
        stream = AgyStream.parse(fixture("stream-json-no-result.jsonl"))
        self.assertEqual(stream.protocol_error, "missing terminal result event")
        self.assert_incomplete(stream)

    def test_nonstring_response_is_rejected(self) -> None:
        stream = AgyStream.parse(fixture("stream-json-nonstring-response.jsonl"))
        self.assertIsNotNone(stream.protocol_error)

    def test_unknown_event_is_rejected_rather_than_ignored(self) -> None:
        stream = AgyStream.parse(fixture("stream-json-unknown-event.jsonl"))
        self.assertIsNotNone(stream.protocol_error)
        self.assertIn("tool_invocation", str(stream.protocol_error))

    def test_empty_stream_is_rejected(self) -> None:
        self.assertEqual(AgyStream.parse("").protocol_error,
                         "agy stream is empty")

    def test_duplicate_object_keys_are_rejected(self) -> None:
        line = ('{"event":"result","event":"init","result":{"status":"SUCCESS",'
                '"response":"hi"}}')
        self.assertIsNotNone(AgyStream.parse(line).protocol_error)

    def test_non_finite_constants_are_rejected(self) -> None:
        line = ('{"event":"result","result":{"status":"SUCCESS","response":"hi",'
                '"duration_seconds":NaN}}')
        self.assertIsNotNone(AgyStream.parse(line).protocol_error)

    def test_a_started_tool_is_not_evidence(self) -> None:
        # One ACTIVE record and no DONE: nothing completed, so nothing is
        # evidence and the step stays open.
        stream = AgyStream.parse(_stream(
            {"step_index": 4, "state": "ACTIVE", "step_type": "tool",
             "tool_name": "run_command",
             "tool_info": {"parameters": {"CommandLine": "ls"}}}))
        self.assertEqual(stream.tools, (),
                         "an ACTIVE tool step is not completed evidence")
        self.assertEqual(
            [(started.tool, started.partition, started.command)
             for started in stream.incomplete_tools],
            [("run_command", "shell", "ls")],
            "a tool step with no DONE must be recorded as incomplete, with "
            "what it had already claimed")

    def test_a_completed_step_closes_the_active_record_that_opened_it(
            self) -> None:
        # The normal agy lifecycle is ACTIVE then DONE for one step_index, as
        # the success fixture emits for step 4. Leaving the ACTIVE recorded
        # made every run that used any tool at all look like it had left one
        # unfinished, which cost every clean negative observation.
        stream = AgyStream.parse(fixture("stream-json-success.jsonl"))
        self.assertEqual(stream.incomplete_tools, (),
                         "a step whose DONE arrived is not incomplete")
        self.assertEqual(
            [item for item in stream.tools
             if isinstance(item, AgySearch)], [],
            "the success fixture has no search tools")

    def test_a_completion_cannot_close_a_step_it_does_not_describe(
            self) -> None:
        # Reconciling on step identity alone would let a DONE close a step
        # started as a different tool.
        stream = AgyStream.parse(_stream(
            {"step_index": 4, "state": "ACTIVE", "step_type": "tool",
             "tool_name": "run_command",
             "tool_info": {"parameters": {"CommandLine": "ls"}}},
            {"step_index": 4, "state": "DONE", "step_type": "tool",
             "tool_name": "view_file",
             "tool_info": {"parameters": {"AbsolutePath": "/a"}}}))
        self.assertIsNotNone(stream.protocol_error)

    def test_a_step_with_no_index_cannot_be_reconciled(self) -> None:
        # Without a step_index there is nothing to match a DONE against, so
        # the ACTIVE record stays open rather than being closed by proximity.
        stream = AgyStream.parse(_stream(
            {"state": "ACTIVE", "step_type": "tool", "tool_name": "run_command",
             "tool_info": {"parameters": {"CommandLine": "ls"}}},
            {"state": "DONE", "step_type": "tool", "tool_name": "run_command",
             "tool_info": {"parameters": {"CommandLine": "ls"}}}))
        self.assertEqual([started.tool for started in stream.incomplete_tools],
                         ["run_command"])

    def test_one_step_cannot_report_two_terminal_updates(self) -> None:
        # The first DONE closes the open step; the second finds nothing open.
        # Appending both made one lifecycle into two tool calls, doubling every
        # counter the step fed -- silently, since each record on its own is
        # well formed.
        stream = AgyStream.parse(_stream(
            {"step_index": 4, "state": "ACTIVE", "step_type": "tool",
             "tool_name": "run_command",
             "tool_info": {"parameters": {"CommandLine": "ls"}}},
            {"step_index": 4, "state": "DONE", "step_type": "tool",
             "tool_name": "run_command",
             "tool_info": {"parameters": {"CommandLine": "ls"}}},
            {"step_index": 4, "state": "DONE", "step_type": "tool",
             "tool_name": "run_command",
             "tool_info": {"parameters": {"CommandLine": "ls"}}}))
        self.assertIsNotNone(
            stream.protocol_error,
            "a repeated terminal update for one step was accepted as a second "
            "tool call")

    def test_a_started_step_need_not_have_reported_its_parameters(self) -> None:
        # The lenience that makes the strictness above safe: an ACTIVE record
        # is a step caught mid-flight, so a missing CommandLine is not the
        # stream failure it is on a completed step.
        stream = AgyStream.parse(_stream(
            {"step_index": 4, "state": "ACTIVE", "step_type": "tool",
             "tool_name": "run_command"}))
        self.assertIsNone(stream.protocol_error)
        self.assertEqual(
            [(started.tool, started.command)
             for started in stream.incomplete_tools],
            [("run_command", "")])

    def test_a_started_step_with_an_unreadable_tool_info_fails(self) -> None:
        # Claimed activity this module cannot read fails the stream in either
        # state: the started step is published, so an unreadable one would be
        # published too.
        stream = AgyStream.parse(_stream(
            {"step_index": 4, "state": "ACTIVE", "step_type": "tool",
             "tool_name": "run_command", "tool_info": "ran the command"}))
        self.assertIsNotNone(stream.protocol_error)

    def test_a_started_step_invoking_an_unclassified_tool_fails(self) -> None:
        stream = AgyStream.parse(_stream(
            {"step_index": 4, "state": "ACTIVE", "step_type": "tool",
             "tool_name": "teleport_file"}))
        self.assertIsNotNone(
            stream.protocol_error,
            "an unclassified tool must fail the stream whether or not its "
            "step finished")

    def test_records_naming_two_conversations_are_not_one_observation(
            self) -> None:
        # A truncated conversation followed by a complete one: taking the
        # latest id credited the first conversation's tool evidence to the
        # second conversation's answer and usage, as a single complete run.
        stream = AgyStream.parse("\n".join(json.dumps(record) for record in [
            {"event": "init", "conversation_id": "CONV-A",
             "init": {"model": "gemini-3.1-pro-low"}},
            {"event": "step_update", "step_update": {
                "step_index": 4, "state": "DONE", "step_type": "tool",
                "tool_name": "run_command",
                "tool_info": {"parameters": {"CommandLine": "ls"}}}},
            {"event": "init", "conversation_id": "CONV-B",
             "init": {"model": "gemini-3.1-pro-low"}},
            {"event": "result", "result": {"status": "SUCCESS",
                                           "response": "done"}}]))
        self.assertIsNotNone(stream.protocol_error)
        self.assertFalse(stream.complete)

    def test_a_step_cannot_name_a_conversation_of_its_own(self) -> None:
        # The nested spelling, which re-scoped step identity mid-stream: after
        # it, no later DONE could close an earlier ACTIVE.
        stream = AgyStream.parse(_stream(
            {"step_index": 4, "state": "ACTIVE", "step_type": "tool",
             "conversation_id": "OTHER", "tool_name": "run_command",
             "tool_info": {"parameters": {"CommandLine": "ls"}}}))
        self.assertIsNotNone(stream.protocol_error)

    def test_contradictory_token_accounting_is_invalid(self) -> None:
        line = ('{"event":"result","result":{"status":"SUCCESS","response":"hi",'
                '"usage":{"input_tokens":10,"output_tokens":10,'
                '"total_tokens":5}}}')
        self.assertIsInstance(AgyStream.parse(line).usage, AgyUsageInvalid)

    def test_contradictory_token_accounting_is_not_absent_telemetry(self) -> None:
        line = ('{"event":"result","result":{"status":"SUCCESS","response":"hi",'
                '"usage":{"input_tokens":10,"output_tokens":10,'
                '"total_tokens":5}}}')
        stream = AgyStream.parse(line)
        self.assertIsNotNone(
            stream.protocol_error,
            "telemetry that contradicts itself is malformed provider "
            "evidence; reporting it as ordinary missing usage makes the two "
            "indistinguishable and leaves the answer scoreable")
        self.assertFalse(stream.complete)


class TheResultEventIsTerminalAndComplete(unittest.TestCase):
    """One result, carrying a status, ends the stream."""

    SUCCESS = ('{"event":"result","result":{"status":"SUCCESS",'
               '"response":"ok","usage":{"input_tokens":5,"output_tokens":5,'
               '"total_tokens":10}}}')

    def test_a_result_without_a_status_is_rejected(self) -> None:
        stream = AgyStream.parse(
            '{"event":"result","result":{"response":"ok"}}')
        self.assertIsNotNone(
            stream.protocol_error,
            "the status check is the only thing that turns a failed run into "
            "a provider error, so an optional status would score every "
            "failure whose status field went missing as a success")
        self.assertFalse(stream.complete)

    def test_a_second_result_cannot_overwrite_the_first(self) -> None:
        failed = ('{"event":"result","result":{"status":"FAILED",'
                  '"error":"boom"}}')
        stream = AgyStream.parse(failed + "\n" + self.SUCCESS)
        self.assertIsNotNone(
            stream.protocol_error,
            "two results are two observations concatenated; merging them "
            "field-by-field let a later success overwrite the answer and "
            "usage of the result that actually terminated the run")

    def test_no_evidence_is_collected_after_the_result(self) -> None:
        trailing = ('{"event":"step_update","step_update":{"state":"DONE",'
                    '"step_type":"tool","tool_name":"run_command","tool_info":'
                    '{"parameters":{"CommandLine":"echo late"}}}}')
        stream = AgyStream.parse(self.SUCCESS + "\n" + trailing)
        self.assertIsNotNone(
            stream.protocol_error,
            "a tool step after the terminal result is activity the stream "
            "cannot account for")
        self.assertFalse(stream.complete)

    def test_a_well_formed_single_result_still_parses(self) -> None:
        stream = AgyStream.parse(self.SUCCESS)
        self.assertIsNone(stream.protocol_error)
        self.assertTrue(stream.complete)
        self.assertEqual(stream.answer, "ok")


class ToolPartitionIsTotalAndDisjoint(unittest.TestCase):
    """No advertised tool may fall into a bucket by accident."""

    def test_the_five_buckets_do_not_overlap(self) -> None:
        import agy_contracts as agy
        buckets = {
            "shell": set(agy.AGY_SHELL_TOOLS),
            "file_read": set(agy.AGY_FILE_READ_TOOLS),
            "search": set(agy.AGY_SEARCH_TOOLS),
            "write": set(agy.AGY_WRITE_TOOLS),
            "generic": set(agy.AGY_GENERIC_TOOLS),
        }
        for left, left_tools in buckets.items():
            for right, right_tools in buckets.items():
                if left < right:
                    self.assertEqual(
                        left_tools & right_tools, set(),
                        f"{left} and {right} classify the same tool")

    def test_an_unclassified_tool_fails_the_stream(self) -> None:
        stream = AgyStream.parse(
            '{"event":"init","init":{"model":"m"}}\n'
            '{"event":"step_update","step_update":{"state":"DONE",'
            '"step_type":"tool","tool_name":"edit_file_v2","tool_info":'
            '{"parameters":{"TargetFile":"/w/f.py"}}}}\n'
            '{"event":"result","result":{"status":"SUCCESS","response":"ok",'
            '"usage":{"input_tokens":5,"output_tokens":5,"total_tokens":10}}}')
        self.assertIsNotNone(
            stream.protocol_error,
            "a tool in none of the five buckets must fail the stream: if it "
            "is a read or a write, defaulting it to a generic call leaves the "
            "run complete while its file counters silently under-report")
        self.assertFalse(stream.complete)

    def test_a_completed_read_with_no_usable_path_fails_the_stream(
            self) -> None:
        # Same harm as an unclassified tool name, so the same posture. A
        # `DONE` view_file whose path parameter was renamed used to degrade to
        # a generic call: the run stayed complete, `file_reads` published 0
        # alongside complete trace evidence, and the incompleteness it recorded
        # had no consumer in the answer or judge path.
        stream = AgyStream.parse(_stream(
            {"step_index": 1, "state": "DONE", "step_type": "tool",
             "tool_name": "view_file",
             "tool_info": {"parameters": {"Uri": "file:///w/f.py"}}}))
        self.assertIsNotNone(stream.protocol_error)
        self.assertFalse(stream.complete)
        self.assertIn("view_file", stream.protocol_error or "")
        self.assertIn("_PATH_PARAMETERS", stream.protocol_error or "",
                      "the failure must say where the parameter spelling is "
                      "added")

    def test_a_completed_write_with_no_usable_path_fails_the_stream(
            self) -> None:
        stream = AgyStream.parse(_stream(
            {"step_index": 1, "state": "DONE", "step_type": "tool",
             "tool_name": "write_to_file",
             "tool_info": {"parameters": {"Contents": "x"}}}))
        self.assertIsNotNone(stream.protocol_error)
        self.assertFalse(stream.complete)

    def test_a_completed_shell_step_with_no_command_fails_the_stream(
            self) -> None:
        stream = AgyStream.parse(_stream(
            {"step_index": 1, "state": "DONE", "step_type": "tool",
             "tool_name": "run_command",
             "tool_info": {"parameters": {"Cmd": "ls"}}}))
        self.assertIsNotNone(stream.protocol_error)
        self.assertFalse(stream.complete)

    def test_unreadable_completed_evidence_is_never_a_zero_valued_metric(
            self) -> None:
        # The property the fail-closed rule exists for, stated end to end:
        # incomplete evidence must not reach the metric as a complete count.
        stream = AgyStream.parse(_stream(
            {"step_index": 1, "state": "DONE", "step_type": "tool",
             "tool_name": "view_file",
             "tool_info": {"parameters": {"Uri": "file:///w/f.py"}}}))
        self.assertEqual(
            [item for item in stream.tools if isinstance(item, AgyFileRead)],
            [], "unreadable evidence produces no read")
        self.assertFalse(
            stream.complete,
            "a run whose completed tool evidence could not be read must not "
            "be publishable as a complete observation")

    def test_the_failure_names_the_tool_and_the_fix(self) -> None:
        stream = AgyStream.parse(
            '{"event":"step_update","step_update":{"state":"DONE",'
            '"step_type":"tool","tool_name":"edit_file_v2","tool_info":'
            '{"parameters":{"TargetFile":"/w/f.py"}}}}')
        error = stream.protocol_error or ""
        self.assertIn("edit_file_v2", error)
        self.assertIn("agy_contracts.py", error,
                      "the failure must say where the vocabulary is updated, "
                      "so absorbing an agy release is a lookup and not an "
                      "investigation")

    def test_the_advertised_vocabulary_survives_the_failure(self) -> None:
        # The init event names every tool the CLI offers, which is what makes
        # absorbing an agy release a single pass. Dropping it on the failing
        # stream would leave the operator rediscovering the new vocabulary one
        # failed run at a time.
        stream = AgyStream.parse(
            '{"event":"init","init":{"model":"m","tools":'
            '["view_file","edit_file_v2","grep_files"]}}\n'
            '{"event":"step_update","step_update":{"state":"DONE",'
            '"step_type":"tool","tool_name":"edit_file_v2","tool_info":'
            '{"parameters":{"TargetFile":"/w/f.py"}}}}')
        self.assertIsNotNone(stream.protocol_error)
        self.assertEqual(stream.unclassified_tools_advertised,
                         ("edit_file_v2", "grep_files"))

    def test_an_explicitly_generic_tool_still_parses(self) -> None:
        stream = AgyStream.parse(
            '{"event":"step_update","step_update":{"state":"DONE",'
            '"step_type":"tool","tool_name":"search_web","tool_info":'
            '{"parameters":{"Query":"agy release notes"}}}}\n'
            '{"event":"result","result":{"status":"SUCCESS","response":"ok",'
            '"usage":{"input_tokens":5,"output_tokens":5,"total_tokens":10}}}')
        self.assertIsNone(
            stream.protocol_error,
            "failing closed applies to unclassified names only; a tool "
            "deliberately filed as generic is a decision already made")
        self.assertTrue(stream.complete)

    def test_every_advertised_tool_is_classified(self) -> None:
        import json as _json

        import agy_contracts as agy
        advertised = _json.loads(
            (FIXTURES / "advertised-tools.json").read_text(encoding="utf-8"))
        unclassified = sorted(
            set(advertised["tools"]) - agy.AGY_CLASSIFIED_TOOLS)
        self.assertEqual(
            unclassified, [],
            "a tool agy advertises has no explicit classification, so it would "
            "silently default to a generic call")


if __name__ == "__main__":
    unittest.main()
