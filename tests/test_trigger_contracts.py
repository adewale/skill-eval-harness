"""Finite-state and recorded-wire proofs for trigger correctness by construction."""
import unittest
from pathlib import Path

import skill_benchmark as sb
from run_pi_trigger_eval import pi_invocation_outcome
from trigger_contracts import (
    CompletionEvidence,
    InvocationOutcome,
    InvocationState,
    TriggerDetection,
    TriggerEvidenceKind,
    TriggerExpectation,
    TriggerObservation,
)

FIXTURES = Path(__file__).parent / "fixtures" / "pi"


class InvocationOutcomeInvariantTests(unittest.TestCase):
    def test_process_constructor_classifies_every_exit_family(self):
        cases = [
            (0, InvocationState.COMPLETE, True, False),
            (1, InvocationState.PROCESS_FAILED, False, False),
            (124, InvocationState.TIMED_OUT, False, True),
            (127, InvocationState.SPAWN_FAILED, False, False),
        ]
        for returncode, state, complete, timed_out in cases:
            with self.subTest(returncode=returncode):
                outcome = InvocationOutcome.from_process(
                    stdout="", stderr="", returncode=returncode, elapsed_ms=0,
                )
                self.assertIs(outcome.state, state)
                self.assertIs(outcome.observation_complete, complete)
                self.assertIs(outcome.timed_out, timed_out)

    def test_direct_constructor_rejects_every_contradictory_state(self):
        invalid = [
            dict(returncode=1, state=InvocationState.COMPLETE),
            dict(returncode=124, state=InvocationState.COMPLETE,
                 completion_evidence=CompletionEvidence.AGENT_WINDOW_EXHAUSTED),
            dict(returncode=127, state=InvocationState.COMPLETE,
                 completion_evidence=CompletionEvidence.AGENT_WINDOW_EXHAUSTED),
            dict(returncode=0, state=InvocationState.TIMED_OUT),
            dict(returncode=1, state=InvocationState.SPAWN_FAILED),
            dict(returncode=0, state=InvocationState.PROCESS_FAILED),
            dict(returncode=0, state=InvocationState.PROVIDER_FAILED),
            dict(returncode=124, state=InvocationState.PROVIDER_FAILED, provider_error="error"),
            dict(returncode=0, state=InvocationState.HARNESS_FAILED),
        ]
        for fields in invalid:
            with self.subTest(fields=fields), self.assertRaises((TypeError, ValueError)):
                InvocationOutcome(stdout="", stderr="", elapsed_ms=0, **fields)

    def test_nonzero_completion_requires_the_named_agent_window_transition(self):
        failed = InvocationOutcome.from_process(
            stdout='{"type":"result","subtype":"error_max_turns"}\n',
            stderr="", returncode=1, elapsed_ms=2,
        )
        complete = failed.as_agent_window_complete()
        self.assertTrue(complete.observation_complete)
        self.assertEqual(complete.returncode, 1)
        self.assertIs(complete.completion_evidence, CompletionEvidence.AGENT_WINDOW_EXHAUSTED)
        with self.assertRaises(ValueError):
            InvocationOutcome.from_process(stdout="", stderr="", returncode=0, elapsed_ms=0).as_agent_window_complete()
        for reserved_code in (124, 127):
            with self.subTest(reserved_code=reserved_code), self.assertRaises(ValueError):
                InvocationOutcome.from_legacy_dict("fake", {
                    "stdout": "", "stderr": "", "returncode": reserved_code,
                    "timed_out": False, "elapsed_ms": 1, "observation_complete": True,
                    "completion_evidence": "agent_window_exhausted",
                }, allow_nonzero_complete=True)

    def test_legacy_boundary_rejects_truthy_strings_and_state_contradictions(self):
        base = {
            "stdout": "", "stderr": "", "returncode": 0,
            "timed_out": False, "elapsed_ms": 1, "observation_complete": True,
        }
        invalid = [
            {**base, "timed_out": "false"},
            {**base, "observation_complete": "false"},
            {**base, "elapsed_ms": None},
            {**base, "returncode": 124, "timed_out": False, "observation_complete": False},
            {**base, "observation_complete": False},
        ]
        for row in invalid:
            with self.subTest(row=row), self.assertRaises((TypeError, ValueError)):
                InvocationOutcome.from_legacy_dict("fake", row)

    def test_harness_failure_preserves_unmeasured_elapsed_time_as_unavailable(self):
        outcome = InvocationOutcome.harness_failed("fixture setup failed")
        self.assertIsNone(outcome.returncode)
        self.assertIsNone(outcome.elapsed_ms)
        self.assertFalse(outcome.observation_complete)

    def test_metadata_is_immutable_and_cannot_shadow_derived_fields(self):
        outcome = InvocationOutcome.from_process(
            stdout="", stderr="", returncode=0, elapsed_ms=0,
        ).with_metadata({"config_isolated": True})
        with self.assertRaises(TypeError):
            outcome.metadata["config_isolated"] = False  # type: ignore[index]
        for reserved in ("stdout", "returncode", "timed_out", "observation_complete", "provider_error"):
            with self.subTest(reserved=reserved), self.assertRaises(ValueError):
                outcome.with_metadata({reserved: "collision"})
        wire = outcome.as_legacy_dict()
        self.assertEqual(wire["returncode"], 0)
        self.assertFalse(wire["timed_out"])
        self.assertTrue(wire["observation_complete"])


