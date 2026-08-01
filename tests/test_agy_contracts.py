"""Protocol laws for the Antigravity (`agy`) wire contract.

These tests encode the three defects PLAN.md names D1, D2 and D3.  They were
written before the parser existed and are expected to fail against the step-2
stub; each failure message names the invariant it protects rather than the
mechanism that happened to break.
"""
from __future__ import annotations

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


if __name__ == "__main__":
    unittest.main()
