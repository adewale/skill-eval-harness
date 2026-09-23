"""Eval-quality review boundary (review_contracts.py): verifier suspicions and
model-order inversions. Model-gap tests attempt contradictory constructions;
the Fisher test is checked against hand-computed hypergeometric tails."""
import unittest
from fractions import Fraction
from math import comb

import review_contracts as rc
from manifest_contracts import ModelId

OBJ, QUAL = rc.AssertionRole.OBJECTIVE, rc.AssertionRole.QUALITATIVE


def run(case="c", variant="with_skill", n=1, model=None):
    return rc.RunRef.parse(case, variant, n, model)


def outcome(r, name, passed, *, role=OBJ, gate=True, near=False):
    return rc.AssertionOutcome(r, name, role, gate, passed, near)


class VerifierSuspicionTests(unittest.TestCase):
    def signals(self, outcomes, **kw):
        return [(s.assertion, s.signal.value) for s in rc.verifier_suspicions(outcomes, **kw)]

    def test_never_passes_needs_every_observation_to_fail_and_a_minimum_count(self):
        runs = [run(variant=v, n=n) for v in ("with_skill", "without_skill") for n in (1, 2)]
        self.assertEqual(self.signals([outcome(r, "a", False) for r in runs]), [("a", "never_passes")])
        self.assertEqual(self.signals([outcome(r, "a", i == 0) for i, r in enumerate(runs)]), [])
        self.assertEqual(self.signals([outcome(runs[0], "a", False)]), [])              # one run is not a pattern
        self.assertEqual(self.signals([outcome(runs[0], "a", False)], min_observations=1), [("a", "never_passes")])
        suspicion = rc.verifier_suspicions([outcome(r, "a", False) for r in runs])[0]
        self.assertEqual(len(suspicion.runs), 4)
        self.assertIn("with_skill", suspicion.detail)

    def test_qualitative_verdicts_never_count_as_never_passing_objective_checks(self):
        runs = [run(n=1), run(n=2)]
        self.assertEqual(self.signals([outcome(r, "j", False, role=QUAL, gate=False) for r in runs]), [])

    def test_format_near_miss_is_reported_per_assertion(self):
        r1, r2 = run(n=1), run(n=2)
        found = rc.verifier_suspicions([outcome(r1, "label", False, near=True), outcome(r2, "label", True)])
        self.assertEqual([(s.signal.value, s.runs) for s in found], [("format_near_miss", (r1,))])

    def test_oracle_disagreement_needs_a_failed_gate_and_a_passing_judge_on_the_same_run(self):
        r1, r2 = run(n=1), run(n=2)
        rows = [outcome(r1, "order", False), outcome(r1, "judge", True, role=QUAL, gate=False),
                outcome(r2, "order", True), outcome(r2, "judge", True, role=QUAL, gate=False)]
        found = rc.verifier_suspicions(rows)
        self.assertEqual([(s.assertion, s.signal.value, s.runs) for s in found], [("order", "oracle_disagreement", (r1,))])
        self.assertIn("judge", found[0].detail)
        soft = [outcome(r1, "order", False, gate=False), outcome(r1, "judge", True, role=QUAL, gate=False)]
        self.assertEqual(self.signals(soft), [])                                         # a soft check is not a gate
        failing_judge = [outcome(r1, "order", False), outcome(r1, "judge", False, role=QUAL)]
        self.assertEqual(self.signals(failing_judge), [])

    def test_contradictory_values_are_refused(self):
        r = run()
        with self.assertRaises(ValueError):
            outcome(r, "a", True, near=True)                                             # a pass cannot be a near miss
        with self.assertRaises(ValueError):
            outcome(r, "j", False, role=QUAL, near=True)                                 # judges are not text checks
        with self.assertRaises(TypeError):
            rc.AssertionOutcome(r, "a", OBJ, 1, False)
        with self.assertRaises(ValueError):
            rc.AssertionOutcome(r, "", OBJ, True, False)
        with self.assertRaises(ValueError):
            rc.VerifierSuspicion(r.case_id, "a", rc.VerifierSignal.NEVER_PASSES, (), "x")
        with self.assertRaises(ValueError):
            rc.VerifierSuspicion(r.case_id, "a", rc.VerifierSignal.NEVER_PASSES, (run(case="other"),), "x")
        with self.assertRaises(ValueError):
            rc.VerifierSuspicion(r.case_id, "a", rc.VerifierSignal.NEVER_PASSES, (r, r), "x")
        with self.assertRaises(ValueError):
            rc.RunRef.parse("c", "sideways", 1)
        with self.assertRaises(ValueError):
            rc.verifier_suspicions([], min_observations=0)

    def test_summary_counts_every_signal_and_queues_each_run_once(self):
        r1, r2 = run(n=1), run(n=2)
        found = rc.verifier_suspicions([
            outcome(r1, "a", False, near=True), outcome(r2, "a", False),
            outcome(r1, "j", True, role=QUAL, gate=False)])
        summary = rc.suspicion_summary(found)
        self.assertEqual(summary["signals"], {"never_passes": 1, "format_near_miss": 1, "oracle_disagreement": 1})
        self.assertEqual(summary["evidence_class"], "diagnostic")
        self.assertEqual([item["run_number"] for item in summary["review_queue"]], [1, 2])
        self.assertEqual(summary["review_queue"][0]["suspects"],
                         ["a:format_near_miss", "a:never_passes", "a:oracle_disagreement"])


