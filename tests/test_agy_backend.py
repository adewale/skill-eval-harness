"""Adapter laws for the Antigravity (`agy`) answer and judge surfaces.

`tests/test_agy_contracts.py` holds the wire protocol laws.  This file covers
what the adapter does with a parsed stream: how it builds argv, where it refuses
to build one at all, and which facts survive into a typed outcome.
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import skill_benchmark as sb
from invocation_contracts import InvocationResult
from trigger_contracts import InvocationState

FIXTURES = Path(__file__).parent / "fixtures" / "agy"


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def completed(stdout: str, *, returncode: int = 0) -> InvocationResult:
    return InvocationResult(
        stdout=stdout, stderr="", returncode=returncode, elapsed_ms=1200,
        invocation_state=(InvocationState.COMPLETE if returncode == 0
                          else InvocationState.PROCESS_FAILED),
        stdout_utf8_valid=True, stderr_utf8_valid=True,
    )


class TheCommandBoundaryIsOneToken(unittest.TestCase):
    """A launcher prefix cannot introduce flags the harness never sets."""

    def test_the_default_is_the_bare_executable(self) -> None:
        self.assertEqual(sb.agy_executable_token(None), "agy")

    def test_a_plain_path_is_accepted(self) -> None:
        self.assertEqual(sb.agy_executable_token("/opt/bin/agy"), "/opt/bin/agy")

    def test_a_continue_prefix_is_rejected_rather_than_honoured(self) -> None:
        with self.assertRaises(ValueError) as caught:
            sb.agy_executable_token("agy --continue")
        self.assertIn("--continue", str(caught.exception))

    def test_every_conversation_seeding_flag_is_refused(self) -> None:
        # The long names are what the plan called out; the short aliases are
        # what a denylist of long names would have missed. agy 1.1.9 exposes
        # -c for --continue and -p/--prompt for --print.
        for prefix in ("agy --continue", "agy -c", "agy --conversation ABC",
                       "agy --agent other", "agy --mode plan", "agy --effort high",
                       "agy --project P", "agy -p hi", "agy --prompt hi",
                       "agy -i", "agy --log-file /tmp/x"):
            with self.subTest(prefix=prefix), self.assertRaises(ValueError):
                sb.agy_executable_token(prefix)

    def test_an_install_path_containing_a_space_is_accepted(self) -> None:
        # Whitespace is not what makes a string a launcher prefix. agy can be
        # installed somewhere like `/Applications/Google Antigravity/agy`, and
        # the token reaches the process as one literal argv element, so
        # rejecting it blocked answer and judge runs on a valid installation.
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp) / "Google Antigravity"
            directory.mkdir()
            executable = directory / "agy"
            executable.touch()
            executable.chmod(0o755)
            self.assertEqual(sb.agy_executable_token(str(executable)),
                             str(executable))

    def test_a_spaced_string_that_is_not_an_executable_is_still_refused(
            self) -> None:
        # The exemption is "names a real executable", not "contains a slash":
        # a prefix must not become acceptable by pointing at a path that does
        # not exist.
        with self.assertRaises(ValueError):
            sb.agy_executable_token("/nonexistent/Google Antigravity/agy")

    def test_a_spaced_bare_name_is_refused_even_beside_a_matching_file(
            self) -> None:
        # What is checked must be what runs. A bare name is resolved by the OS
        # against PATH, so admitting one because a file of that name sits in
        # the current directory would vet a different file than the one that
        # ends up executing.
        with tempfile.TemporaryDirectory() as tmp:
            executable = Path(tmp) / "my agy"
            executable.touch()
            executable.chmod(0o755)
            cwd = os.getcwd()
            os.chdir(tmp)
            try:
                with self.assertRaises(ValueError):
                    sb.agy_executable_token("my agy")
            finally:
                os.chdir(cwd)

    def test_a_bare_flag_is_not_an_executable(self) -> None:
        with self.assertRaises(ValueError):
            sb.agy_executable_token("--continue")

    def test_empty_and_non_string_launchers_are_refused(self) -> None:
        with self.assertRaises(ValueError):
            sb.agy_executable_token("   ")
        with self.assertRaises(TypeError):
            sb.agy_executable_token(["agy"])  # type: ignore[arg-type]

    def test_an_invalid_launcher_never_becomes_a_process(self) -> None:
        with mock.patch.object(sb, "run_argv_capture") as spawn:
            result = sb.agy_cli_invoke(
                "hi", agy_cmd="agy --continue", cwd="/tmp", timeout=5)
        spawn.assert_not_called()
        self.assertEqual(result["invocation_state"],
                         InvocationState.SPAWN_FAILED.value)
        self.assertIn("--continue", result["protocol_error"])
        self.assertIsNone(result["usage"])


class ValuesAreBoundAsValues(unittest.TestCase):
    """Dash-prefixed data must reach the process as data, not as structure."""

    def test_a_dash_prefixed_prompt_is_one_argv_element(self) -> None:
        argv = sb.agy_cli_argv(prompt="--dangerously-skip-permissions",
                               cwd="/tmp")
        self.assertEqual(argv[argv.index("--print") + 1],
                         "--dangerously-skip-permissions")

    def test_a_dash_prefixed_model_is_one_argv_element(self) -> None:
        argv = sb.agy_cli_argv(prompt="hi", model="--sandbox", cwd="/tmp")
        self.assertEqual(argv[argv.index("--model") + 1], "--sandbox")

    def test_a_dash_prefixed_schema_is_one_argv_element(self) -> None:
        argv = sb.agy_cli_argv(prompt="hi", json_schema="--continue", cwd="/tmp")
        self.assertEqual(argv[argv.index("--json-schema") + 1], "--continue")

    def test_the_workspace_is_attached_to_the_run(self) -> None:
        argv = sb.agy_cli_argv(prompt="hi", cwd="/work")
        self.assertIn("--add-dir", argv)
        self.assertEqual(argv[argv.index("--add-dir") + 1], "/work")
        self.assertIn("--new-project", argv)

    def test_the_harness_timeout_is_pushed_into_the_cli(self) -> None:
        argv = sb.agy_cli_argv(prompt="hi", cwd="/work", timeout=900)
        self.assertEqual(argv[argv.index("--print-timeout") + 1], "900s")

    def test_slash_command_expansion_can_be_disabled(self) -> None:
        self.assertNotIn("--disable-slash-commands",
                         sb.agy_cli_argv(prompt="hi", cwd="/w"))
        self.assertIn("--disable-slash-commands",
                      sb.agy_cli_argv(prompt="hi", cwd="/w",
                                      disable_slash_commands=True))

    def test_the_prompt_is_redacted_from_recorded_commands(self) -> None:
        argv = sb.agy_cli_argv(prompt="secret question", cwd="/w")
        self.assertNotIn("secret question", sb.redact_agy_prompt_arg(argv))


class TheAnswerPathReportsWhatHappened(unittest.TestCase):
    def invoke(self, stdout: str, *, returncode: int = 0) -> dict:
        with mock.patch.object(sb, "run_argv_capture",
                               return_value=completed(stdout, returncode=returncode)):
            return sb.agy_cli_invoke("hi", cwd="/tmp", timeout=30)

    def test_a_successful_run_reports_answer_and_usage(self) -> None:
        result = self.invoke(fixture("stream-json-success.jsonl"))
        self.assertEqual(result["answer"], "The current stable version is 2.11.2.")
        self.assertEqual(result["usage"]["total_tokens"], 14439)
        self.assertEqual(result["model"], "gemini-3.1-pro-low")
        self.assertIsNone(result["provider_error"])

    def test_an_auth_failure_keeps_its_error_and_reports_no_usage(self) -> None:
        result = self.invoke(fixture("stream-json-auth-failure.jsonl"),
                             returncode=1)
        self.assertEqual(result["provider_error"],
                         "authentication failed or timed out")
        self.assertIsNone(
            result["usage"],
            "a run that never reached a model must not publish token counters")

    def test_an_auth_failure_is_a_provider_failure_outcome(self) -> None:
        with mock.patch.object(
                sb, "run_argv_capture",
                return_value=completed(fixture("stream-json-auth-failure.jsonl"),
                                       returncode=1)):
            outcome = sb.AgyBackend().invoke_answer(
                sb.InvocationRequest(prompt="hi", workspace=Path("/tmp"),
                                     model=None, timeout_s=30))
        self.assertEqual(outcome.reason, "authentication failed or timed out")
        self.assertNotEqual(outcome.returncode, 0)

    def test_a_run_that_reported_no_model_is_not_labelled_with_the_request(
            self) -> None:
        with mock.patch.object(
                sb, "run_argv_capture",
                return_value=completed(fixture("stream-json-auth-failure.jsonl"),
                                       returncode=1)):
            result = sb.agy_cli_invoke("hi", cwd="/tmp", timeout=30,
                                       model="gemini-3.1-pro-high")
        self.assertEqual(result["model_reported"], [],
                         "the run reported no model of its own")
        self.assertIsNone(result["model"],
                          "a run that never reached a model reported none")
        self.assertEqual(result["model_requested"], "gemini-3.1-pro-high",
                         "what was asked for is still recorded, separately")

    def test_two_reported_models_leave_the_identity_unresolved(self) -> None:
        result = self.invoke(fixture("stream-json-multi-model.jsonl"))
        self.assertEqual(result["model_reported"],
                         ["gemini-3.1-pro-low", "gemini-3.1-pro-high"])
        self.assertIsNone(result["model"],
                          "two reported models cannot collapse to one")

    def test_a_request_cannot_resolve_an_ambiguous_reported_identity(
            self) -> None:
        # The regression this closes: with `--model` supplied, the requested
        # name stood in for the resolved one, so zero-reported and
        # many-reported both published a confident single identity.
        with mock.patch.object(
                sb, "run_argv_capture",
                return_value=completed(
                    fixture("stream-json-multi-model.jsonl"))):
            result = sb.agy_cli_invoke("hi", cwd="/tmp", timeout=30,
                                       model="gemini-3.1-pro-low")
        self.assertIsNone(result["model"])
        self.assertEqual(result["model_requested"], "gemini-3.1-pro-low")
        self.assertEqual(result["model_reported"],
                         ["gemini-3.1-pro-low", "gemini-3.1-pro-high"])

    def test_both_model_identities_survive_into_the_outcome(self) -> None:
        # `model` alone leaves zero-reported and many-reported identical, so
        # the distinction only exists downstream if these travel with it.
        with mock.patch.object(
                sb, "run_argv_capture",
                return_value=completed(
                    fixture("stream-json-multi-model.jsonl"))):
            outcome = sb.AgyBackend().invoke_answer(
                sb.InvocationRequest(prompt="hi", workspace=Path("/tmp"),
                                     model="gemini-3.1-pro-low", timeout_s=30))
        context = sb.outcome_context(outcome)
        self.assertIsNone(context.model)
        self.assertEqual(context.metadata_extra["model_requested"],
                         "gemini-3.1-pro-low")
        self.assertEqual(list(context.metadata_extra["model_reported"]),
                         ["gemini-3.1-pro-low", "gemini-3.1-pro-high"])

    def test_a_truncated_stream_is_not_a_completed_run(self) -> None:
        result = self.invoke(fixture("stream-json-bad-line.jsonl"))
        self.assertIsNotNone(result["protocol_error"])

    def test_the_containment_exposure_is_recorded_on_every_run(self) -> None:
        result = self.invoke(fixture("stream-json-success.jsonl"))
        environment = result["environment"]
        self.assertFalse(environment["config_isolated"])
        self.assertFalse(environment["sandbox_contains_run"])
        self.assertTrue(environment["disposable_host_required"])
        self.assertEqual(environment["command_boundary"],
                         "single-executable-token")


VERDICT_SCHEMA = {
    "type": "object",
    "properties": {"passed": {"type": "boolean"}},
    "required": ["passed"],
}


class TheJudgePathUsesTheLifecycleFormat(unittest.TestCase):
    def judge(self, stdout: str, *, returncode: int = 0):
        with mock.patch.object(sb, "run_argv_capture",
                               return_value=completed(stdout, returncode=returncode)):
            return sb.agy_judge_invoke(
                "verdict?", judge_model=None, agy_cmd="agy",
                assertion_schema=VERDICT_SCHEMA, explore_hint="/tmp")

    def invoke_kwargs(self) -> dict:
        with mock.patch.object(sb, "agy_cli_invoke") as invoke:
            invoke.return_value = {
                "answer": "{}", "provider_error": None, "protocol_error": None,
                "stderr": "", "returncode": 0, "timed_out": False,
                "invocation_state": "complete", "elapsed_ms": 5,
                "usage": None, "model": None, "environment": {},
            }
            sb.agy_judge_invoke("verdict?", judge_model=None, agy_cmd="agy",
                                assertion_schema=VERDICT_SCHEMA)
        return invoke.call_args.kwargs

    def test_the_judge_runs_under_stream_json_with_tools_unapproved(self) -> None:
        kwargs = self.invoke_kwargs()
        self.assertEqual(kwargs["output"], "stream-json")
        self.assertFalse(
            kwargs["auto_approve"],
            "a judge reads and decides; it has no reason to auto-approve tools")

    def test_the_verdict_schema_reaches_the_cli(self) -> None:
        # The registry dispatches every judge with `assertion_schema=`. A
        # parameter named after the agy flag instead absorbed it into `**_`,
        # so `--json-schema` was silently dropped from every judge run while
        # the docs advertised it as applied.
        self.assertEqual(
            json.loads(self.invoke_kwargs()["json_schema"]), VERDICT_SCHEMA,
            "the judge's declared verdict schema never reached agy")

    def test_the_schema_is_bound_as_one_argv_value(self) -> None:
        kwargs = self.invoke_kwargs()
        argv = sb.agy_cli_argv("agy", prompt="verdict?", cwd="/tmp",
                               json_schema=kwargs["json_schema"])
        self.assertEqual(argv[argv.index("--json-schema") + 1],
                         kwargs["json_schema"])

    def test_the_judge_keeps_both_model_identities(self) -> None:
        # `model_label` is the resolved identity or an explicit statement that
        # there is none, so a judge run that reported two models is labelled
        # with neither, and the model that was asked for must still be
        # recoverable. Leaving the label empty was not neutral: the verdict
        # writer falls back to the requested model, so the run was persisted
        # and priced under a name agy never confirmed.
        with mock.patch.object(
                sb, "run_argv_capture",
                return_value=completed(
                    fixture("stream-json-multi-model.jsonl"))):
            invocation = sb.agy_judge_invoke(
                "verdict?", judge_model="gemini-3.1-pro-low", agy_cmd="agy",
                assertion_schema=VERDICT_SCHEMA, explore_hint="/tmp")
        self.assertEqual(invocation.model_label, "agy/multi-model")
        self.assertEqual(invocation.metadata["model_requested"],
                         "gemini-3.1-pro-low")
        self.assertEqual(list(invocation.metadata["model_reported"]),
                         ["gemini-3.1-pro-low", "gemini-3.1-pro-high"])

    def test_a_judge_that_reported_no_model_is_not_labelled_with_the_request(
            self) -> None:
        with mock.patch.object(
                sb, "run_argv_capture",
                return_value=completed(
                    fixture("stream-json-auth-failure.jsonl"), returncode=1)):
            invocation = sb.agy_judge_invoke(
                "verdict?", judge_model="gemini-3.1-pro-high", agy_cmd="agy",
                assertion_schema=VERDICT_SCHEMA, explore_hint="/tmp")
        self.assertEqual(invocation.model_label, "agy/unreported")
        self.assertEqual(invocation.metadata["model_requested"],
                         "gemini-3.1-pro-high")
        self.assertEqual(list(invocation.metadata["model_reported"]), [])

    def test_the_judge_records_the_approval_it_was_launched_with(self) -> None:
        with mock.patch.object(sb, "run_argv_capture",
                               return_value=completed("", returncode=0)):
            result = sb.agy_cli_invoke("verdict?", agy_cmd="agy", cwd="/tmp",
                                       auto_approve=False)
        self.assertFalse(
            result["environment"]["ambient_tools_auto_approved"],
            "an isolation audit must read the flag the run was launched "
            "with, not a module-level default")

    def test_an_answer_run_still_records_auto_approval(self) -> None:
        with mock.patch.object(sb, "run_argv_capture",
                               return_value=completed("", returncode=0)):
            result = sb.agy_cli_invoke("answer?", agy_cmd="agy", cwd="/tmp",
                                       auto_approve=True)
        self.assertTrue(result["environment"]["ambient_tools_auto_approved"])

    def test_a_judge_verdict_survives_the_stream_format(self) -> None:
        invocation = self.judge(fixture("stream-json-success.jsonl"))
        self.assertEqual(invocation.stdout,
                         "The current stable version is 2.11.2.")
        self.assertIsNone(invocation.provider_error)
        self.assertEqual(invocation.usage["total_tokens"], 14439)

    def test_a_judge_auth_failure_keeps_its_diagnosis(self) -> None:
        invocation = self.judge(fixture("stream-json-auth-failure.jsonl"),
                                returncode=1)
        self.assertEqual(invocation.provider_error,
                         "authentication failed or timed out")
        self.assertIsNone(invocation.usage)

    def test_no_permissive_json_parsing_survives_in_the_agy_path(self) -> None:
        import re

        source = Path(sb.__file__).read_text(encoding="utf-8")
        permissive = re.findall(r"json\.loads\([^)]*strict\s*=\s*False", source)
        self.assertEqual(
            permissive, [],
            "a JSON parser that accepts raw control characters is fail-open; "
            "the agy judge uses stream-json precisely so this is unnecessary")

    def test_the_agy_path_recovers_nothing_through_cast(self) -> None:
        import inspect
        for function in (sb.agy_cli_invoke, sb.agy_cli_argv,
                         sb.agy_executable_token):
            with self.subTest(function=function.__name__):
                self.assertNotIn(
                    "cast(", inspect.getsource(function),
                    "cast()-based recovery hides a boundary that should be "
                    "validated")


class TheTraceDialectSeparatesSearchFromReads(unittest.TestCase):
    def normalized(self, name: str) -> tuple[dict, dict]:
        records, errors = sb.parse_trace_jsonl_text(fixture(name))
        self.assertEqual(errors, [])
        return sb.normalize_trace_records(records, source="agy")

    def test_a_completed_read_is_counted_as_a_file_read(self) -> None:
        events, metrics = self.normalized("stream-json-success.jsonl")
        self.assertEqual(metrics["file_reads"], 1)
        # The shared normalizer promotes a completed read of a SKILL.md into a
        # skill_load event; that promotion is what trigger detection keys on,
        # and it is exactly what a search must never reach.
        loads = [event for event in events["events"]
                 if event["type"] == "skill_load"]
        self.assertEqual(len(loads), 1)
        self.assertIn("SKILL.md", loads[0]["input_summary"])

    def test_a_search_never_becomes_a_file_read(self) -> None:
        events, metrics = self.normalized("stream-json-search-only.jsonl")
        self.assertEqual(
            [event for event in events["events"]
             if event["type"] == "file_read"], [],
            "grep_search and skill_search are discovery, not reads")
        self.assertEqual(metrics.get("file_reads", 0), 0)

    def test_a_search_carries_no_path_to_be_mistaken_for_evidence(self) -> None:
        flat = sb._agy_tool_flat_record(sb.AgySearch(tool="grep_search"))
        self.assertNotIn("path", flat)
        self.assertEqual(flat["type"], "tool_call")

    def test_each_write_tool_keeps_its_own_name(self) -> None:
        # `tool_call.required_calls` matches exact tool names, so collapsing
        # four write tools onto `write_to_file` made an assertion for the tool
        # that ran miss while one for a tool that did not ran matched.
        from agy_contracts import AGY_WRITE_TOOLS
        for tool in AGY_WRITE_TOOLS:
            with self.subTest(tool=tool):
                flat = sb._agy_tool_flat_record(
                    sb.AgyFileWrite(path="/w/f.py", tool=tool))
                self.assertEqual(flat["name"], tool)
                self.assertEqual(flat["type"], "file_write")

    def test_a_write_normalizes_under_its_real_name(self) -> None:
        stream = (
            '{"event":"init","init":{"model":"m"}}\n'
            '{"event":"step_update","step_update":{"state":"DONE",'
            '"step_type":"tool","tool_name":"replace_file_content",'
            '"tool_info":{"parameters":{"TargetFile":"/w/f.py"}}}}\n'
            '{"event":"result","result":{"status":"SUCCESS","response":"ok",'
            '"usage":{"input_tokens":5,"output_tokens":5,"total_tokens":10}}}\n')
        records, errors = sb.parse_trace_jsonl_text(stream)
        self.assertEqual(errors, [])
        events, metrics = sb.normalize_trace_records(records, source="agy")
        names = [event["name"] for event in events["events"]
                 if event.get("type") == "file_write"]
        self.assertEqual(names, ["replace_file_content"])
        self.assertEqual(metrics["file_writes"], 1)

    def test_tool_events_cite_the_line_they_came_from(self) -> None:
        # Evidence is a filtered view of the stream: init records and non-tool
        # steps produce none. Indexing the line table by position in the
        # evidence list gave the first tool the init record's line.
        text = fixture("stream-json-success.jsonl")
        records, errors, lines = sb.parse_trace_jsonl_text_with_lines(text)
        self.assertEqual(errors, [])
        flat = sb.agy_stream_flat_records(records, record_lines=lines)
        physical = {
            record["name"] if record.get("name") else record["tool"]: line
            for line, record in flat}
        self.assertEqual(
            physical, {"run_command": 5, "view_file": 6},
            "each tool event must cite the physical line of the record that "
            "produced it")

    def test_line_refs_hold_when_tools_open_the_stream(self) -> None:
        text = (
            '{"event":"step_update","step_update":{"state":"DONE",'
            '"step_type":"tool","tool_name":"view_file","tool_info":'
            '{"parameters":{"AbsolutePath":"/w/a.md"}}}}\n'
            '{"event":"result","result":{"status":"SUCCESS","response":"ok",'
            '"usage":{"input_tokens":5,"output_tokens":5,"total_tokens":10}}}\n')
        records, _, lines = sb.parse_trace_jsonl_text_with_lines(text)
        flat = sb.agy_stream_flat_records(records, record_lines=lines)
        self.assertEqual([line for line, _ in flat], [1])

    def test_the_registry_projects_the_agy_dialect(self) -> None:
        self.assertIn("agy", sb.TRACE_DIALECTS)
        self.assertIs(sb.TRACE_DIALECTS["agy"], sb.AGY_TRACE_DIALECT)

    def test_search_only_trace_publishes_unavailable_skill_invoked(self) -> None:
        text = fixture("stream-json-search-only.jsonl")
        records, errors = sb.parse_trace_jsonl_text(text)
        self.assertEqual(errors, [])
        events, metrics = sb.normalize_trace_records(records, source="agy")
        self.assertIsNone(metrics.get("skill_invoked"))
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            (base / "events.json").write_text(json.dumps(events))
            (base / "metrics.json").write_text(json.dumps(metrics))
            res, msg = sb.process_or_efficiency_assertion_result(
                {"type": "skill_invoked", "expected": False}, base, metrics)
            self.assertIsNone(res)
            self.assertIn("unavailable", msg)

    def test_missing_started_command_text_makes_command_not_ran_unavailable(self) -> None:
        text = (
            '{"event":"step_update","step_update":{"state":"ACTIVE",'
            '"step_type":"tool","tool_name":"run_command","tool_info":{"parameters":{}}}}\n'
            '{"event":"result","result":{"status":"SUCCESS","response":"ok",'
            '"usage":{"input_tokens":5,"output_tokens":5,"total_tokens":10}}}\n')
        records, errors = sb.parse_trace_jsonl_text(text)
        self.assertEqual(errors, [])
        events, metrics = sb.normalize_trace_records(records, source="agy")
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            (base / "events.json").write_text(json.dumps(events))
            (base / "metrics.json").write_text(json.dumps(metrics))
            res, msg = sb.process_or_efficiency_assertion_result(
                {"type": "command_not_ran", "pattern": "rm -rf"}, base, metrics)
            self.assertIsNone(res)
            self.assertIn("unavailable", msg)


if __name__ == "__main__":
    unittest.main()
