"""Spend-ceiling boundary (spend_contracts.py): the closed ledger state machine.

Model-gap tests attempt contradictory constructions directly; persistence tests
prove a ledger parsed back from JSON must agree with its own records.
"""
import unittest
from decimal import Decimal

import telemetry as td
from spend_contracts import (
    AssumedCharge,
    ObservedCharge,
    PlannedSpendRow,
    SpendLedger,
    SpendPolicy,
    SpendPopulation,
    SpendStopReason,
    UnpricedRun,
    charge_from_dict,
)


def usd(amount: str, provenance: str = "provider_reported") -> td.Measurement:
    return td.Measurement.available(td.Money.from_raw(amount, "USD"), provenance=provenance)


def row(label: str, case_id: str = "c", variant: str = "with_skill", run_number: int = 1, model=None) -> PlannedSpendRow:
    return PlannedSpendRow.parse(label, case_id, variant, run_number, model)


class SpendPolicyTests(unittest.TestCase):
    def test_from_raw_is_exact_and_usd(self):
        policy = SpendPolicy.from_raw(0.02, 0.1)
        self.assertEqual((policy.ceiling.amount, policy.ceiling.currency), (Decimal("0.02"), "USD"))
        self.assertEqual(policy.assumed_cost_per_run.amount, Decimal("0.1"))
        self.assertEqual(policy.to_dict(), {"ceiling_usd": "0.02", "assumed_cost_per_run_usd": "0.1"})
        self.assertIsNone(SpendPolicy.from_raw(0).assumed_cost_per_run)

    def test_rejects_non_usd_negative_non_finite_and_boolean(self):
        for bad in (-1, float("nan"), float("inf"), True, "x", None):
            with self.assertRaises((TypeError, ValueError)):
                SpendPolicy.from_raw(bad)
        with self.assertRaises(ValueError):
            SpendPolicy.from_raw(1, -0.5)
        with self.assertRaises(ValueError):
            SpendPolicy(td.Money.from_raw("1", "EUR"))
        with self.assertRaises(TypeError):
            SpendPolicy(1.0)