class FormattingRelaxationTests(unittest.TestCase):
    def test_removes_presentation_and_keeps_line_structure(self):
        text = "- **Refactor**: `fetchUser`   now\n> “quoted” — here\n1. item"
        self.assertEqual(rc.formatting_relaxed_text(text), 'Refactor: fetchUser now\n"quoted" - here\nitem')
        self.assertEqual(rc.formatting_relaxed_text("snake_case_name"), "snake_case_name")   # identifiers survive
        with self.assertRaises(TypeError):
            rc.formatting_relaxed_text(None)


class ModelOrderTests(unittest.TestCase):
    ORDER = rc.ModelOrder.parse("haiku, sonnet,opus")

    def test_parse_is_explicit_and_closed(self):
        self.assertEqual(self.ORDER.models, (ModelId("haiku"), ModelId("sonnet"), ModelId("opus")))
        self.assertEqual((self.ORDER.rank(ModelId("opus")), self.ORDER.rank(ModelId("gpt")), self.ORDER.rank(None)), (2, None, None))
        for bad in ("haiku", "haiku,haiku", "haiku,,opus", 3, ["haiku", ""]):
            with self.assertRaises((TypeError, ValueError)):
                rc.ModelOrder.parse(bad)

    def test_fisher_matches_the_hypergeometric_tail(self):
        def tail(k1, n1, k2, n2):
            total, successes = n1 + n2, k1 + k2
            return float(sum(Fraction(comb(successes, k) * comb(total - successes, n1 - k), comb(total, n1))
                             for k in range(k1, min(successes, n1) + 1)))
        for args in ((3, 3, 0, 3), (10, 10, 5, 10), (2, 4, 1, 4), (1, 1, 0, 5), (0, 3, 0, 3)):
            k1, n1, k2, n2 = args
            self.assertAlmostEqual(rc.fisher_one_sided_p(rc.PassCount(n1, k1), rc.PassCount(n2, k2)), tail(*args))
        self.assertEqual(rc.fisher_one_sided_p(rc.PassCount(3, 3), rc.PassCount(3, 0)), 0.05)

    def passes(self, model, variant, case, passed, failed):
        return [rc.RunPass(run(case, variant, n, model), n <= passed) for n in range(1, passed + failed + 1)]

    def test_flags_weaker_beating_stronger_per_case_and_pooled(self):
        rows = (self.passes("haiku", "with_skill", "c1", 3, 0) + self.passes("opus", "with_skill", "c1", 0, 3)
                + self.passes("haiku", "with_skill", "c2", 1, 2) + self.passes("opus", "with_skill", "c2", 2, 1)
                + self.passes("sonnet", "with_skill", "c3", 3, 0))     # sonnet has no partner case: not pooled
        check = rc.model_order_check(rows, self.ORDER)
        found = {(i.scope, str(i.weaker), str(i.stronger)): i for i in check.inversions}
        self.assertEqual(set(found), {("c1", "haiku", "opus"), ("suite", "haiku", "opus")})
        self.assertTrue(found[("c1", "haiku", "opus")].significant)
        suite = found[("suite", "haiku", "opus")]
        self.assertEqual((suite.weaker_count.passed, suite.weaker_count.runs, suite.stronger_count.passed), (4, 6, 2))
        self.assertFalse(suite.significant)
        self.assertEqual(check.compared_pairs, 2)
        self.assertEqual(check.to_dict()["significant_inversions"], 1)
        self.assertEqual(check.inversions[0].scope, "suite")                       # suite first

    def test_arms_are_compared_separately_and_unknown_models_are_reported(self):
        rows = (self.passes("haiku", "without_skill", "c1", 2, 0) + self.passes("opus", "without_skill", "c1", 0, 2)
                + self.passes("haiku", "with_skill", "c1", 0, 2) + self.passes("opus", "with_skill", "c1", 2, 0)
                + self.passes("gpt-x", "with_skill", "c1", 2, 0))
        check = rc.model_order_check(rows, self.ORDER)
        self.assertEqual({(i.scope, str(i.variant)) for i in check.inversions}, {("c1", "without_skill"), ("suite", "without_skill")})
        self.assertEqual((check.unordered_models, check.unobserved_models), (("gpt-x",), ("sonnet",)))

    def test_contradictory_values_are_refused(self):
        with self.assertRaises(ValueError):
            rc.ModelOrderInversion("c", rc.RunRef.parse("c", "with_skill", 1).variant, ModelId("a"), ModelId("b"),
                                   rc.PassCount(2, 1), rc.PassCount(2, 1))           # not an inversion
        with self.assertRaises(ValueError):
            rc.PassCount(0, 0)
        with self.assertRaises(ValueError):
            rc.PassCount(2, 3)
        r = run(model="haiku")
        with self.assertRaises(ValueError):
            rc.model_order_check([rc.RunPass(r, True), rc.RunPass(r, False)], self.ORDER)   # a run counts once
        with self.assertRaises(TypeError):
            rc.model_order_check([], ["haiku", "opus"])
        self.assertEqual(rc.model_order_not_declared()["availability"], "not_applicable")


if __name__ == "__main__":
    unittest.main()