class PiStreamContractTests(unittest.TestCase):
    def test_recorded_success_stream_is_parsed_once_as_cumulative_usage(self):
        raw = (FIXTURES / "lifecycle-success.jsonl").read_text(encoding="utf-8")
        stream = sb.PiStream.parse(raw)
        self.assertIsNone(stream.terminal_error)
        self.assertEqual(stream.usage_normalized["total_tokens"], 15)
        self.assertEqual(stream.cost_normalized["total_cost"], 0.015)
        detection = sb.detect_trigger_records(stream.records, [Path("/tmp/pi-config/skills/demo/SKILL.md")])
        self.assertTrue(detection.triggered)
        _, metrics = sb.normalize_trace_records(list(stream.records), source="pi", pi_stream=stream)
        self.assertEqual(metrics["total_tokens"], 15)
        self.assertEqual(metrics["tool_calls"], 1)

    def test_exit_zero_without_a_final_agent_end_is_a_protocol_failure(self):
        for raw in (
            '{"type":"agent_start"}\n',
            '{"type":"turn_end","message":{"role":"assistant","stopReason":"stop"}}\n',
        ):
            with self.subTest(raw=raw):
                process = InvocationOutcome.from_process(
                    stdout=raw, stderr="", returncode=0, elapsed_ms=1,
                )
                invocation = pi_invocation_outcome(process)
                self.assertIs(invocation.state, InvocationState.PROVIDER_FAILED)
                self.assertIn("without a final agent_end", invocation.provider_error or "")
                self.assertEqual(invocation.provider_payload.usage_normalized, {"source": "missing"})

    def test_malformed_json_fails_even_when_a_valid_terminal_event_exists(self):
        raw = 'not-json\n{"type":"agent_end","messages":[{"role":"assistant","stopReason":"stop","usage":{"totalTokens":3}}]}\n'
        invocation = pi_invocation_outcome(InvocationOutcome.from_process(
            stdout=raw, stderr="", returncode=0, elapsed_ms=1,
        ))
        self.assertIs(invocation.state, InvocationState.PROVIDER_FAILED)
        self.assertIn("parse error", invocation.provider_error or "")
        self.assertEqual(invocation.provider_payload.usage_normalized, {"source": "missing"})

    def test_successful_retry_uses_only_the_final_attempt(self):
        raw = (FIXTURES / "retry-then-success.jsonl").read_text(encoding="utf-8")
        invocation = pi_invocation_outcome(InvocationOutcome.from_process(
            stdout=raw, stderr="", returncode=0, elapsed_ms=1,
        ))
        self.assertIs(invocation.state, InvocationState.COMPLETE)
        self.assertIsNone(invocation.provider_error)
        self.assertEqual(invocation.provider_payload.usage_normalized["total_tokens"], 13)
        self.assertEqual(invocation.provider_payload.cost_normalized["total_cost"], 0.013)

    def test_exhausted_retries_use_the_final_provider_error(self):
        raw = (FIXTURES / "retries-exhausted.jsonl").read_text(encoding="utf-8")
        invocation = pi_invocation_outcome(InvocationOutcome.from_process(
            stdout=raw, stderr="", returncode=0, elapsed_ms=1,
        ))
        self.assertIs(invocation.state, InvocationState.PROVIDER_FAILED)
        self.assertEqual(invocation.provider_error, "final provider failure")
        self.assertEqual(invocation.provider_payload.usage_normalized, {"source": "missing"})

    def test_nonzero_process_with_terminal_provider_error_is_provider_failed(self):
        raw = (FIXTURES / "provider-error-exit-zero.jsonl").read_text(encoding="utf-8")
        invocation = pi_invocation_outcome(InvocationOutcome.from_process(
            stdout=raw, stderr="", returncode=1, elapsed_ms=1,
        ))
        self.assertIs(invocation.state, InvocationState.PROVIDER_FAILED)
        self.assertEqual(invocation.returncode, 1)

    def test_timeout_remains_a_timeout_when_its_partial_stream_has_no_terminal_event(self):
        process = InvocationOutcome.from_process(
            stdout='{"type":"agent_start"}\n', stderr="timeout", returncode=124, elapsed_ms=1,
        )
        invocation = pi_invocation_outcome(process)
        self.assertIs(invocation.state, InvocationState.TIMED_OUT)
        self.assertTrue(invocation.timed_out)
        self.assertEqual(invocation.provider_payload.usage_normalized, {"source": "missing"})

    def test_recorded_exit_zero_provider_error_becomes_one_failed_state(self):
        raw = (FIXTURES / "provider-error-exit-zero.jsonl").read_text(encoding="utf-8")
        process = InvocationOutcome.from_process(
            stdout=raw, stderr="", returncode=0, elapsed_ms=3,
        )
        invocation = pi_invocation_outcome(process)
        self.assertIs(invocation.state, InvocationState.PROVIDER_FAILED)
        self.assertFalse(invocation.observation_complete)
        self.assertIn("invalid model", invocation.provider_error or "")
        stream = invocation.provider_payload
        self.assertIsInstance(stream, sb.PiStream)
        self.assertEqual(stream.usage_normalized, {"source": "missing"})
        self.assertEqual(stream.cost_normalized, {"source": "missing"})

        observation = TriggerObservation(
            agent="pi", model="invalid", query="ordinary chat",
            expectation=TriggerExpectation.DO_NOT_TRIGGER,
            invocation=invocation,
            detection=TriggerDetection.absent(),
            usage={"source": "missing"}, cost={"source": "missing"},
        )
        self.assertFalse(observation.passed)
        with self.assertRaises(ValueError):
            TriggerObservation(
                agent="pi", model="invalid", query="ordinary chat",
                expectation=TriggerExpectation.DO_NOT_TRIGGER,
                invocation=invocation,
                detection=TriggerDetection.absent(),
                usage={"source": "trace_normalized", "total_tokens": 9},
                cost={"source": "missing"},
            )


