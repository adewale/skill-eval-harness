"""Laws for the registry's typed isolation/containment posture.

Capability booleans cannot express conditional safety such as "works only on a
disposable host".  These tests hold the vocabulary closed and stop a backend
that cannot be contained from quietly advertising unattended trigger runs.
"""
from __future__ import annotations

import unittest

import agent_capabilities as ac

CONTAINMENT_VOCABULARY = {
    "contained", "config_isolated_only", "uncontained_requires_disposable_host",
}
CONFIG_AUTHORITY_VOCABULARY = {
    "isolated_home_enforced", "isolated_home_conditional", "ambient_user_config",
    "not_applicable",
}


def capabilities(**overrides: object) -> ac.AgentCapabilities:
    base: dict[str, object] = {
        "answer_runner": True, "autonomous_trigger": False,
        "trigger_ablation": False, "trace_artifacts": True,
        "token_usage": False, "dollar_cost": "missing",
        "judge_backend": False, "tool_replay": False, "live_smoke_env": None,
        "elapsed_provenance": "process_measured",
        "isolation": ac.IsolationPosture(
            containment="contained", config_authority="not_applicable",
            reason="offline test double"),
    }
    base.update(overrides)
    return ac.AgentCapabilities(**base)  # type: ignore[arg-type]


class EveryBackendDeclaresAPosture(unittest.TestCase):
    def test_every_registered_backend_has_an_isolation_posture(self) -> None:
        for name, registration in ac.BACKENDS.items():
            with self.subTest(backend=name):
                posture = registration.capabilities.isolation
                self.assertIsInstance(
                    posture, ac.IsolationPosture,
                    f"{name} does not declare how far it can be contained")
                self.assertIn(posture.containment, CONTAINMENT_VOCABULARY)
                self.assertIn(posture.config_authority,
                              CONFIG_AUTHORITY_VOCABULARY)
                self.assertTrue(
                    posture.reason.strip(),
                    f"{name} declares a containment posture with no stated basis")

    def test_the_posture_reaches_the_serialized_capability_record(self) -> None:
        record = ac.BACKENDS["codex"].capabilities.as_dict()
        self.assertEqual(record["isolation"], {
            "containment": "contained",
            "config_authority": "isolated_home_enforced",
            "reason": ac.BACKENDS["codex"].capabilities.isolation.reason,
            "trigger_optin_env": None,
        })


class UncontainedBackendsCannotAdvertiseTrigger(unittest.TestCase):
    """The rule the whole posture exists to enforce."""

    UNCONTAINED: dict[str, str] = {
        "containment": "uncontained_requires_disposable_host",
        "config_authority": "ambient_user_config",
        "reason": "sandbox is bypassable and there is no config-home override",
    }

    def test_uncontained_backend_cannot_advertise_autonomous_trigger(self) -> None:
        with self.assertRaises(ValueError) as caught:
            capabilities(autonomous_trigger=True,
                         isolation=ac.IsolationPosture(**self.UNCONTAINED))
        self.assertIn("operator opt-in", str(caught.exception))

    def test_uncontained_backend_cannot_advertise_trigger_ablation(self) -> None:
        with self.assertRaises(ValueError):
            capabilities(trigger_ablation=True,
                         isolation=ac.IsolationPosture(**self.UNCONTAINED))

    def test_uncontained_backend_may_still_advertise_answer_and_judge(self) -> None:
        capability = capabilities(
            answer_runner=True, judge_backend=True,
            isolation=ac.IsolationPosture(**self.UNCONTAINED))
        self.assertTrue(capability.isolation.disposable_host_required)

    def test_an_explicit_operator_optin_permits_trigger(self) -> None:
        capability = capabilities(
            autonomous_trigger=True,
            isolation=ac.IsolationPosture(
                **self.UNCONTAINED, trigger_optin_env="ALLOW_UNCONTAINED_TRIGGER"))
        self.assertEqual(capability.isolation.trigger_optin_env,
                         "ALLOW_UNCONTAINED_TRIGGER")

    def test_no_shipped_backend_relies_on_the_optin_escape_hatch(self) -> None:
        for name, registration in ac.BACKENDS.items():
            with self.subTest(backend=name):
                self.assertIsNone(
                    registration.capabilities.isolation.trigger_optin_env,
                    f"{name} waives containment to advertise trigger support; "
                    "that escape hatch is for an operator, not a registry row")


class PostureVocabularyIsClosed(unittest.TestCase):
    def test_unknown_containment_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            ac.IsolationPosture(containment="mostly_fine",  # type: ignore[arg-type]
                                config_authority="not_applicable", reason="x")

    def test_unknown_config_authority_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            ac.IsolationPosture(containment="contained",
                                config_authority="probably",  # type: ignore[arg-type]
                                reason="x")

    def test_a_posture_needs_a_reason(self) -> None:
        with self.assertRaises(ValueError):
            ac.IsolationPosture(containment="contained",
                                config_authority="not_applicable", reason="   ")

    def test_ambient_config_cannot_be_called_contained(self) -> None:
        with self.assertRaises(ValueError) as caught:
            ac.IsolationPosture(containment="contained",
                                config_authority="ambient_user_config",
                                reason="tool allowlist only")
        self.assertIn("contained", str(caught.exception))

    def test_config_isolated_only_needs_an_isolable_config_home(self) -> None:
        with self.assertRaises(ValueError):
            ac.IsolationPosture(containment="config_isolated_only",
                                config_authority="ambient_user_config",
                                reason="claims isolation it cannot perform")

    def test_a_contained_backend_cannot_carry_a_trigger_optin(self) -> None:
        with self.assertRaises(ValueError) as caught:
            ac.IsolationPosture(containment="contained",
                                config_authority="not_applicable",
                                reason="offline", trigger_optin_env="ALLOW")
        self.assertIn("uncontained", str(caught.exception))

    def test_capabilities_require_an_explicit_posture(self) -> None:
        with self.assertRaises(TypeError):
            capabilities(isolation=None)


if __name__ == "__main__":
    unittest.main()
