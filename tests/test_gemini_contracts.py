"""Offline conformance tests for the official Gemini CLI wire contract."""
from __future__ import annotations

import json
import unittest
from pathlib import Path

from gemini_contracts import GeminiJsonResponse, GeminiStream, GeminiToolCall

FIXTURES = Path(__file__).parent / "fixtures" / "gemini"


def event(kind: str, **values: object) -> str:
    return json.dumps({
        "type": kind,
        "timestamp": "2026-07-31T09:00:00.000Z",
        **values,
    })


def stream(*records: str) -> str:
    return "\n".join(records) + "\n"


class GeminiStreamContractTests(unittest.TestCase):
    def test_upstream_conformance_fixture_is_accepted_without_invented_invariants(self):
        parsed = GeminiStream.parse(
            (FIXTURES / "tool-answer.stream.jsonl").read_text(encoding="utf-8"))

        self.assertTrue(parsed.complete)
        self.assertEqual(parsed.answer, "Final answer")
        self.assertEqual(len(parsed.tool_calls), 1)
        self.assertEqual(parsed.tool_calls[0].name, "read_file")

    def test_success_constructs_one_complete_answer_and_usage_observation(self):
        parsed = GeminiStream.parse(stream(
            event("init", session_id="session-1", model="gemini-2.5-flash"),
            event("message", role="user", content="Question"),
            event("message", role="assistant", content="Final ", delta=True),
            event("message", role="assistant", content="answer", delta=True),
            event("result", status="success", stats={
                "total_tokens": 13,
                "input_tokens": 8,
                "output_tokens": 5,
                "cached": 2,
                "input": 6,
                "duration_ms": 42,
                "tool_calls": 0,
                "models": {
                    "gemini-2.5-flash": {
                        "total_tokens": 13,
                        "input_tokens": 8,
                        "output_tokens": 5,
                        "cached": 2,
                        "input": 6,
                    },
                },
            }),
        ))

        self.assertTrue(parsed.complete)
        self.assertIsNone(parsed.protocol_error)
        self.assertEqual(parsed.answer, "Final answer")
        self.assertEqual(parsed.resolved_model, "gemini-2.5-flash")
        self.assertEqual(parsed.usage, {
            "input_tokens": 8,
            "output_tokens": 5,
            "total_tokens": 13,
            "cache_read_tokens": 2,
        })

    def test_tool_lifecycle_preserves_only_the_terminal_answer_segment(self):
        parsed = GeminiStream.parse(stream(
            event("init", session_id="session-1", model="gemini-2.5-pro"),
            event("message", role="user", content="Read it"),
            event("message", role="assistant", content="I will inspect it.", delta=True),
            event("tool_use", tool_name="read_file", tool_id="call-1",
                  parameters={"file_path": "fixture.txt"}),
            event("tool_result", tool_id="call-1", status="success", output="fixture"),
            event("message", role="assistant", content="The fixture is valid.", delta=True),
            event("result", status="success", stats={
                "total_tokens": 0, "input_tokens": 0, "output_tokens": 0,
                "cached": 0, "input": 0, "duration_ms": 50,
                # Gemini CLI's own nonInteractiveCli snapshot reports zero here
                # despite emitting the complete tool lifecycle above. Treat the
                # counter as telemetry, not an invented protocol invariant.
                "tool_calls": 0, "models": {},
            }),
        ))

        self.assertTrue(parsed.complete)
        self.assertEqual(parsed.answer, "The fixture is valid.")
        self.assertEqual(len(parsed.tool_calls), 1)
        self.assertEqual(parsed.tool_calls[0].name, "read_file")
        self.assertEqual(parsed.tool_calls[0].status, "success")

    def test_usage_can_be_absent_without_becoming_zero(self):
        parsed = GeminiStream.parse(stream(
            event("init", session_id="session-1", model="gemini-2.5-flash"),
            event("message", role="user", content="Question"),
            event("message", role="assistant", content="Answer", delta=True),
            event("result", status="success"),
        ))

        self.assertTrue(parsed.complete)
        self.assertIsNone(parsed.usage)

    def test_degenerate_streams_construct_explicit_protocol_failures(self):
        valid_init = event("init", session_id="session-1", model="gemini-2.5-flash")
        valid_answer = event("message", role="assistant", content="Answer", delta=True)
        valid_result = event("result", status="success")
        cases = {
            "malformed JSONL": "{not-json}\n",
            "non-object JSONL": "[]\n",
            "missing init": stream(valid_answer, valid_result),
            "missing terminal result": stream(valid_init, valid_answer),
            "unknown event": stream(valid_init, event("future_event"), valid_result),
            "dangling tool call": stream(
                valid_init,
                event("tool_use", tool_name="read_file", tool_id="call-1", parameters={}),
                valid_answer,
                valid_result,
            ),
            "missing final answer": stream(valid_init, valid_result),
        }
        for expected, raw in cases.items():
            with self.subTest(expected=expected):
                parsed = GeminiStream.parse(raw)
                self.assertFalse(parsed.complete)
                self.assertIn(expected, parsed.protocol_error or "")

    def test_error_event_with_success_terminal_is_provider_failure(self):
        parsed = GeminiStream.parse(stream(
            event("init", session_id="session-1", model="gemini-test"),
            event("error", severity="error", message="maximum turns reached"),
            event("message", role="assistant", content="partial", delta=True),
            event("result", status="success"),
        ))

        self.assertFalse(parsed.complete)
        self.assertEqual(parsed.provider_error, "maximum turns reached")
        self.assertIsNone(parsed.protocol_error)

    def test_terminal_warning_events_cannot_promote_partial_answers(self):
        for warning in (
                "Loop detected, stopping execution",
                "Agent execution stopped: stop hook"):
            with self.subTest(warning=warning):
                parsed = GeminiStream.parse(stream(
                    event("init", session_id="session-1", model="gemini-test"),
                    event("error", severity="warning", message=warning),
                    event("message", role="assistant", content="partial", delta=True),
                    event("result", status="success"),
                ))
                self.assertFalse(parsed.complete)
                self.assertEqual(parsed.provider_error, warning)
        blocked = "Agent execution blocked: policy hook"
        recovered = GeminiStream.parse(stream(
            event("init", session_id="session-1", model="gemini-test"),
            event("error", severity="warning", message=blocked),
            event("message", role="assistant", content="recovered", delta=True),
            event("result", status="success"),
        ))
        self.assertTrue(recovered.complete)
        self.assertIn(blocked, recovered.warnings)

    def test_surrogate_code_points_are_rejected_before_typed_construction(self):
        parsed_stream = GeminiStream.parse(
            '{"type":"init","timestamp":"t","session_id":"s",'
            '"model":"m"}\n'
            '{"type":"message","timestamp":"t","role":"assistant",'
            '"content":"\\ud800","delta":true}\n'
            '{"type":"result","timestamp":"t","status":"success"}\n')
        parsed_json = GeminiJsonResponse.parse(
            '{"session_id":"s","response":"\\ud800"}')

        self.assertIn("surrogate", parsed_stream.protocol_error or "")
        self.assertIn("surrogate", parsed_json.protocol_error or "")

    def test_provider_error_result_never_constructs_success(self):
        parsed = GeminiStream.parse(stream(
            event("init", session_id="session-1", model="gemini-2.5-pro"),
            event("message", role="user", content="Question"),
            event("result", status="error",
                  error={"type": "MaxSessionTurnsError", "message": "turn limit"}),
        ))

        self.assertFalse(parsed.complete)
        self.assertEqual(parsed.provider_error, "MaxSessionTurnsError: turn limit")

    def test_wire_optional_error_fields_do_not_invent_required_fields(self):
        recovered = GeminiStream.parse(stream(
            event("init", session_id="session-1", model="gemini-test"),
            event("tool_use", tool_name="read_file", tool_id="call-1",
                  parameters={"file_path": "missing.txt"}),
            event("tool_result", tool_id="call-1", status="error"),
            event("message", role="assistant", content="Recovered", delta=True),
            event("result", status="success"),
        ))
        terminal = GeminiStream.parse(stream(
            event("init", session_id="session-1", model="gemini-test"),
            event("error", severity="error", message="Invalid stream"),
            event("result", status="error"),
        ))

        self.assertTrue(recovered.complete)
        self.assertEqual(recovered.tool_calls[0].status, "error")
        self.assertIsNone(recovered.tool_calls[0].error)
        self.assertIsNone(terminal.protocol_error)
        self.assertEqual(terminal.provider_error, "Invalid stream")

    def test_frozen_values_close_mutable_constructor_and_whitespace_gaps(self):
        call = GeminiToolCall(
            call_id="call-1", name="read_file", parameters={},
            status="success")
        models = ["gemini-test"]
        calls = [call]
        parsed = GeminiStream(models=models, tool_calls=calls, answer="answer")
        models.append("ambient")
        calls.clear()

        self.assertEqual(parsed.models, ("gemini-test",))
        self.assertEqual(parsed.tool_calls, (call,))
        self.assertFalse(GeminiStream(answer="   ").complete)
        with self.assertRaises(ValueError):
            GeminiToolCall(
                call_id="call-2", name="read_file", parameters={},
                status="success", error="contradictory")
        with self.assertRaises(TypeError):
            GeminiStream(models="gemini-test", answer="answer")

    def test_multi_model_stream_does_not_claim_one_resolved_model(self):
        parsed = GeminiStream(
            configured_model="auto", models=("model-a", "model-b"),
            answer="answer")
        self.assertIsNone(parsed.resolved_model)

    def test_configured_model_without_provider_stats_is_not_resolved_model(self):
        parsed = GeminiStream(configured_model="auto", answer="answer")
        self.assertIsNone(parsed.resolved_model)


