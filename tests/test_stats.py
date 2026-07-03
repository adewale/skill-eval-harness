"""Closed-form statistics: unbiased pass@k / pass^k, paired reliability lift, permutation tests.

Classes moved verbatim from the PR-named test files (test_audit_fixes,
test_roadmap_features, test_followup_features, test_external_review_gaps,
test_cbc) and test_skill_benchmark, which accreted by merge rather than by
subject; docstrings citing finding/roadmap ids are preserved.
"""
import argparse
import contextlib
import io
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import skill_benchmark as sb
import run_pi_trigger_eval as tr
import ablation_model as am
from helpers import (
    CODEX_CRASH_OUTPUT as CRASH,
    CONTAINS_APPROVED_CASE as CASE,
    demo_manifest as base_manifest,
    good_pr_manifest as _manifest,
    load_example_module,
    make_eval_repo,
    report_fixture,
    write_demo_manifest as write_manifest,
    write_good_pr_skill as _skill,
    write_run,
)

ROOT = Path(__file__).resolve().parents[1]


class PassAtKTests(unittest.TestCase):
    def test_unbiased_pass_at_k_matches_known_value(self):
        # n=10, c=3, k=5 -> 0.9167 (unbiased), NOT the biased 1-(1-0.3)^5=0.832.
        self.assertAlmostEqual(sb.pass_at_k(10, 3, 5), 0.916667, places=5)
        self.assertEqual(sb.pass_at_k(4, 4, 2), 1.0)      # all succeed
        self.assertEqual(sb.pass_at_k(4, 0, 2), 0.0)      # none succeed
        self.assertAlmostEqual(sb.pass_at_k(3, 1, 1), 1 / 3)   # pass@1 == c/n
        self.assertIsNone(sb.pass_at_k(3, 1, 4))          # k > n

    def test_pass_hat_k_is_all_k_succeed(self):
        self.assertAlmostEqual(sb.pass_hat_k(10, 3, 2), 3 / 45)
        self.assertEqual(sb.pass_hat_k(4, 4, 4), 1.0)     # every run passes
        self.assertEqual(sb.pass_hat_k(4, 1, 2), 0.0)     # fewer successes than k
        self.assertEqual(sb.pass_hat_k(4, 3, 4), 0.0)     # c<n at k=n -> not all pass

    def test_k_below_one_and_n_zero_return_none(self):
        # boundary: the `k < 1` and `n <= 0` guards (a k>=0 mutation must be caught)
        self.assertIsNone(sb.pass_at_k(3, 1, 0))
        self.assertIsNone(sb.pass_hat_k(3, 1, 0))
        self.assertIsNone(sb.pass_at_k(0, 0, 1))

    def test_monotonicity_properties(self):
        # pass@k non-decreasing in k; pass^k non-increasing in k (mathematical-properties)
        n, c = 10, 4
        at = [sb.pass_at_k(n, c, k) for k in range(1, n + 1)]
        hat = [sb.pass_hat_k(n, c, k) for k in range(1, n + 1)]
        self.assertEqual(at, sorted(at))
        self.assertEqual(hat, sorted(hat, reverse=True))

    def test_reliability_report_pools_per_variant(self):
        results = []
        # with_skill: 3 of 4 runs fully pass; without_skill: 0 of 4.
        for i in range(4):
            results.append({"case_id": "c1", "variant": "with_skill", "objective_pass_rate": 1.0 if i < 3 else 0.0})
            results.append({"case_id": "c1", "variant": "without_skill", "objective_pass_rate": 0.0})
        rel = sb.build_reliability(results)
        w = rel["by_case_variant"]["c1"]["with_skill"]
        self.assertEqual((w["n"], w["c"]), (4, 3))
        self.assertAlmostEqual(w["pass_at_1"], 0.75)
        # the full estimator maps are inspected, not just pass_at_1 (kills a
        # drop-the-dict / swap-the-dicts mutation)
        self.assertEqual(w["pass_at_k"]["4"], 1.0)     # 3 of 4 pass -> drawing all 4 always includes a pass
        self.assertEqual(w["pass_hat_k"]["4"], 0.0)    # ...but not all 4 pass
        self.assertEqual(rel["by_variant"]["without_skill"]["mean_pass_at_1"], 0.0)
        self.assertAlmostEqual(rel["by_variant"]["with_skill"]["mean_pass_at_1"], 0.75)
        self.assertEqual(rel["by_variant"]["with_skill"]["all_runs_pass_rate"], 0.0)  # not every run passed

    def test_all_runs_pass_rate_reaches_one(self):
        # the c==n side of all_runs_pass_rate (a constant-0.0 mutation must be caught)
        results = [{"case_id": "c1", "variant": "with_skill", "objective_pass_rate": 1.0} for _ in range(3)]
        rel = sb.build_reliability(results)
        self.assertEqual(rel["by_variant"]["with_skill"]["all_runs_pass_rate"], 1.0)


