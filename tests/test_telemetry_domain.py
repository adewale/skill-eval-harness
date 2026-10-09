"""Telemetry availability/comparability domain contract.

These are deliberately boundary- and behavior-focused: they prove a measured zero,
an unavailable observation, and a non-comparable pair remain different states.
"""
from __future__ import annotations

import unittest
from decimal import Decimal

from telemetry import (
    EVIDENCE_COMPLETE,
    EVIDENCE_INCOMPLETE,
    EVIDENCE_UNKNOWN,
    Aggregate,
    Comparison,
    Measurement,
    Money,
    ObservationEvidence,
    aggregate_money_by_currency,
    aggregate_numeric,
    compare_cost_pair,
    lift_per_dollar,
    measurement_from_envelope_or_nonnegative,
)


class ObservationEvidenceTests(unittest.TestCase):
    def test_full_state_product_never_promotes_an_independent_axis(self):
        states = (EVIDENCE_COMPLETE, EVIDENCE_INCOMPLETE, EVIDENCE_UNKNOWN)
        for process in states:
            for provider in states:
                for trace in states:
                    for artifacts in states:
                        with self.subTest(process=process, provider=provider,
                                          trace=trace, artifacts=artifacts):
                            evidence = ObservationEvidence(process, provider, trace, artifacts)
                            self.assertEqual(
                                evidence.operation_complete,
                                process == provider == trace == EVIDENCE_COMPLETE,
                            )
                            self.assertEqual(
                                evidence.artifact_complete,
                                artifacts == EVIDENCE_COMPLETE,
                            )
                            self.assertEqual(
                                ObservationEvidence.from_dict(evidence.to_dict()), evidence)

    def test_wire_cannot_override_derived_operation_completeness(self):
        wire = ObservationEvidence(
            EVIDENCE_COMPLETE, EVIDENCE_INCOMPLETE,
            EVIDENCE_COMPLETE, EVIDENCE_COMPLETE).to_dict()
        wire["operation_evidence_complete"] = True
        with self.assertRaisesRegex(ValueError, "contradicts"):
            ObservationEvidence.from_dict(wire)

    def test_legacy_generic_completion_cannot_certify_process_or_trace(self):
        evidence = ObservationEvidence.from_run({"observation_complete": True})
        self.assertEqual(evidence.provider_response, EVIDENCE_COMPLETE)
        self.assertEqual(evidence.process, EVIDENCE_UNKNOWN)
        self.assertEqual(evidence.trace, EVIDENCE_UNKNOWN)
        self.assertFalse(evidence.operation_complete)

    def test_available_operation_scalar_cannot_override_incomplete_evidence(self):
        evidence = ObservationEvidence(
            EVIDENCE_COMPLETE, EVIDENCE_INCOMPLETE,
            EVIDENCE_COMPLETE, EVIDENCE_COMPLETE)
        raw = {"telemetry": {
            "schema_version": 3,
            "observation_evidence": evidence.to_dict(),
            "measurements": {"commands": {
                "availability": "available", "value": 0,
                "provenance": "trace_normalized",
            }},
        }}
        measurement = measurement_from_envelope_or_nonnegative(raw, "commands")
        self.assertEqual(measurement.availability, "unavailable")
        self.assertEqual(measurement.reason, "provider_response_incomplete")


class MeasurementModelTests(unittest.TestCase):
    def test_available_zero_is_not_unavailable(self):
        measurement = Measurement.available(0, provenance="provider_reported")
        self.assertEqual(measurement.availability, "available")
        self.assertEqual(measurement.value, 0)
        self.assertEqual(measurement.to_dict()["value"], 0)

    def test_model_rejects_invalid_states(self):
        with self.assertRaises(ValueError):
            Measurement("available", None, provenance="provider_reported")
        with self.assertRaises(ValueError):
            Measurement("unavailable", 0, reason="missing")
        with self.assertRaises(ValueError):
            Measurement.available(-1, provenance="provider_reported")
        with self.assertRaises(ValueError):
            Money(Decimal("-0.01"), "USD")
        with self.assertRaises(ValueError):
            Money(Decimal("0.01"), "usd")
        with self.assertRaises(ValueError):
            Aggregate("complete", value=float("nan"))
        with self.assertRaises(ValueError):
            Comparison.comparable(float("nan"))

    def test_evidence_mappings_are_strict_and_recursively_immutable(self):
        basis = {"nested": {"labels": ["original"]}}
        measurement = Measurement.available(
            1, provenance="provider_reported", basis=basis)
        comparison = Comparison.comparable(1.0, basis=basis)
        basis["nested"]["labels"].append("mutated")
        expected = {"nested": {"labels": ("original",)}}
        self.assertEqual(measurement.basis, expected)
        self.assertEqual(comparison.basis, expected)

        reasons = {"missing": 1}
        aggregate = Aggregate(
            "unavailable", unavailable_count=1, reason_counts=reasons)
        reasons["missing"] = 2
        self.assertEqual(aggregate.reason_counts, {"missing": 1})

        for basis_value in ([], {1: "bad key"}, {"score": float("nan")}):
            with self.subTest(measurement_basis=basis_value), \
                 self.assertRaises((TypeError, ValueError)):
                Measurement.available(
                    1, provenance="provider_reported", basis=basis_value)
            with self.subTest(comparison_basis=basis_value), \
                 self.assertRaises((TypeError, ValueError)):
                Comparison.comparable(1.0, basis=basis_value)

        for kwargs in (
            {"observed_count": True},
            {"reason_counts": {1: 1}},
            {"reason_counts": {"missing": True}},
        ):
            with self.subTest(aggregate=kwargs), self.assertRaises((TypeError, ValueError)):
                Aggregate("complete", value=1, **kwargs)


