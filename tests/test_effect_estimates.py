"""Lift intervals, the noise check, and the floor/ceiling split.

The interval is the sign-flip test inverted, so it must exclude zero exactly
when the exact test rejects "no lift". The noise check must say when an eval
could not have shown a lift at all. A case both arms fail must never be sent
to suggest-cases for hardening."""
import json
import random
import tempfile
import unittest
from pathlib import Path

from helpers import (
    attest_answer_design,
    demo_manifest,
    judge_with_stub,
    result_row,
    run_cli,
    write_demo_manifest,
    write_run,
)

import effect_estimates as ee
import skill_benchmark as sb
from findings import CaseFlag


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

    def test_sampled_interval_agrees_with_the_sampled_test(self):
        # Past 14 cases both sample sign patterns. The test gates on a
        # conservative upper bound, so an interval that gated on the point
        # estimate excluded zero where the test reported no significance.
        rng = random.Random(11)
        choices = [-3, -2, -1, 0, 0, 1, 2, 3]
        checked = 0
        for n in (15, 18, 22, 30):
            for _ in range(40):
                deltas = thirds([rng.choice(choices) for _ in range(n)])
                if all(abs(value) < 1e-12 for value in deltas):
                    continue
                interval = ee.sign_flip_interval(deltas)
                significance = sb.sign_flip_significance(deltas)
                self.assertEqual(interval["method"], "sign-flip-inversion-sampled")
                if not interval["bounded"]:
                    continue
                with self.subTest(deltas=deltas):
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

    def test_six_cases_moving_together_are_enough(self):
        # The boundary next to the blog split: six cases that all gain one full
        # run reach p = 2/2**6 = 0.03125 <= 0.05. Every shift but +1 leaves six
        # same-sign deltas the test rejects, so the interval is the point
        # [1, 1], the noise floor is 0 and the eval can resolve the lift.
        result = self.check([1.0] * 6, [0.0] * 6)
        self.assertEqual(result["cases_moved"], 6)
        self.assertEqual(result["smallest_achievable_p"], 0.03125)
        self.assertEqual(result["noise_floor"], 0.0)
        self.assertEqual(result["verdict"], "resolvable")


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

    def test_incomplete_grading_moves_every_estimate_to_observed(self):
        # Every pair forms, but a script oracle that did not run (no
        # --allow-scripts) leaves each row's grading incomplete. The report
        # withholds the lift, and with it the interval, the noise check and
        # the graded channel, on the pooled block and on each model's.
        cases = [{"id": f"c{i}", "split": "tune", "kind": "behavior", "prompt": "Do it.",
                  "assertions": [
                      {"name": "has-alpha", "type": "contains", "value": "alpha"},
                      {"name": "oracle", "type": "script", "command": ["true"]},
                      {"name": "quality", "type": "judge", "rubric": ["Names the first Greek letter"]}]}
                 for i in range(6)]
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            path = write_demo_manifest(root, demo_manifest(cases=cases))
            runs = root / "runs"
            for i in range(6):
                for model in ("m1", "m2"):
                    write_run(runs / f"c{i}" / model / "with_skill", "alpha")
                    # c0 passes in both arms, so one delta per model is 0.
                    write_run(runs / f"c{i}" / model / "without_skill", "none" if i else "alpha")
            attest_answer_design(path, runs)
            verdicts = judge_with_stub(path, runs, root / "verdicts.jsonl",
                                       passes_on="alpha", scored=True)
            out = root / "benchmark.json"
            code, _, stderr = run_cli("benchmark", path, "--runs", runs,
                                      "--judge-results", verdicts, "--out", out)
            report = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual(code, 0, stderr)
        self.assertEqual(report["incomplete_reasons"], ["grading_evidence_incomplete"])
        withheld = {"availability": "unavailable", "reason": "grading_evidence_incomplete"}
        lift = report["paired_summary"]
        for label, block in (("pooled", lift), *lift["by_model"].items()):
            with self.subTest(block=label):
                self.assertEqual(block["interval"], withheld)
                self.assertEqual(block["noise_check"], withheld)
                # Each model pairs 6 cases, 5 of which moved; pooled, 12 and 10.
                self.assertEqual(block["observed_interval"]["n"], 12 if label == "pooled" else 6)
                self.assertEqual(block["observed_noise_check"]["cases_moved"],
                                 10 if label == "pooled" else 5)
        self.assertEqual(lift["graded"]["availability"], "partial")
        self.assertIsNone(lift["graded"]["delta"])
        self.assertEqual(lift["graded"]["reason"], "grading_evidence_incomplete")
        self.assertEqual(lift["observed_graded"]["delta"], round(10 / 12, 4))