class GeminiJsonContractTests(unittest.TestCase):
    def test_max_turn_warning_is_provider_failure_even_with_partial_response(self):
        parsed = GeminiJsonResponse.parse(json.dumps({
            "session_id": "session-1",
            "response": '{"passed":true}',
            "warnings": ["Maximum session turns exceeded"],
        }))

        self.assertFalse(parsed.complete)
        self.assertEqual(parsed.provider_error, "Maximum session turns exceeded")
        self.assertIsNone(parsed.protocol_error)

    def test_terminal_json_warnings_are_provider_failures(self):
        for warning in (
                "Loop detected, stopping execution",
                "Agent execution stopped: stop hook"):
            with self.subTest(warning=warning):
                parsed = GeminiJsonResponse.parse(json.dumps({
                    "response": '{"passed":true}', "warnings": [warning],
                }))
                self.assertFalse(parsed.complete)
                self.assertEqual(parsed.provider_error, warning)
        blocked = "Agent execution blocked: policy hook"
        recovered = GeminiJsonResponse.parse(json.dumps({
            "response": '{"passed":true}', "warnings": [blocked],
        }))
        self.assertTrue(recovered.complete)
        self.assertIn(blocked, recovered.warnings)

    def test_absent_model_stats_do_not_construct_resolved_identity(self):
        parsed = GeminiJsonResponse.parse(json.dumps({
            "session_id": "session-1", "response": "answer",
        }))
        self.assertTrue(parsed.complete)
        self.assertIsNone(parsed.resolved_model)

    def test_upstream_json_formatter_fixture_preserves_multi_model_usage(self):
        parsed = GeminiJsonResponse.parse(
            (FIXTURES / "judge.json").read_text(encoding="utf-8"))

        self.assertTrue(parsed.complete)
        self.assertIsNone(parsed.resolved_model)
        self.assertEqual(parsed.usage, {
            "input_tokens": 45204,
            "output_tokens": 931,
            "total_tokens": 46376,
            "cache_read_tokens": 10656,
        })

    def test_json_response_preserves_final_answer_usage_and_model_resolution(self):
        parsed = GeminiJsonResponse.parse(json.dumps({
            "session_id": "session-1",
            "response": '{"passed":true,"evidence":"grounded"}',
            "stats": {
                "models": {
                    "gemini-2.5-flash": {
                        "api": {"totalRequests": 1, "totalErrors": 0,
                                "totalLatencyMs": 20},
                        "tokens": {
                            "input": 6, "prompt": 8, "candidates": 5,
                            "total": 13, "cached": 2, "thoughts": 0,
                            "tool": 0,
                        },
                        "roles": {},
                    },
                },
                "tools": {"totalCalls": 0},
                "files": {"totalLinesAdded": 0, "totalLinesRemoved": 0},
            },
        }))

        self.assertTrue(parsed.complete)
        self.assertEqual(parsed.response, '{"passed":true,"evidence":"grounded"}')
        self.assertEqual(parsed.resolved_model, "gemini-2.5-flash")
        self.assertEqual(parsed.usage, {
            "input_tokens": 8,
            "output_tokens": 5,
            "total_tokens": 13,
            "cache_read_tokens": 2,
        })

    def test_json_response_fails_closed_on_error_or_non_string_response(self):
        for raw, expected in (
            ({"error": {"type": "APIError", "message": "nope"}}, "APIError: nope"),
            ({"response": {"text": "wrong shape"}}, "response must be a non-empty string"),
            ({"response": ""}, "response must be a non-empty string"),
        ):
            with self.subTest(raw=raw):
                parsed = GeminiJsonResponse.parse(json.dumps(raw))
                self.assertFalse(parsed.complete)
                self.assertIn(expected, parsed.protocol_error or parsed.provider_error or "")

    def test_direct_json_values_freeze_and_validate_models(self):
        models = ["gemini-test"]
        parsed = GeminiJsonResponse(response="answer", models=models)
        models.append("ambient")
        self.assertEqual(parsed.models, ("gemini-test",))
        self.assertFalse(GeminiJsonResponse(response="   ").complete)
        with self.assertRaises(ValueError):
            GeminiJsonResponse(response="answer", models=("",))
        with self.assertRaises(TypeError):
            GeminiJsonResponse(response="answer", models="gemini-test")
        with self.assertRaises(ValueError):
            GeminiJsonResponse(response="answer", models=("gemini-\ud800",))
        with self.assertRaises(ValueError):
            GeminiJsonResponse(response="answer\ud800")


if __name__ == "__main__":
    unittest.main()