class AggregateTests(unittest.TestCase):
    def test_zero_complete_partial_and_unavailable_are_distinct(self):
        zero = aggregate_numeric([Measurement.available(0, provenance="provider_reported")])
        partial = aggregate_numeric([
            Measurement.available(0, provenance="provider_reported"),
            Measurement.unavailable("runner_does_not_report_cost"),
        ])
        absent = aggregate_numeric([Measurement.unavailable("trace_absent")])

        self.assertEqual(zero.availability, "complete")
        self.assertEqual(zero.value, 0)
        self.assertEqual(partial.availability, "partial")
        self.assertEqual(partial.known_subtotal, 0)
        self.assertEqual(absent.availability, "unavailable")
        self.assertIsNone(absent.value)

    def test_incompatible_bases_cannot_claim_one_complete_total(self):
        aggregate = aggregate_numeric([
            Measurement.available(3, provenance="provider_reported", basis={"population": "answer", "billing_scope": "run"}),
            Measurement.available(7, provenance="trace_normalized", basis={"population": "answer", "billing_scope": "run"}),
        ])
        self.assertEqual(aggregate.availability, "unavailable")
        self.assertEqual(aggregate.reason_counts, {"basis_mismatch": 2})

    def test_currency_buckets_do_not_turn_each_other_into_missing_values(self):
        basis = {"population": "answer", "billing_scope": "run"}
        buckets = aggregate_money_by_currency([
            Measurement.available(Money(Decimal("1.00"), "USD"), provenance="provider_reported", basis=basis),
            Measurement.available(Money(Decimal("2.00"), "EUR"), provenance="provider_reported", basis=basis),
        ])
        self.assertEqual(buckets["USD"].availability, "complete")
        self.assertEqual(buckets["USD"].value, Decimal("1.00"))
        self.assertEqual(buckets["EUR"].availability, "complete")
        self.assertEqual(buckets["EUR"].value, Decimal("2.00"))

    def test_unavailable_rows_do_not_change_known_subtotal(self):
        complete = aggregate_numeric([
            Measurement.available(3, provenance="provider_reported"),
            Measurement.available(7, provenance="provider_reported"),
        ])
        partial = aggregate_numeric([
            Measurement.available(3, provenance="provider_reported"),
            Measurement.available(7, provenance="provider_reported"),
            Measurement.unavailable("trace_absent"),
        ])
        self.assertEqual(complete.value, 10)
        self.assertEqual(partial.known_subtotal, 10)
        self.assertEqual(partial.unavailable_count, 1)


class CostComparisonTests(unittest.TestCase):
    def test_lift_per_dollar_requires_complete_comparable_basis(self):
        basis = {
            "population": "answer", "provider": "anthropic", "model": "claude-test",
            "billing_scope": "run", "case_id": "case-1", "run_number": 1,
            "pricing_model": "claude-test", "pricing_table_version": "2026-07",
        }
        with_cost = Measurement.available(Money(Decimal("0.30"), "USD"), provenance="provider_reported", basis=basis)
        without_cost = Measurement.available(Money(Decimal("0.10"), "USD"), provenance="provider_reported", basis=basis)
        delta = compare_cost_pair(with_cost, without_cost)
        ratio = lift_per_dollar(Comparison.comparable(1.0), delta)

        self.assertEqual(delta.availability, "comparable")
        self.assertEqual(delta.value.amount, Decimal("0.20"))
        self.assertEqual(ratio.availability, "comparable")
        self.assertEqual(ratio.value, 5.0)

    def test_lift_per_dollar_blocks_missing_mismatched_and_nonpositive_cost(self):
        basis = {
            "population": "answer", "provider": "anthropic", "model": "claude-test",
            "billing_scope": "run", "case_id": "case-1", "run_number": 1,
            "pricing_model": "claude-test", "pricing_table_version": "2026-07",
        }
        with_cost = Measurement.available(Money(Decimal("0.30"), "USD"), provenance="provider_reported", basis=basis)
        missing = Measurement.unavailable("runner_does_not_report_cost")
        other_currency = Measurement.available(Money(Decimal("0.10"), "EUR"), provenance="provider_reported", basis=basis)
        same_cost = Measurement.available(Money(Decimal("0.30"), "USD"), provenance="provider_reported", basis=basis)

        self.assertEqual(compare_cost_pair(with_cost, missing).reason, "missing_right")
        self.assertEqual(compare_cost_pair(with_cost, other_currency).reason, "currency_mismatch")
        self.assertEqual(lift_per_dollar(Comparison.comparable(1.0), compare_cost_pair(with_cost, same_cost)).reason,
                         "non_positive_denominator")
        saving = Measurement.available(Money(Decimal("0.40"), "USD"), provenance="provider_reported", basis=basis)
        saving_delta = compare_cost_pair(with_cost, saving)
        self.assertEqual(saving_delta.value.amount, Decimal("-0.10"))
        self.assertEqual(lift_per_dollar(Comparison.comparable(1.0), saving_delta).reason, "non_positive_denominator")
        other_case = Measurement.available(
            Money(Decimal("0.10"), "USD"), provenance="provider_reported",
            basis={**basis, "case_id": "other-case"},
        )
        self.assertEqual(compare_cost_pair(with_cost, other_case).reason, "pair_key_mismatch")
        self.assertEqual(compare_cost_pair(with_cost, same_cost, left_scorable=False).reason, "unscorable_arm")


if __name__ == "__main__":
    unittest.main()
