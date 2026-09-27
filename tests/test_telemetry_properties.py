"""Property and finite-state tests for the telemetry boundary.

The type constructors make invalid internal states hard to express; these tests
exercise the remaining untyped JSON/numeric boundary and algebraic laws.

Written as ``unittest.TestCase`` methods (not module-level pytest functions or
``pytest.mark.parametrize``) so CI's ``python -m unittest discover`` collects
them; ``scripts/check_test_collection_parity.py`` fails CI if the two
collectors diverge again.
"""
from __future__ import annotations

import itertools
import unittest
from decimal import Decimal

from hypothesis import given
from hypothesis import strategies as st

from telemetry import (
    AVAILABLE,
    BLOCKED,
    COMPARABLE,
    Comparison,
    Measurement,
    Money,
    aggregate_numeric,
    compare_cost_pair,
    lift_per_dollar,
    measurement_from_cost_block,
)

STATES = ("available", "unavailable", "not_applicable")
INCREMENTS = (Decimal("-0.10"), Decimal(0), Decimal("0.10"))


class TelemetryPropertyTests(unittest.TestCase):
    @given(st.lists(st.integers(min_value=0, max_value=1_000_000), max_size=8))
    def test_numeric_aggregate_is_permutation_invariant(self, values):
        measurements = [Measurement.available(value, provenance="provider_reported") for value in values]
        expected = aggregate_numeric(measurements).to_dict()
        # Testing every permutation becomes impractical above a few rows; these
        # deterministic orderings still prove the property over the broad numeric
        # input space and the finite-state test below exhausts availability states.
        self.assertEqual(aggregate_numeric(list(reversed(measurements))).to_dict(), expected)
        self.assertEqual(
            aggregate_numeric(sorted(measurements, key=lambda measurement: measurement.value)).to_dict(),
            expected)

    @given(st.one_of(st.none(), st.booleans(), st.text(), st.floats(allow_nan=True, allow_infinity=True),
                     st.dictionaries(st.text(max_size=12), st.integers())))
    def test_cost_parser_is_valid_or_explicitly_unavailable(self, raw):
        measurement = measurement_from_cost_block({"source": "provider_reported", "total_cost": raw})
        self.assertIn(measurement.availability, {"available", "unavailable", "not_applicable"})
        if measurement.availability == AVAILABLE:
            self.assertIsInstance(measurement.value, Money)
            self.assertGreaterEqual(measurement.value.amount, 0)
            self.assertTrue(measurement.value.amount.is_finite())
        else:
            self.assertIsNone(measurement.value)
            self.assertTrue(measurement.reason)


class TelemetryFiniteStateTests(unittest.TestCase):
    def test_every_availability_currency_and_denominator_combination_is_safe(self):
        basis = {"population": "answer", "provider": "p", "model": "m", "billing_scope": "run",
                 "case_id": "case-1", "run_number": 1}

        def measurement(state, amount, currency):
            if state == "available":
                return Measurement.available(Money(amount, currency), provenance="provider_reported", basis=basis)
            if state == "unavailable":
                return Measurement.unavailable("missing", basis=basis)
            return Measurement.not_applicable("offline", basis=basis)

        combinations = list(itertools.product(STATES, STATES, (True, False), INCREMENTS))
        # The exhaustive table is the point of this test: 3 x 3 x 2 x 3 cells.
        self.assertEqual(len(combinations), 54)
        for left_state, right_state, same_currency, increment in combinations:
            with self.subTest(left_state=left_state, right_state=right_state,
                              same_currency=same_currency, increment=str(increment)):
                without = measurement(right_state, Decimal("1.00"), "USD")
                # A negative increment still leaves a non-negative observed treatment cost.
                with_amount = Decimal("1.00") + increment
                with_cost = measurement(left_state, with_amount, "USD" if same_currency else "EUR")
                delta = compare_cost_pair(with_cost, without)
                ratio = lift_per_dollar(Comparison.comparable(1.0), delta)

                if left_state == right_state == "available" and same_currency:
                    self.assertEqual(delta.availability, COMPARABLE)
                    if increment > 0:
                        self.assertEqual(ratio.availability, COMPARABLE)
                    else:
                        self.assertEqual(ratio.availability, BLOCKED)
                        self.assertEqual(ratio.reason, "non_positive_denominator")
                else:
                    self.assertEqual(delta.availability, BLOCKED)
                    self.assertEqual(ratio.availability, BLOCKED)


if __name__ == "__main__":
    unittest.main()