class FloorCeilingTests(unittest.TestCase):
    def test_classification(self):
        self.assertIs(ee.ceiling_or_floor(1.0, 1.0), ee.DiscriminationFailure.CEILING)
        self.assertIs(ee.ceiling_or_floor(0.0, 0.0), ee.DiscriminationFailure.FLOOR)
        self.assertIsNone(ee.ceiling_or_floor(0.5, 0.5))
        self.assertIsNone(ee.ceiling_or_floor(None, 0.0))

    def test_suggest_cases_never_hardens_a_floor_case(self):
        report = {"case_flags": [
            {"case_id": "floor", "flags": [CaseFlag.FLOOR.value, "no objective lift", "with-skill failure"]},
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
        self.assertIn(CaseFlag.FLOOR.value, flags)
        self.assertNotIn("saturated/non-discriminating", flags)
        kinds = {finding["kind"] for finding in audit["findings"]}
        self.assertIn("floor-eval", kinds)
        self.assertNotIn("no-lift-eval", kinds)
        self.assertIn("noise_check", report["paired_summary"])

    def test_a_gate_judge_that_passes_in_one_arm_keeps_the_case_off_the_floor(self):
        # The contains check fails in both arms, but the gate judge passes the
        # with-skill answer: combined 0.5 against 0. The flag, the audit and
        # readiness decide the floor on one rule, so none of them calls this
        # case a floor, and readiness keeps it as qualitative-only.
        manifest = demo_manifest()
        manifest["cases"][0]["assertions"].append(
            {"name": "quality", "type": "judge", "severity": "gate",
             "rubric": ["Names the third Greek letter"]})
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            path = write_demo_manifest(root, manifest)
            runs = root / "runs"
            write_run(runs / "case-1" / "with_skill", "gamma")
            write_run(runs / "case-1" / "without_skill", "none")
            attest_answer_design(path, runs)
            verdicts = judge_with_stub(path, runs, root / "verdicts.jsonl", passes_on="gamma")
            report = sb.build_benchmark_report(path, runs, judge_results_path=str(verdicts))
            audit = sb.audit_manifest_report(path, runs=str(runs),
                                             judge_results_path=str(verdicts))
        self.assertEqual([(row["variant"], row["objective_pass_rate"], row["combined_pass_rate"])
                          for row in report["results"]],
                         [("with_skill", 0.0, 0.5), ("without_skill", 0.0, 0.0)])
        flags = report["case_flags"][0]["flags"]
        self.assertNotIn(CaseFlag.FLOOR.value, flags)
        self.assertNotIn("floor-eval", {finding["kind"] for finding in audit["findings"]})
        self.assertEqual(audit["readiness"]["floor_cases"], [])
        self.assertEqual(audit["readiness"]["qualitative_only_cases"], ["case-1"])


class EstimateTests(unittest.TestCase):
    def test_test_interval_and_noise_come_from_one_set_of_deltas(self):
        deltas = thirds([1, 2, 1, 0, 1, 2, 1, 1])
        estimate = ee.Estimate.from_deltas(deltas, unit=ee.InferenceUnit.CASE,
                                           without_rates=[0.3] * 8, min_lift=0.2)
        blocks = estimate.blocks()
        self.assertEqual({k: v for k, v in blocks["significance"].items() if k != "unit"},
                         sb.sign_flip_significance(deltas))
        self.assertEqual({k: v for k, v in blocks["interval"].items() if k != "unit"},
                         ee.sign_flip_interval(deltas))
        self.assertEqual({blocks[name]["unit"] for name in blocks}, {"case"})
        self.assertEqual(blocks["noise_check"]["min_lift"], 0.2)

    def test_without_baseline_rates_there_is_no_noise_check(self):
        estimate = ee.Estimate.from_deltas([1.0] * 6, unit=ee.InferenceUnit.QUERY)
        self.assertNotIn("noise_check", estimate.blocks())
        self.assertTrue(estimate.significant)

    def test_the_sample_size_note_names_the_unit_and_what_repeats_cannot_do(self):
        case_note = ee.minimum_units_note(ee.InferenceUnit.CASE)
        self.assertIn("at least 6 cases", case_note)
        self.assertIn("do not add cases", case_note)
        replicate_note = ee.minimum_units_note(ee.InferenceUnit.REPLICATE_PAIR)
        self.assertIn("at least 6 matched replicate pairs", replicate_note)
        self.assertNotIn("do not add cases", replicate_note)
        self.assertIn("at least 8 authored queries",
                      ee.minimum_units_note(ee.InferenceUnit.QUERY, alpha=0.01))


if __name__ == "__main__":
    unittest.main()
