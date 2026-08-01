import unittest

import report_contracts as rc
import skill_benchmark as sb


class ReportCohortTests(unittest.TestCase):
    @staticmethod
    def cohort(rows, observed_ids):
        return rc.report_cohort(
            rows,
            identity=lambda row: row["id"],
            eligibility=lambda row: (
                (True, None) if row["id"] in observed_ids
                else (False, "blocked")
            ),
        )

    def test_empty_complete_and_partial_are_distinct(self):
        first = {"id": "first", "objective_pass_rate": 1.0}
        second = {"id": "second", "objective_pass_rate": 0.0}

        empty = self.cohort([], set())
        complete = self.cohort([first, second], {"first", "second"})
        partial = self.cohort([first, second], {"first"})

        self.assertIsInstance(empty, rc.EmptyReportCohort)
        self.assertIsInstance(complete, rc.CompleteReportCohort)
        self.assertIsInstance(partial, rc.PartialReportCohort)
        self.assertEqual(rc.observed_rates(complete, "objective_pass_rate"), (1.0, 0.0))
        self.assertIsNone(rc.headline_value(partial, 1.0))
        self.assertEqual(rc.headline_value(complete, 0.5), 0.5)

    def test_attempt_identity_is_stable_and_unique(self):
        with self.assertRaises(ValueError):
            rc.report_cohort(
                [{"id": "same"}, {"id": "same"}],
                identity=lambda row: row["id"],
                eligibility=lambda row: (True, None),
            )

    def test_invalid_rate_never_reaches_a_report_headline(self):
        row = {"objective_pass_rate": 1.1}
        cohort = rc.report_cohort(
            [row], identity=lambda unused: "row",
            eligibility=lambda unused: (True, None))
        with self.assertRaises(ValueError):
            rc.observed_rates(cohort, "objective_pass_rate")

    def test_coverage_fields_keep_observed_and_attempted_counts_together(self):
        observed = {"id": "observed", "objective_pass_rate": 1.0}
        blocked = {"id": "blocked", "objective_pass_rate": None}
        cohort = rc.report_cohort(
            [observed, blocked],
            identity=lambda row: row["id"],
            eligibility=lambda row: (
                (True, None) if row["id"] == "observed"
                else (False, "missing evidence")),
        )
        self.assertEqual(rc.coverage_fields(cohort), {
            "attempted_runs": 2,
            "runs": 1,
            "blocked_runs": 1,
            "availability": "partial",
            "reason": "missing evidence",
            "blocked_reason_counts": {"missing evidence": 1},
        })

    def test_metric_missing_from_complete_rows_is_partial_not_a_survivor_headline(self):
        rows = [
            {"id": "first", "objective_pass_rate": 1.0},
            {"id": "second", "objective_pass_rate": None},
        ]
        row_cohort = self.cohort(rows, {"first", "second"})
        metric = rc.metric_cohort(row_cohort, "objective_pass_rate")

        self.assertIsInstance(metric, rc.PartialReportCohort)
        self.assertEqual(rc.observed_rates(metric, "objective_pass_rate"), (1.0,))
        self.assertIsNone(rc.headline_value(metric, 1.0))
        self.assertEqual(rc.diagnostic_rates(metric, "objective_pass_rate"), (1.0,))

    def test_not_applicable_metric_is_distinct_from_empty_and_unavailable(self):
        cohort = self.cohort([{"id": "one", "process_pass_rate": None}], {"one"})
        metric = rc.metric_cohort(
            cohort, "process_pass_rate", applicability=lambda row: False)

        self.assertIsInstance(metric, rc.NotApplicableReportCohort)
        self.assertEqual(rc.coverage_fields(metric), {
            "attempted_runs": 1,
            "runs": 0,
            "blocked_runs": 0,
            "availability": "not_applicable",
            "not_applicable_runs": 1,
        })

    def test_metric_refinement_cannot_resurrect_base_not_applicable_attempt(self):
        base = rc.report_cohort(
            [{"id": "one", "process_pass_rate": 1.0}],
            identity=lambda row: row["id"],
            eligibility=lambda row: (None, "outside_population"),
        )
        callback_called = False

        def applicable(row):
            nonlocal callback_called
            callback_called = True
            return True

        metric = rc.metric_cohort(
            base, "process_pass_rate", applicability=applicable)

        self.assertIsInstance(metric, rc.NotApplicableReportCohort)
        self.assertFalse(callback_called)

    def test_empty_variant_summary_is_named_empty(self):
        summary = sb.variant_summary_block([])
        self.assertEqual(summary["availability"], "empty")
        self.assertEqual(summary["runs"], 0)
        self.assertEqual(summary["scorable_runs"], 0)

    @staticmethod
    def result_row(run_number=1, *, grading="complete", elapsed_ms=10):
        return {
            "case_id": "case-1",
            "variant": "with_skill",
            "run_number": run_number,
            "missing_output": False,
            "execution_valid": True,
            "grading_availability": grading,
            "objective_total": 1,
            "objective_pass_rate": 1.0,
            "combined_total": 1,
            "combined_pass_rate": 1.0,
            "process_total": 0,
            "process_pass_rate": None,
            "efficiency_total": 0,
            "efficiency_pass_rate": None,
            "metadata": {"elapsed_ms": elapsed_ms},
            "run_base": "/definitely/not/a/run",
        }

    def test_zero_denominator_metrics_are_not_applicable_in_production_summary(self):
        summary = sb.variant_summary_block([self.result_row()])

        self.assertEqual(summary["availability"], "complete")
        self.assertEqual(summary["metric_availability"], {
            "objective_pass_rate": "complete",
            "combined_pass_rate": "complete",
            "process_pass_rate": "not_applicable",
            "efficiency_pass_rate": "not_applicable",
        })
        self.assertIsNone(summary["mean_process_pass_rate"])
        self.assertNotIn("observed_mean_process_pass_rate", summary)

    def test_grading_incompleteness_does_not_discard_execution_telemetry(self):
        rows = [
            self.result_row(1, elapsed_ms=10),
            self.result_row(2, grading="partial", elapsed_ms=100),
        ]
        summary = sb.variant_summary_block(rows)

        self.assertEqual(summary["availability"], "partial")
        self.assertEqual(summary["scorable_runs"], 2)
        self.assertEqual(summary["elapsed_ms"]["n"], 2)
        self.assertEqual(summary["elapsed_ms"]["mean"], 55)

    def test_report_attempt_identity_rejects_boolean_run_number(self):
        row = self.result_row()
        row["run_number"] = True
        with self.assertRaises(ValueError):
            sb.variant_summary_block([row])

    def test_metric_totals_reject_coercion_and_zero_rate_contradictions(self):
        for total in (False, 0.0, -1, "0"):
            row = self.result_row()
            row["process_total"] = total
            with self.subTest(total=total), self.assertRaises(ValueError):
                sb.variant_summary_block([row])

        row = self.result_row()
        row["process_pass_rate"] = 0.0
        with self.assertRaisesRegex(ValueError, "contradicts"):
            sb.variant_summary_block([row])


if __name__ == "__main__":
    unittest.main()
