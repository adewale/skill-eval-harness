"""One vocabulary for "did we observe it", one list of telemetry sources."""
import ast
import subprocess
import typing
import unittest
from pathlib import Path

import agent_capabilities as caps
import judge_contracts as jc
import observation_contracts as oc
import runner_contracts as rc
import skill_benchmark as sb
import telemetry
from invocation_contracts import InvocationState
from trigger_contracts import (
    InvocationOutcome,
    TriggerDetection,
    TriggerExpectation,
    TriggerObservation,
)


class AvailabilityTests(unittest.TestCase):
    def test_every_legacy_spelling_reads_as_one_canonical_state(self):
        cases = {
            "complete": oc.Availability.COMPLETE,
            "partial": oc.Availability.PARTIAL,
            "incomplete": oc.Availability.PARTIAL,
            "unavailable": oc.Availability.UNAVAILABLE,
            "unknown": oc.Availability.UNAVAILABLE,
            "unobserved": oc.Availability.UNAVAILABLE,
            "missing": oc.Availability.UNAVAILABLE,
            "not_applicable": oc.Availability.NOT_APPLICABLE,
            "not-applicable": oc.Availability.NOT_APPLICABLE,
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertIs(oc.Availability.parse(raw), expected)

    def test_an_unknown_spelling_is_an_error(self):
        for raw in ("done", "", None, 1):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                oc.Availability.parse(raw)

    def test_only_complete_counts_as_observed(self):
        self.assertEqual([item for item in oc.Availability if item.observed],
                         [oc.Availability.COMPLETE])


class RetiredSpellingTests(unittest.TestCase):
    """The spellings Availability replaced do not come back in new code.

    observation_contracts keeps them only to read old artifacts. Two Gemini
    sandbox fields persisted before this vocabulary are listed explicitly."""

    RETIRED = {"unobserved", "not-requested", "backend-default", "not-applicable"}
    PERSISTED = {("skill_benchmark.py", "not-applicable"): 2}

    def test_no_production_module_writes_a_retired_spelling(self):
        root = Path(__file__).resolve().parents[1]
        files = subprocess.check_output(
            ["git", "ls-files", "*.py", "scripts/*.py"], cwd=root, text=True).split()
        found: dict[tuple[str, str], int] = {}
        for name in files:
            if name.startswith(("tests/", "type_tests/")) or name == "observation_contracts.py":
                continue
            tree = ast.parse((root / name).read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Constant) and node.value in self.RETIRED:
                    key = (name, str(node.value))
                    found[key] = found.get(key, 0) + 1
        self.assertEqual(found, self.PERSISTED)


class TelemetrySourceTests(unittest.TestCase):
    def test_cost_never_accepts_a_bare_estimate(self):
        # A cost estimate names its price table; the trigger path once
        # accepted "estimated" while the answer path rejected it.
        self.assertNotIn("estimated", oc.COST_SOURCES)
        self.assertIn("price_table_estimated", oc.COST_SOURCES)
        self.assertNotIn("price_table_estimated", oc.USAGE_SOURCES)

    def test_declared_capability_literals_stay_within_the_source_list(self):
        # The capability registry spells its sources as Literal types for ty;
        # they must name sources the harness can actually record.
        known = {source.value for source in oc.TelemetrySource}
        self.assertLessEqual(set(typing.get_args(caps.CostSupport)), known)
        self.assertLessEqual(set(typing.get_args(caps.TelemetryProvenance)),
                             set(oc.MEASUREMENT_PROVENANCE))


class ProducersAndValidatorsAgreeTests(unittest.TestCase):
    """Every block the answer path's normalizers emit, the trigger path accepts.

    The two paths kept separate source lists and drifted: the trigger path
    accepted a bare "estimated" cost and rejected the partial-component
    missing block that normalize_cost emits."""

    def observation(self, *, usage, cost):
        return TriggerObservation(
            agent="pi", model=None, query="q",
            expectation=TriggerExpectation.DO_NOT_TRIGGER,
            invocation=InvocationOutcome.from_process(stdout="", stderr="", returncode=0, elapsed_ms=0),
            detection=TriggerDetection.absent(), usage=usage, cost=cost)

    def test_every_normalized_cost_block_is_a_valid_trigger_cost(self):
        blocks = [
            sb.normalize_cost(0.25),
            sb.normalize_cost({"total_cost": 1.5, "currency": "EUR"}, source="trace_normalized"),
            sb.normalize_cost(None),
            sb.normalize_cost({"input_cost": 0.1}),  # parts without a total stay missing
            sb.normalize_cost(None, source="not_applicable"),
            sb.normalize_cost(0.5, source="price_table_estimated", pricing_table_version="t1"),
        ]
        for block in blocks:
            with self.subTest(block=block):
                self.observation(usage={"source": "missing"}, cost=block)

    def test_every_normalized_usage_block_is_a_valid_trigger_usage(self):
        for source in sorted(oc.USAGE_SOURCES - oc.ABSENT_SOURCES):
            with self.subTest(source=source):
                self.observation(usage=sb.normalize_usage({"input_tokens": 3, "output_tokens": 4},
                                                          source=source),
                                 cost={"source": "missing"})

    def test_a_bare_estimated_cost_is_rejected_by_both_paths(self):
        with self.assertRaises(ValueError):
            sb.normalize_cost(0.5, source="estimated")
        with self.assertRaises(ValueError):
            self.observation(usage={"source": "missing"},
                             cost={"source": "estimated", "total_cost": 0.5, "currency": "USD"})

    def test_the_telemetry_domain_reads_the_same_provenance_list(self):
        self.assertIs(telemetry.PROVENANCE, oc.MEASUREMENT_PROVENANCE)


class RawUsageBoundariesAgreeTests(unittest.TestCase):
    """The runner and the judge accept exactly the same raw usage.

    Each kept its own validator: only the judge rejected non-string keys, and
    only the runner rejected an amount too large to be a float."""

    def accepted_by(self, usage):
        verdicts = []
        for build in (
                lambda: rc.RunnerOutcome(provider="gemini", answer="ok", returncode=0, usage=usage),
                lambda: jc.JudgeInvocation(stdout="{}", stderr="", returncode=0,
                                           invocation_state=InvocationState.COMPLETE, usage=usage)):
            try:
                build()
            except (TypeError, ValueError):
                verdicts.append(False)
            else:
                verdicts.append(True)
        return verdicts

    def test_both_boundaries_give_the_same_answer(self):
        cases = [
            ({"input_tokens": 10 ** 400}, True),         # exact integer counts at any size
            ({"input_tokens": 1.5}, False),              # a token count is an integer
            ({"cost": 10 ** 400}, False),                # an amount must fit a float
            ({"nested": {"output_tokens": -1}}, False),
            ({1: 2}, False),                             # keys are strings
            ({"input_tokens": True}, False),
        ]
        for usage, expected in cases:
            with self.subTest(usage=usage):
                self.assertEqual(self.accepted_by(usage), [expected, expected])


if __name__ == "__main__":
    unittest.main()
