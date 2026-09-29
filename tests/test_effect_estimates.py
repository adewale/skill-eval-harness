"""Lift intervals, the noise check, and the floor/ceiling split.

The interval is the sign-flip test inverted, so it must exclude zero exactly
when the exact test rejects "no lift". The noise check must say when an eval
could not have shown a lift at all. A case both arms fail must never be sent
to suggest-cases for hardening."""
import random
import tempfile
import unittest
from pathlib import Path

from helpers import attest_answer_design, demo_manifest, result_row, write_demo_manifest

import effect_estimates as ee
import skill_benchmark as sb


def thirds(values: list[int]) -> list[float]:
    """Per-case deltas at three repeats per arm, the blog's customer-support shape."""
    return [value / 3 for value in values]


class IntervalAgreesWithTheTestTests(unittest.TestCase):
    def test_interval_excludes_zero_exactly_when_the_exact_test_rejects(self):
        rng = random.Random(7)
        choices = [-3, -2, -1, 0, 0, 1, 2, 3]
        checked = 0
        for n in range(6, 13):
            for _ in range(25):
                deltas = thirds([rng.choice(choices) for _ in range(n)])
                if all(abs(value) < 1e-12 for value in deltas):
                    continue
                interval = ee.sign_flip_interval(deltas)
                significance = sb.sign_flip_significance(deltas)
                with self.subTest(deltas=deltas):
                    if not interval["bounded"]:
                        self.assertFalse(significance["significant_at_0_05"])
                        continue
                    zero_inside = interval["lower"] <= 0 <= interval["upper"]
                    self.assertEqual(zero_inside, not significance["significant_at_0_05"])
                    checked += 1
        self.assertGreater(checked, 100)

    def test_interval_contains_the_observed_mean(self):
        deltas = thirds([1, 2, 0, 3, 1, -1, 2, 1])
        interval = ee.sign_flip_interval(deltas)
        mean = sum(deltas) / len(deltas)
        self.assertTrue(interval["bounded"])
        self.assertLess(interval["lower"], mean)
        self.assertGreater(interval["upper"], mean)

    def test_too_few_cases_cannot_bound_anything(self):
        # 2 / 2**5 = 0.0625 > 0.05: five cases can never exclude any shift.
        interval = ee.sign_flip_interval([0.5, 0.5, 0.5, 0.5, 0.5])
        self.assertFalse(interval["bounded"])
        self.assertIsNone(interval["lower"])
        self.assertIn("at least 6", interval["reason"])
        # 2 / 2**6 = 0.03125 <= 0.05: the sixth case makes a bound possible.
        self.assertTrue(ee.sign_flip_interval([0.5] * 6)["bounded"])

    def test_sampled_path_is_deterministic_and_order_invariant(self):
        rng = random.Random(3)
        deltas = [rng.choice([-1.0, 0.0, 0.5, 1.0]) for _ in range(30)]
        first = ee.sign_flip_interval(deltas)
        second = ee.sign_flip_interval(list(reversed(deltas)))
        self.assertEqual(first, second)
        self.assertEqual(first["method"], "sign-flip-inversion-sampled")

    def test_empty_and_non_finite_input(self):
        self.assertFalse(ee.sign_flip_interval([])["bounded"])
        with self.assertRaises(ValueError):
            ee.sign_flip_interval([float("nan"), 1.0])


class NoiseCheckTests(unittest.TestCase):
    def check(self, deltas, without, **options):
        return ee.noise_check(deltas, without, interval=ee.sign_flip_interval(deltas), **options)

    def test_blog_held_out_split_cannot_reach_significance(self):
        # 14 held-out tickets, 3 repeats each, net +5 runs: at best five
        # tickets moved one run each, so p can never go below 2/2**5.
        deltas = thirds([1, 1, 1, 1, 1] + [0] * 9)
        result = self.check(deltas, [0.79] * 14)
        self.assertEqual(result["verdict"], "too-few-cases-moved")
        self.assertEqual(result["cases_moved"], 5)
        self.assertEqual(result["smallest_achievable_p"], 0.0625)
        self.assertEqual(result["cases_needed_for_alpha"], 6)

    def test_noise_larger_than_headroom(self):
        deltas = thirds([3, -3, 3, -3, 3, -3, 2, -2])
        result = self.check(deltas, [0.95] * len(deltas))
        self.assertEqual(result["verdict"], "noise-exceeds-headroom")
        self.assertIn("projected_cases", result)

    def test_min_lift_the_author_would_act_on(self):
        deltas = thirds([1, 2, 1, 0, 1, 2, 1, 1, 0, 1])
        result = self.check(deltas, [0.3] * len(deltas), min_lift=0.05)
        self.assertEqual(result["verdict"], "noise-exceeds-min-lift")
        self.assertEqual(result["min_lift"], 0.05)
        self.assertGreater(result["projected_cases"], len(deltas))

    def test_resolvable_when_the_floor_is_small(self):
        deltas = [0.5] * 12
        result = self.check(deltas, [0.2] * 12, min_lift=0.3)
        self.assertEqual(result["verdict"], "resolvable")

    def test_smallest_achievable_p_and_cases_needed(self):
        self.assertEqual(ee.smallest_achievable_p(0), 1.0)
        self.assertEqual(ee.smallest_achievable_p(6), 0.03125)
        self.assertEqual(ee.cases_needed_for_alpha(0.05), 6)
        self.assertEqual(ee.cases_needed_for_alpha(0.01), 8)


