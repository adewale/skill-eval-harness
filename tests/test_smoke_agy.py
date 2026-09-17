"""Opt-in live agy smoke: RUN_AGY_SMOKE=1 and an authenticated `agy`.

**Run this only on a disposable host.** agy cannot be contained at its CLI
surface: `--sandbox` restricts terminal operations only, and
`--dangerously-skip-permissions` auto-approves the sandbox-bypass prompt itself
(antigravity-cli#36), so non-terminal tools such as `write_to_file` are outside
the sandbox entirely. There is no config-home override (antigravity-cli#155), so
the invoking user's Antigravity configuration is in play. A run asked to write
outside its workspace succeeded on 1.1.8.

That is why this smoke is deliberately *not* part of
`scripts/smoke_supported_clis.py`, which sweeps every supported CLI in one
command: the choice to run an uncontained agent belongs to an operator, once,
explicitly.

    RUN_AGY_SMOKE=1 python3 -m unittest discover tests -k smoke_agy -v
"""
from __future__ import annotations

import os
import shutil
import unittest

import skill_benchmark as sb
from agent_capabilities import BACKENDS

SMOKE = os.environ.get("RUN_AGY_SMOKE") == "1"
AGY_ON_PATH = shutil.which("agy") is not None


@unittest.skipUnless(SMOKE, "set RUN_AGY_SMOKE=1 to run the live agy smoke")
@unittest.skipUnless(AGY_ON_PATH, "agy is not on PATH")
class LiveAgySmoke(unittest.TestCase):
    """One token-backed answer run, recorded as its own layer of evidence."""

    def test_the_backend_is_still_declared_uncontained(self) -> None:
        # If this ever fails, the posture was relaxed; re-read the containment
        # findings before trusting any result below.
        posture = BACKENDS["agy"].capabilities.isolation
        self.assertTrue(
            posture.disposable_host_required,
            "this smoke assumes an uncontained backend on a disposable host")

    def test_a_token_backed_answer_run_reports_real_usage(self) -> None:
        result = sb.agy_cli_invoke(
            "Reply with exactly: ok", timeout=180, cwd=os.getcwd())
        self.assertIsNone(result["protocol_error"], result["stderr"][:2000])
        self.assertIsNone(result["provider_error"], result["stderr"][:2000])
        self.assertTrue(result["answer"].strip(), "the run produced no answer")
        usage = result["usage"]
        self.assertIsNotNone(
            usage, "a token-backed run must report token counters")
        self.assertGreater(
            usage["total_tokens"], 0,
            "usage that survives normalization must be nonzero; an all-zero "
            "block is absent telemetry, not a measurement")

    def test_an_unauthenticated_run_reports_missing_usage(self) -> None:
        """The auth-failure layer, verified live rather than from a fixture."""
        result = sb.agy_cli_invoke(
            "hi", timeout=60, cwd=os.getcwd(),
            agy_cmd=os.environ.get("AGY_UNAUTHENTICATED_BIN", "agy"))
        if result["provider_error"] is None:
            self.skipTest("this host is authenticated; run the layer elsewhere")
        self.assertIsNone(
            result["usage"],
            "an authentication failure must not publish token counters")


if __name__ == "__main__":
    unittest.main()
