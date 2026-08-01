"""Closed invocation results shared by native and shell judge backends."""
from __future__ import annotations

import math
import unittest
from dataclasses import FrozenInstanceError

import judge_contracts as jc
from invocation_contracts import InvocationState


class JudgeInvocationTests(unittest.TestCase):
    def test_valid_invocation_is_frozen_recursively(self):
        invocation = jc.JudgeInvocation(
            stdout='{"passed":true}',
            stderr="diagnostic",
            returncode=0,
            invocation_state=InvocationState.COMPLETE,
            usage={"input_tokens": 3, "cache": {"read_tokens": 1}},
            cost_usd=0.02,
            usage_source="trace_normalized",
            model_label="codex/gpt-mini",
        )

        self.assertTrue(invocation.succeeded)
        self.assertEqual(invocation.usage["input_tokens"], 3)
        with self.assertRaises(FrozenInstanceError):
            invocation.stdout = "changed"  # type: ignore[misc]
        with self.assertRaises(TypeError):
            invocation.usage["input_tokens"] = 4  # type: ignore[index]
        with self.assertRaises(TypeError):
            invocation.usage["cache"]["read_tokens"] = 2  # type: ignore[index]

    def test_nonzero_exit_is_an_explicit_unsuccessful_invocation(self):
        invocation = jc.JudgeInvocation(
            stdout="", stderr="provider failed", returncode=17,
            invocation_state=InvocationState.PROCESS_FAILED)
        self.assertFalse(invocation.succeeded)

    def test_wire_scalars_and_identity_are_closed(self):
        invalid = (
            {"stdout": None},
            {"stderr": b"bad"},
            {"returncode": None},
            {"returncode": False},
            {"returncode": 0.0},
            {"usage_source": "guessed"},
            {"model_label": ""},
            {"model_label": "   "},
        )
        for overrides in invalid:
            with self.subTest(overrides=overrides), self.assertRaises(
                    (TypeError, ValueError)):
                values = {"stdout": "", "stderr": "", "returncode": 0,
                          "invocation_state": InvocationState.COMPLETE}
                values.update(overrides)
                jc.JudgeInvocation(**values)

    def test_cost_and_usage_reject_invalid_measurements(self):
        invalid = (
            {"cost_usd": True},
            {"cost_usd": -0.01},
            {"cost_usd": math.inf},
            {"usage": []},
            {"usage": {"input_tokens": True}},
            {"usage": {"input_tokens": 1.5}},
            {"usage": {"cache": {"read_tokens": 3.0}}},
            {"usage": {"input_tokens": -1}},
            {"usage": {"input_tokens": math.nan}},
            {"usage": {"input_tokens": 2, "prompt_tokens": 3}},
            {"usage": {1: 2}},
        )
        for overrides in invalid:
            with self.subTest(overrides=overrides), self.assertRaises(
                    (TypeError, ValueError)):
                values = {"stdout": "", "stderr": "", "returncode": 0,
                          "invocation_state": InvocationState.COMPLETE}
                values.update(overrides)
                jc.JudgeInvocation(**values)

        with self.assertRaises(ValueError):
            jc.JudgeInvocation(
                stdout="", stderr="", returncode=0,
                invocation_state=InvocationState.COMPLETE, cost_usd=10 ** 400)

    def test_large_integer_usage_is_exact_and_surrogates_are_rejected(self):
        huge = 10 ** 400
        invocation = jc.JudgeInvocation(
            stdout="{}", stderr="", returncode=0,
            invocation_state=InvocationState.COMPLETE,
            usage={"input_tokens": huge})
        self.assertEqual(invocation.usage["input_tokens"], huge)
        with self.assertRaisesRegex(ValueError, "surrogate"):
            jc.JudgeInvocation(
                stdout="\ud800", stderr="", returncode=0,
                invocation_state=InvocationState.COMPLETE)

    def test_lifecycle_discriminant_preserves_exit_zero_provider_failure(self):
        invocation = jc.JudgeInvocation(
            stdout="malformed provider envelope", stderr="protocol failed",
            returncode=0, invocation_state=InvocationState.PROVIDER_FAILED,
            provider_error="protocol failed")
        self.assertFalse(invocation.succeeded)
        self.assertEqual(invocation.returncode, 0)

        contradictory = (
            (InvocationState.COMPLETE, 1),
            (InvocationState.TIMED_OUT, 0),
            (InvocationState.SPAWN_FAILED, 0),
            (InvocationState.PROCESS_FAILED, 0),
            (InvocationState.HARNESS_FAILED, 0),
            (InvocationState.PROVIDER_FAILED, 1),
        )
        for state, returncode in contradictory:
            with self.subTest(state=state), self.assertRaises(ValueError):
                jc.JudgeInvocation(
                    stdout="", stderr="", returncode=returncode,
                    invocation_state=state,
                    provider_error=("provider failed"
                                    if state is InvocationState.PROVIDER_FAILED else None))

        with self.assertRaisesRegex(ValueError, "provider-failed"):
            jc.JudgeInvocation(
                stdout="", stderr="", returncode=0,
                invocation_state=InvocationState.PROVIDER_FAILED)

        process_failure = jc.JudgeInvocation(
            stdout="", stderr="provider exited", returncode=17,
            invocation_state=InvocationState.PROCESS_FAILED,
            provider_error="provider reported a terminal error")
        self.assertFalse(process_failure.succeeded)


if __name__ == "__main__":
    unittest.main()
