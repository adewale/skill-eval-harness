"""Closed invocation results shared by native and shell judge backends."""
from __future__ import annotations

import math
import unittest
from dataclasses import FrozenInstanceError

import judge_contracts as jc


class JudgeInvocationTests(unittest.TestCase):
    def test_valid_invocation_is_frozen_recursively(self):
        invocation = jc.JudgeInvocation(
            stdout='{"passed":true}',
            stderr="diagnostic",
            returncode=0,
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
            stdout="", stderr="provider failed", returncode=17)
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
                values = {"stdout": "", "stderr": "", "returncode": 0}
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
                values = {"stdout": "", "stderr": "", "returncode": 0}
                values.update(overrides)
                jc.JudgeInvocation(**values)


if __name__ == "__main__":
    unittest.main()