class PairedReliabilityLiftTests(unittest.TestCase):
    """G6 — paired with_skill − without_skill lift on pass@k / pass^k, the sliver
    of the reliability item (#5) that build_reliability leaves per-arm."""

    @staticmethod
    def _arm(case_id, variant, n, c, model=None):
        rows = []
        for i in range(n):
            r = {"case_id": case_id, "variant": variant, "objective_pass_rate": 1.0 if i < c else 0.0}
            if model is not None:
                r["model"] = model
            rows.append(r)
        return rows

    def test_paired_case_counts_matches_per_arm_and_drops_unpaired(self):
        results = (self._arm("c1", "with_skill", 4, 3) + self._arm("c1", "without_skill", 4, 0)
                   + self._arm("c2", "with_skill", 3, 2)       # no without arm -> dropped
                   + self._arm("c3", "without_skill", 3, 1))   # no with arm -> dropped
        pairs = {cid: (w, n) for cid, w, n in sb.paired_case_counts(results)}
        self.assertEqual(set(pairs), {"c1"})
        self.assertEqual(pairs["c1"], ((4, 3), (4, 0)))
        rel = sb.build_reliability(results)["by_case_variant"]["c1"]
        self.assertEqual((rel["with_skill"]["n"], rel["with_skill"]["c"]), (4, 3))

    def test_per_case_delta_equals_estimator_difference(self):
        results = self._arm("c1", "with_skill", 4, 3) + self._arm("c1", "without_skill", 4, 0)
        bc = sb.paired_reliability_block(sb.paired_case_counts(results))["by_case"]["c1"]
        for k in range(1, 5):
            self.assertAlmostEqual(bc["pass_at_k_delta"][str(k)],
                                   round(sb.pass_at_k(4, 3, k) - sb.pass_at_k(4, 0, k), 6))
            self.assertAlmostEqual(bc["pass_hat_k_delta"][str(k)],
                                   round(sb.pass_hat_k(4, 3, k) - sb.pass_hat_k(4, 0, k), 6))
        self.assertNotIn("5", bc["pass_at_k_delta"])   # k never exceeds n

    def test_k_capped_at_min_of_the_two_arms(self):
        results = self._arm("c1", "with_skill", 4, 4) + self._arm("c1", "without_skill", 2, 0)
        bc = sb.paired_reliability_block(sb.paired_case_counts(results))["by_case"]["c1"]
        self.assertEqual(set(bc["pass_at_k_delta"]), {"1", "2"})

    def test_sign_convention(self):
        helps = self._arm("c1", "with_skill", 4, 3) + self._arm("c1", "without_skill", 4, 0)
        hurts = self._arm("c1", "with_skill", 4, 1) + self._arm("c1", "without_skill", 4, 3)
        self.assertGreater(sb.paired_reliability_block(sb.paired_case_counts(helps))["by_case"]["c1"]["pass_at_1_delta"], 0)
        self.assertLess(sb.paired_reliability_block(sb.paired_case_counts(hurts))["by_case"]["c1"]["pass_at_1_delta"], 0)

    def test_pooling_per_k_excludes_short_cases(self):
        results = (self._arm("c1", "with_skill", 4, 4) + self._arm("c1", "without_skill", 4, 0)
                   + self._arm("c2", "with_skill", 2, 2) + self._arm("c2", "without_skill", 2, 0))
        pooled = sb.paired_reliability_block(sb.paired_case_counts(results))["pooled"]
        self.assertEqual(pooled["cases"], 2)
        # k=4 exists only from c1 (c2 has 2 runs), so its pooled mean equals c1's own k=4 delta
        c1 = sb.paired_reliability_block(sb.paired_case_counts(
            self._arm("c1", "with_skill", 4, 4) + self._arm("c1", "without_skill", 4, 0)))["by_case"]["c1"]
        self.assertAlmostEqual(pooled["mean_pass_at_k_delta"]["4"], c1["pass_at_k_delta"]["4"])

    def test_significance_is_signflip_over_pass_at_1_deltas(self):
        results = []
        for i in range(6):
            results += self._arm(f"c{i}", "with_skill", 3, 3) + self._arm(f"c{i}", "without_skill", 3, 0)
        block = sb.paired_reliability_block(sb.paired_case_counts(results))
        deltas = [block["by_case"][cid]["pass_at_1_delta"] for cid in sorted(block["by_case"])]
        self.assertEqual(block["pooled"]["significance"], sb.sign_flip_significance(deltas))
        self.assertTrue(block["pooled"]["significance"]["significant_at_0_05"])   # 6 unanimous cases

    def test_zero_delta_not_significant(self):
        results = self._arm("c1", "with_skill", 3, 2) + self._arm("c1", "without_skill", 3, 2)
        block = sb.paired_reliability_block(sb.paired_case_counts(results))
        self.assertEqual(block["by_case"]["c1"]["pass_at_1_delta"], 0.0)
        self.assertFalse(block["pooled"]["significance"]["significant_at_0_05"])

    def test_by_model_present_and_pooled_keys_tagged(self):
        results = (self._arm("c1", "with_skill", 3, 3, model="m1") + self._arm("c1", "without_skill", 3, 0, model="m1")
                   + self._arm("c1", "with_skill", 3, 0, model="m2") + self._arm("c1", "without_skill", 3, 0, model="m2"))
        out = sb.build_paired_reliability(results)
        self.assertEqual(set(out["by_model"]), {"m1", "m2"})
        self.assertGreater(out["by_model"]["m1"]["by_case"]["c1"]["pass_at_1_delta"], 0)
        self.assertEqual(out["by_model"]["m2"]["by_case"]["c1"]["pass_at_1_delta"], 0.0)
        self.assertEqual(set(out["by_case"]), {"c1@m1", "c1@m2"})   # model-tagged, no collision

    def test_unlabeled_uses_fallback_no_by_model(self):
        out = sb.build_paired_reliability(self._arm("c1", "with_skill", 3, 3) + self._arm("c1", "without_skill", 3, 0))
        self.assertNotIn("by_model", out)
        self.assertEqual(set(out["by_case"]), {"c1"})

    def test_deterministic(self):
        results = self._arm("c1", "with_skill", 4, 3) + self._arm("c1", "without_skill", 4, 1)
        self.assertEqual(sb.build_paired_reliability(results), sb.build_paired_reliability(results))

    def test_end_to_end_attached_and_per_arm_untouched(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "repo" / "skill").mkdir(parents=True)
            (root / "repo" / "skill" / "SKILL.md").write_text("---\nname: demo\ndescription: Demo\n---\n", encoding="utf-8")
            (root / "repo" / "evals").mkdir()
            manifest = {
                "version": 1, "skill_name": "demo", "skill_paths": ["skill/SKILL.md"],
                "variants": ["with_skill", "without_skill"],
                "cases": [{"id": "case-1", "split": "tune", "kind": "behavior", "prompt": "Do it.",
                           "assertions": [{"name": "has-alpha", "type": "contains", "value": "alpha"}]}],
                "ablations": [],
            }
            path = root / "repo" / "evals" / "shared-benchmark.json"
            path.write_text(json.dumps(manifest), encoding="utf-8")
            runs = root / "runs"
            for variant, text in [("with_skill", "alpha"), ("without_skill", "nope")]:
                base = runs / "case-1" / variant
                base.mkdir(parents=True)
                (base / "output.md").write_text(text, encoding="utf-8")
            report = sb.build_benchmark_report(path, runs)
        rel = report["reliability"]
        self.assertIn("by_case_variant", rel)   # per-arm keys untouched
        self.assertIn("by_variant", rel)
        self.assertIn("paired_lift", rel)        # new nested block attached
        self.assertEqual(rel["paired_lift"]["by_case"]["case-1"]["pass_at_1_delta"], 1.0)