class SpendLedgerStateTests(unittest.TestCase):
    def test_observed_charges_are_exact_and_stop_starting_at_the_ceiling(self):
        ledger = SpendLedger(SpendPolicy.from_raw(0.02), SpendPopulation.ANSWER, planned=3)
        self.assertTrue(ledger.can_start)
        ledger = ledger.charge("c0/with_skill", usd("0.0123"))
        self.assertTrue(ledger.can_start)
        ledger = ledger.charge("c1/with_skill", usd("0.0123"))
        self.assertEqual(ledger.spent.amount, Decimal("0.0246"))    # no float drift
        self.assertTrue(ledger.exhausted)
        self.assertFalse(ledger.can_start)
        self.assertIsNone(ledger.stop_reason)                        # nothing refused yet
        ledger = ledger.skip(row("c2/with_skill", case_id="c2"))
        self.assertEqual(ledger.stop_reason, SpendStopReason.COST_CEILING)
        self.assertEqual((ledger.started, len(ledger.skipped), ledger.spent_availability), (2, 1, td.COMPLETE))

    def test_zero_ceiling_refuses_the_first_run(self):
        ledger = SpendLedger(SpendPolicy.from_raw(0), "answer", planned=1)
        self.assertFalse(ledger.can_start)
        with self.assertRaises(ValueError):
            ledger.charge("a", usd("0.01"))                          # never allowed to start
        self.assertEqual(ledger.skip(row("a")).stop_reason, SpendStopReason.COST_CEILING)

    def test_unavailable_cost_is_never_charged_as_zero(self):
        ledger = SpendLedger(SpendPolicy.from_raw(1.0), SpendPopulation.ANSWER, planned=2)
        ledger = ledger.charge("a", td.Measurement.unavailable("runner_does_not_report_cost"))
        self.assertEqual(ledger.unpriced, (UnpricedRun("a", "runner_does_not_report_cost"),))
        self.assertEqual((ledger.started, ledger.spent.amount, ledger.spent_availability), (1, Decimal(0), td.PARTIAL))
        self.assertFalse(ledger.can_start)
        self.assertEqual(ledger.stop_reason, SpendStopReason.COST_UNOBSERVABLE)
        for cost in (td.Measurement.not_applicable("offline_runner"),
                     td.Measurement.available(td.Money.from_raw("1", "EUR"), provenance="provider_reported")):
            stopped = SpendLedger(SpendPolicy.from_raw(1.0), "answer", planned=1).charge("b", cost)
            self.assertEqual(stopped.stop_reason, SpendStopReason.COST_UNOBSERVABLE)
        self.assertEqual(
            SpendLedger(SpendPolicy.from_raw(1.0), "answer", planned=1).charge(
                "b", td.Measurement.available(td.Money.from_raw("1", "EUR"), provenance="provider_reported")).unpriced[0].reason,
            "non_usd_cost:EUR")

    def test_assumed_cost_prices_unavailable_runs_and_keeps_the_total_complete(self):
        ledger = SpendLedger(SpendPolicy.from_raw(1.0, 0.4), SpendPopulation.ANSWER, planned=3)
        ledger = ledger.charge("a", td.Measurement.unavailable("runner_does_not_report_cost"))
        ledger = ledger.charge("b", usd("0.25"))
        self.assertEqual(ledger.spent.amount, Decimal("0.65"))
        self.assertEqual(ledger.spent_availability, td.COMPLETE)
        self.assertEqual([c.basis.value for c in ledger.charges], ["assumed", "observed"])
        self.assertEqual(ledger.charges[0], AssumedCharge("a", td.Money.from_raw("0.4"), "runner_does_not_report_cost"))
        self.assertTrue(ledger.can_start)

    def test_cannot_skip_a_startable_run_or_charge_a_refused_one(self):
        ledger = SpendLedger(SpendPolicy.from_raw(1.0), "answer", planned=2)
        with self.assertRaises(ValueError):
            ledger.skip(row("a"))
        stopped = ledger.charge("a", td.Measurement.unavailable("x"))
        with self.assertRaises(ValueError):
            stopped.charge("b", usd("0.1"))

    def test_contradictory_constructions_are_refused(self):
        policy = SpendPolicy.from_raw(1.0)
        with self.assertRaises(ValueError):    # more records than planned
            SpendLedger(policy, "answer", planned=1, charges=(ObservedCharge("a", td.Money.from_raw("0.1"), "provider_reported"),),
                        skipped=(row("b"),))
        with self.assertRaises(ValueError):    # skipped without exhaustion or an unpriced run
            SpendLedger(policy, "answer", planned=2, skipped=(row("b"),))
        with self.assertRaises(ValueError):    # duplicate labels across records
            SpendLedger(policy, "answer", planned=3, charges=(ObservedCharge("a", td.Money.from_raw("1"), "provider_reported"),),
                        skipped=(row("a"),))
        with self.assertRaises(TypeError):
            SpendLedger(policy, "answer", planned=2, unpriced=UnpricedRun("a", "x"))
        with self.assertRaises(TypeError):
            SpendLedger(policy, "answer", planned=2, in_flight=("a", ""))
        with self.assertRaises(ValueError):
            SpendLedger(policy, "sideways", planned=1)
        for bad_planned in (-1, True, 1.5):
            with self.assertRaises(ValueError):
                SpendLedger(policy, "answer", planned=bad_planned)
        with self.assertRaises(ValueError):
            ObservedCharge("a", td.Money.from_raw("1"), "guess")
        with self.assertRaises(ValueError):
            AssumedCharge("a", td.Money.from_raw("1"), "")
        with self.assertRaises(TypeError):
            SpendLedger(policy, "answer", planned=1, charges=[])
        with self.assertRaises(ValueError):
            PlannedSpendRow.parse("", "c", "with_skill", 1)
        with self.assertRaises(ValueError):
            PlannedSpendRow.parse("a", "c", "sideways", 1)
        with self.assertRaises(ValueError):
            PlannedSpendRow.parse("a", "c", "with_skill", 0)