class TriggerObservationTruthTableTests(unittest.TestCase):
    def _observation(self, *, usage, cost):
        return TriggerObservation(
            agent="pi", model=None, query="q",
            expectation=TriggerExpectation.DO_NOT_TRIGGER,
            invocation=InvocationOutcome.from_process(
                stdout="", stderr="", returncode=0, elapsed_ms=0,
            ),
            detection=TriggerDetection.absent(), usage=usage, cost=cost,
        )

    def test_available_telemetry_requires_typed_numeric_evidence(self):
        invalid_usage = [
            {"source": "provider_reported"},
            {"source": "provider_reported", "total_tokens": "banana"},
            {"source": "trace_normalized", "unknown_tokens": 1},
        ]
        for usage in invalid_usage:
            with self.subTest(usage=usage), self.assertRaises(ValueError):
                self._observation(usage=usage, cost={"source": "missing"})
        invalid_cost = [
            {"source": "provider_reported"},
            {"source": "provider_reported", "total_cost": "free", "currency": "USD"},
            {"source": "trace_normalized", "total_cost": 1, "currency": "usd"},
        ]
        for cost in invalid_cost:
            with self.subTest(cost=cost), self.assertRaises(ValueError):
                self._observation(usage={"source": "missing"}, cost=cost)
        valid = self._observation(
            usage={"source": "trace_normalized", "total_tokens": 0},
            cost={"source": "provider_reported", "total_cost": 0.0, "currency": "USD"},
        )
        self.assertEqual(valid.usage["total_tokens"], 0)
        self.assertEqual(valid.cost["total_cost"], 0.0)

    def test_pass_is_total_and_derived_for_every_finite_state_combination(self):
        complete = InvocationOutcome.from_process(
            stdout="", stderr="", returncode=0, elapsed_ms=0,
        )
        incomplete = InvocationOutcome.from_process(
            stdout="", stderr="failure", returncode=1, elapsed_ms=0,
        )
        absent = TriggerDetection.absent()
        present = TriggerDetection.from_texts(
            TriggerEvidenceKind.MOUNTED_PATH, ["/tmp/skills/demo/SKILL.md"],
        )
        for invocation in (complete, incomplete):
            for expectation in TriggerExpectation:
                for detection in (absent, present):
                    with self.subTest(state=invocation.state, expectation=expectation, triggered=detection.triggered):
                        observation = TriggerObservation(
                            agent="pi", model=None, query="q", expectation=expectation,
                            invocation=invocation, detection=detection,
                            usage={"source": "missing"}, cost={"source": "missing"},
                        )
                        expected = (
                            invocation.observation_complete
                            and detection.triggered == expectation.should_trigger
                        )
                        self.assertIs(observation.passed, expected)
                        row = observation.as_row()
                        self.assertIs(row["pass"], expected)
                        self.assertIs(row["observation_complete"], invocation.observation_complete)

    def test_observation_metadata_cannot_shadow_derived_row_fields(self):
        base = self._observation(usage={"source": "missing"}, cost={"source": "missing"})
        for reserved in ("returncode", "pass", "provider", "usage_normalized"):
            with self.subTest(reserved=reserved), self.assertRaises(ValueError):
                TriggerObservation(
                    agent=base.agent, model=base.model, query=base.query,
                    expectation=base.expectation, invocation=base.invocation,
                    detection=base.detection, usage=base.usage, cost=base.cost,
                    metadata={reserved: "collision"},
                )

    def test_nonzero_completion_and_each_evidence_kind_round_trip_through_json_row(self):
        invocation = InvocationOutcome.from_process(
            stdout="", stderr="", returncode=1, elapsed_ms=2,
        ).as_agent_window_complete()
        for kind in TriggerEvidenceKind:
            with self.subTest(kind=kind):
                detection = TriggerDetection.from_texts(kind, [f"evidence:{kind.value}"])
                original = TriggerObservation(
                    agent="claude", model="haiku", query="q",
                    expectation=TriggerExpectation.TRIGGER,
                    invocation=invocation, detection=detection,
                    usage={"source": "missing"}, cost={"source": "missing"},
                )
                row = original.as_row()
                self.assertEqual(row["completion_evidence"], "agent_window_exhausted")
                self.assertEqual(row["evidence_typed"], [{"kind": kind.value, "text": f"evidence:{kind.value}"}])
                restored = TriggerObservation.from_row(row)
                self.assertIs(restored.invocation.state, InvocationState.COMPLETE)
                self.assertIs(restored.invocation.completion_evidence, CompletionEvidence.AGENT_WINDOW_EXHAUSTED)
                self.assertIs(restored.detection.evidence[0].kind, kind)
                self.assertTrue(restored.passed)

    def test_persisted_boundary_rejects_population_and_evidence_disagreement(self):
        row = self._observation(
            usage={"source": "missing"}, cost={"source": "missing"},
        ).as_row()
        for mutation in (
            {"population": "answer"},
            {"evidence": ["legacy-only"]},
            {"triggered": True},
            {"timed_out": True},
            {"provider_error": "bad", "pass": False},
            {"provider_error": "bad", "pass": False,
             "completion_evidence": "agent_window_exhausted"},
        ):
            with self.subTest(mutation=mutation), self.assertRaises((TypeError, ValueError)):
                TriggerObservation.from_row({**row, **mutation})

    def test_triggered_cannot_disagree_with_evidence(self):
        absent = TriggerDetection.absent()
        present = TriggerDetection.from_texts(TriggerEvidenceKind.SKILL_TOOL, ["skill: demo"])
        self.assertFalse(absent.triggered)
        self.assertTrue(present.triggered)
        with self.assertRaises(TypeError):
            TriggerDetection(("untyped evidence",))  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