class TwoSamplePermutationTests(unittest.TestCase):
    def test_single_run_per_arm_never_significant(self):
        r = sb.two_sample_permutation_significance([1.0], [0.0])
        self.assertEqual(r["p_value"], 1.0)
        self.assertFalse(r["significant_at_0_05"])

    def test_clean_separation_becomes_significant_by_four_per_arm(self):
        self.assertFalse(sb.two_sample_permutation_significance([1, 1, 1], [0, 0, 0])["significant_at_0_05"])  # p=0.1
        self.assertTrue(sb.two_sample_permutation_significance([1, 1, 1, 1], [0, 0, 0, 0])["significant_at_0_05"])  # p=0.0286

    def test_symmetric_under_group_swap(self):
        # two-sided: the p-value must not depend on which arm is passed first (a
        # `target = observed` one-sided mutation makes the reversed order differ)
        fwd = sb.two_sample_permutation_significance([1, 1, 1, 1], [0, 0, 0, 0])
        rev = sb.two_sample_permutation_significance([0, 0, 0, 0], [1, 1, 1, 1])
        self.assertEqual(fwd["p_value"], rev["p_value"])
        self.assertTrue(rev["significant_at_0_05"])   # reversed order still fires

    def test_method_edges_and_exact_vs_sampled(self):
        self.assertEqual(sb.two_sample_permutation_significance([1, 1, 1, 1], [0, 0, 0, 0])["method"], "two-sample-permutation-exact")
        big = sb.two_sample_permutation_significance([1.0] * 12, [0.0] * 12)   # total 24 -> sampled
        self.assertEqual(big["method"], "two-sample-permutation-sampled")
        self.assertIsNone(sb.two_sample_permutation_significance([], [1, 2])["p_value"])   # empty arm
        self.assertEqual(sb.two_sample_permutation_significance([1, 1], [1, 1])["p_value"], 1.0)  # all equal

    def test_deterministic_under_sampling(self):
        a, b = [1.0] * 12, [0.0] * 12   # total 24 -> sampled branch, seeded
        r1, r2 = sb.two_sample_permutation_significance(a, b), sb.two_sample_permutation_significance(a, b)
        self.assertEqual(r1, r2)
        self.assertEqual(r1["method"], "two-sample-permutation-sampled")   # actually exercising the sampled path

    def test_noisy_non_degenerate_effects(self):
        # not perfect separation: a strong noisy effect (7-1 vs 1-7) is significant;
        # a moderate one (4-1 vs 1-4, exact) is not; a weak one is not.
        strong = sb.two_sample_permutation_significance([1, 1, 1, 1, 1, 1, 1, 0], [0, 0, 0, 0, 0, 0, 0, 1])
        self.assertTrue(strong["significant_at_0_05"])
        self.assertLess(strong["p_value"], 0.05)
        moderate = sb.two_sample_permutation_significance([1, 1, 1, 1, 0], [0, 0, 0, 0, 1])
        self.assertFalse(moderate["significant_at_0_05"])
        self.assertAlmostEqual(moderate["p_value"], 0.206349, places=5)   # exact, deterministic
        self.assertFalse(sb.two_sample_permutation_significance([1, 1, 0, 0], [1, 0, 0, 0])["significant_at_0_05"])

    def test_sampled_p_is_never_exact_zero(self):
        # (b+1)/(m+1) Monte-Carlo estimator: a sampled p can never be an impossible 0.0
        r = sb.two_sample_permutation_significance([1.0] * 12, [0.0] * 12)   # sampled, perfect separation
        self.assertGreater(r["p_value"], 0.0)
        self.assertAlmostEqual(r["p_value"], 1 / 4097, places=6)
        # and the pre-existing sign-flip sampled branch (n>14) is corrected too
        self.assertGreater(sb.sign_flip_significance([0.5] * 20)["p_value"], 0.0)


if __name__ == "__main__":
    unittest.main()