class SpendLedgerAdmissionTests(unittest.TestCase):
    """A concurrent loop admits before submitting and settles on completion."""

    def test_admitted_runs_count_as_started_and_settle_in_any_order(self):
        ledger = SpendLedger(SpendPolicy.from_raw(0.05), SpendPopulation.TRIGGER, planned=3)
        ledger = ledger.admit("a").admit("b")
        self.assertEqual((ledger.started, ledger.in_flight, ledger.settled), (2, ("a", "b"), False))
        self.assertTrue(ledger.can_start)                         # in-flight cost is not yet known
        with self.assertRaises(ValueError):
            ledger.to_dict()                                      # never persist unsettled runs
        ledger = ledger.settle("b", usd("0.03")).settle("a", usd("0.03"))
        self.assertEqual((ledger.settled, ledger.spent.amount, ledger.exhausted), (True, Decimal("0.06"), True))
        self.assertEqual(ledger.refusal, SpendStopReason.COST_CEILING)
        self.assertIsNone(ledger.stop_reason)                     # nothing refused yet
        ledger = ledger.skip(row("c", case_id="c3"))
        self.assertEqual(ledger.stop_reason, SpendStopReason.COST_CEILING)
        self.assertEqual(SpendLedger.from_dict(ledger.to_dict()), ledger)

    def test_admission_transitions_are_total_only_where_allowed(self):
        ledger = SpendLedger(SpendPolicy.from_raw(1.0), "trigger", planned=2)
        with self.assertRaises(ValueError):
            ledger.settle("a", usd("0.1"))                        # never admitted
        ledger = ledger.admit("a")
        with self.assertRaises(ValueError):
            ledger.admit("a")                                     # duplicate label
        with self.assertRaises(ValueError):
            ledger.skip(row("b"))                                 # still startable
        stopped = ledger.settle("a", td.Measurement.unavailable("missing"))
        with self.assertRaises(ValueError):
            stopped.admit("b")                                    # admission closed
        self.assertEqual(stopped.refusal, SpendStopReason.COST_UNOBSERVABLE)

    def test_several_in_flight_runs_can_all_turn_out_unpriced(self):
        ledger = SpendLedger(SpendPolicy.from_raw(1.0), "trigger", planned=3).admit("a").admit("b")
        ledger = ledger.settle("a", td.Measurement.unavailable("missing"))
        self.assertFalse(ledger.can_start)
        ledger = ledger.settle("b", td.Measurement.unavailable("missing"))   # was admitted before the stop
        ledger = ledger.skip(row("c"))
        self.assertEqual([run.label for run in ledger.unpriced], ["a", "b"])
        self.assertEqual((ledger.started, ledger.spent_availability, ledger.stop_reason),
                         (2, td.PARTIAL, SpendStopReason.COST_UNOBSERVABLE))
        doc = ledger.to_dict()
        self.assertEqual(doc["unpriced"], [{"label": "a", "reason": "missing"}, {"label": "b", "reason": "missing"}])
        self.assertEqual(SpendLedger.from_dict(doc), ledger)


class SpendLedgerPersistenceTests(unittest.TestCase):
    def ledger(self) -> SpendLedger:
        return (SpendLedger(SpendPolicy.from_raw(0.05, 0.02), SpendPopulation.JUDGE, planned=4)
                .charge("t1", usd("0.03"))
                .charge("t2", td.Measurement.unavailable("shell_judge"))   # assumed 0.02 -> exhausted
                .skip(row("t3", case_id="c3", model="m"))
                .skip(row("t4", case_id="c4")))

    def test_round_trips_exactly(self):
        ledger = self.ledger()
        doc = ledger.to_dict()
        self.assertEqual((doc["spent_usd"], doc["spent_availability"], doc["runs_started"], doc["runs_skipped"], doc["stop_reason"]),
                         ("0.05", "complete", 2, 2, "cost_ceiling"))
        self.assertEqual(doc["charges"][1], {"label": "t2", "basis": "assumed", "amount_usd": "0.02", "reason": "shell_judge"})
        self.assertEqual(doc["skipped"][0], {"label": "t3", "case_id": "c3", "variant": "with_skill", "run_number": 1, "model": "m"})
        self.assertEqual(SpendLedger.from_dict(doc), ledger)
        unpriced = SpendLedger(SpendPolicy.from_raw(1.0), "answer", planned=2).charge("a", td.Measurement.unavailable("x")).skip(row("b"))
        self.assertEqual(SpendLedger.from_dict(unpriced.to_dict()), unpriced)
        self.assertEqual(unpriced.to_dict()["unpriced"], [{"label": "a", "reason": "x"}])

    def test_persisted_derived_fields_must_agree_with_the_records(self):
        base = self.ledger().to_dict()
        for key, value in (("spent_usd", "0.04"), ("runs_started", 3), ("stop_reason", None),
                           ("spent_availability", "partial"), ("exhausted", False), ("runs_skipped", 1),
                           ("schema_version", 2), ("population", "sideways"), ("planned", 1)):
            doc = dict(base)
            doc[key] = value
            with self.assertRaises(ValueError, msg=key):
                SpendLedger.from_dict(doc)
        doc = dict(base)
        doc["extra"] = 1
        with self.assertRaises(ValueError):
            SpendLedger.from_dict(doc)
        doc = dict(base)
        del doc["charges"]
        with self.assertRaises(ValueError):
            SpendLedger.from_dict(doc)
        with self.assertRaises(ValueError):
            SpendLedger.from_dict([])
        with self.assertRaises(ValueError):
            charge_from_dict({"label": "a", "basis": "observed", "amount_usd": "1", "reason": "x"})
        with self.assertRaises(ValueError):
            charge_from_dict({"label": "a", "basis": "guess", "amount_usd": "1"})


if __name__ == "__main__":
    unittest.main()
