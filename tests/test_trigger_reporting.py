"""Aggregate-level proofs that unknown trigger evidence never becomes false."""
import unittest

from trigger_contracts import (
    InvocationOutcome,
    InvocationState,
    TriggerDetection,
    TriggerExpectation,
    TriggerObservation,
)
from trigger_reporting import (
    CompleteTriggerCohort,
    EmptyTriggerCohort,
    IncompleteTriggerCohort,
    summarize_trigger_cohort,
    summarize_trigger_matrix,
    trigger_cohort_as_dict,
    trigger_cohort_exit_code,
)


def observation(*, complete: bool, should_trigger: bool = True,
                triggered: bool = True) -> TriggerObservation:
    invocation = InvocationOutcome.from_process(
        stdout="", stderr="", returncode=0 if complete else 1, elapsed_ms=1,
    )
    detection = TriggerDetection.absent()
    if triggered:
        from trigger_contracts import TriggerEvidenceKind
        detection = TriggerDetection.from_texts(TriggerEvidenceKind.MOUNTED_PATH, ["skill/SKILL.md"])
    return TriggerObservation(
        agent="stub",
        model=None,
        query="q",
        expectation=TriggerExpectation.from_bool(should_trigger),
        invocation=invocation,
        detection=detection,
        usage={"source": "missing"},
        cost={"source": "missing"},
    )


class TriggerCohortTypeTests(unittest.TestCase):
    def test_complete_cohort_is_the_only_variant_with_rates(self):
        cohort = summarize_trigger_cohort([
            observation(complete=True, triggered=True),
            observation(complete=True, triggered=False),
        ])
        self.assertIsInstance(cohort, CompleteTriggerCohort)
        block = trigger_cohort_as_dict(cohort)
        self.assertEqual(block["pass_rate"], 0.5)
        self.assertEqual(block["trigger_rate"], 0.5)
        self.assertEqual(trigger_cohort_exit_code(cohort), 0)

    def test_incomplete_cohort_has_diagnostics_but_no_quality_rate(self):
        cohort = summarize_trigger_cohort([
            observation(complete=True),
            observation(complete=False),
        ])
        self.assertIsInstance(cohort, IncompleteTriggerCohort)
        block = trigger_cohort_as_dict(cohort)
        self.assertEqual(block["measurement_status"], "incomplete")
        self.assertEqual((block["observed"], block["total"]), (1, 2))
        self.assertEqual(block["incomplete_reasons"], {InvocationState.PROCESS_FAILED.value: 1})
        self.assertNotIn("pass_rate", block)
        self.assertNotIn("trigger_rate", block)
        self.assertEqual(trigger_cohort_exit_code(cohort), 1)

    def test_empty_cohort_is_not_a_zero_rate(self):
        cohort = summarize_trigger_cohort([])
        self.assertIsInstance(cohort, EmptyTriggerCohort)
        block = trigger_cohort_as_dict(cohort)
        self.assertEqual(block["measurement_status"], "empty")
        self.assertNotIn("pass_rate", block)
        self.assertEqual(trigger_cohort_exit_code(cohort), 1)

    def test_invalid_aggregate_states_cannot_be_constructed(self):
        with self.assertRaises(ValueError):
            CompleteTriggerCohort(total=0, passed=0, triggered=0)
        with self.assertRaises(ValueError):
            IncompleteTriggerCohort(total=2, observed=2, passed=2, triggered=2,
                                    reasons=(("timed_out", 0),))

    def test_matrix_queries_and_polarities_share_the_same_coverage_rule(self):
        cells = summarize_trigger_matrix([
            observation(complete=True),
            observation(complete=False),
        ])
        cell = cells[0]
        self.assertEqual(cell["summary"]["measurement_status"], "incomplete")
        self.assertEqual(cell["summary"]["should_trigger"]["measurement_status"], "incomplete")
        self.assertEqual(cell["queries"][0]["measurement_status"], "incomplete")
        self.assertNotIn("pass_rate", cell["queries"][0])


if __name__ == "__main__":
    unittest.main()