class PairedSummaryTests(unittest.TestCase):
    def rows(self, pairs):
        out = []
        for index, (with_rate, without_rate) in enumerate(pairs):
            case = f"c{index}"
            out.append(result_row(case, "with_skill", rate=with_rate, run_number=1))
            out.append(result_row(case, "without_skill", rate=without_rate, run_number=1))
        return out

    def test_summary_carries_interval_and_noise_check(self):
        summary = sb.build_paired_summary(self.rows([(1.0, 0.0)] * 8), min_lift=0.2)
        self.assertTrue(summary["interval"]["bounded"])
        self.assertGreater(summary["interval"]["lower"], 0)
        self.assertEqual(summary["noise_check"]["cases_moved"], 8)
        self.assertEqual(summary["noise_check"]["min_lift"], 0.2)

    def test_blocked_pairing_moves_both_to_observed(self):
        rows = self.rows([(1.0, 0.0)] * 8)
        rows.append(result_row("orphan", "with_skill", rate=1.0, run_number=1))
        summary = sb.build_paired_summary(rows)
        self.assertEqual(summary["availability"], "partial")
        self.assertEqual(summary["interval"]["availability"], "unavailable")
        self.assertTrue(summary["observed_interval"]["bounded"])
        self.assertIn("verdict", summary["observed_noise_check"])


class FloorCeilingTests(unittest.TestCase):
    def test_classification(self):
        self.assertIs(ee.ceiling_or_floor(1.0, 1.0), ee.DiscriminationFailure.CEILING)
        self.assertIs(ee.ceiling_or_floor(0.0, 0.0), ee.DiscriminationFailure.FLOOR)
        self.assertIsNone(ee.ceiling_or_floor(0.5, 0.5))
        self.assertIsNone(ee.ceiling_or_floor(None, 0.0))

    def test_suggest_cases_never_hardens_a_floor_case(self):
        report = {"case_flags": [
            {"case_id": "floor", "flags": [sb.FLOOR_FLAG, "no objective lift", "with-skill failure"]},
            {"case_id": "ceiling", "flags": ["saturated/non-discriminating", "no objective lift"]},
        ]}
        manifest = {"cases": [{"id": "floor", "prompt": "p", "assertions": []},
                              {"id": "ceiling", "prompt": "q", "assertions": []}]}
        seeds = sb.suggest_case_candidates(report, manifest)
        self.assertEqual([seed["case_id"] for seed in seeds], ["ceiling"])
        self.assertIn("why the case is hard", seeds[0]["instruction"])

    def res(self, case, variant, rate, intent="capability"):
        return {"case_id": case, "variant": variant, "run_number": 1,
                "objective_pass_rate": rate, "combined_pass_rate": rate,
                "missing_output": False, "execution_valid": True, "eval_intent": intent}

    def test_readiness_separates_floor_from_base_saturation(self):
        report = {"availability": "complete", "results": [
            self.res("floor", "with_skill", 0.0), self.res("floor", "without_skill", 0.0),
            self.res("guard", "with_skill", 0.0, "regression"),
            self.res("guard", "without_skill", 0.0, "regression"),
            self.res("ceiling", "with_skill", 1.0), self.res("ceiling", "without_skill", 1.0),
        ]}
        signals = sb.readiness_run_signals(report)
        self.assertEqual(signals["floor_cases"], ["floor", "guard"])
        self.assertEqual(signals["base_saturated_cases"], ["ceiling"])
        # A regression guard nothing passes is broken, not holding.
        self.assertEqual(signals["base_saturated_expected_cases"], [])


class FloorEndToEndTests(unittest.TestCase):
    def test_graded_floor_case_is_flagged_and_audited(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            path = write_demo_manifest(root, demo_manifest())
            runs = root / "runs"
            for variant in ("with_skill", "without_skill"):
                base = runs / "case-1" / variant
                base.mkdir(parents=True)
                (base / "output.md").write_text("no match here", encoding="utf-8")
            attest_answer_design(path, runs)
            report = sb.build_benchmark_report(path, runs)
            audit = sb.audit_manifest_report(path, runs=str(runs))
        flags = report["case_flags"][0]["flags"]
        self.assertIn(sb.FLOOR_FLAG, flags)
        self.assertNotIn("saturated/non-discriminating", flags)
        kinds = {finding["kind"] for finding in audit["findings"]}
        self.assertIn("floor-eval", kinds)
        self.assertNotIn("no-lift-eval", kinds)
        self.assertIn("noise_check", report["paired_summary"])


if __name__ == "__main__":
    unittest.main()
