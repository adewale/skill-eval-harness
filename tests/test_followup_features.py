"""Tests for the follow-up features (1, 2, 5, 6, 8) layered on the roadmap:

- 1: the ablation confirmation gate requires statistical significance across
     replicates (the core gate change is covered in test_skill_benchmark's
     AblationRegressionReportTests; here we unit-test the estimator it uses).
- 2: judge-vs-human alignment (Cohen's kappa, precision/recall/F1).
- 5: pass@k / pass^k unbiased estimators + the reliability report block.
- 6: BFCL-style tool_call taxonomy (expected_no_call / required_calls / call_set).
- 8: error-analysis review queue + axial failure taxonomy.

All model-free and deterministic.
"""
import json
import tempfile
import unittest
from pathlib import Path

import skill_benchmark as sb


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


class JudgeAlignmentTests(unittest.TestCase):
    def test_cohen_kappa_chance_corrects(self):
        self.assertEqual(sb.cohen_kappa([True, False, True, False], [True, False, True, False]), 1.0)
        self.assertEqual(sb.cohen_kappa([True, True], [True, True]), 1.0)  # degenerate-agree
        self.assertAlmostEqual(sb.cohen_kappa([True, False, True, False], [False, True, False, True]), -1.0)

    def test_alignment_confusion_and_warnings(self):
        human = {"a": {"passed": True}, "b": {"passed": False}, "c": {"passed": True}}
        judge = {"a": {"passed": True}, "b": {"passed": True}, "c": {"passed": True}}   # b is a false positive
        rep = sb.judge_alignment_report(human, judge)
        self.assertEqual(rep["confusion"], {"tp": 2, "fp": 1, "fn": 0, "tn": 0})
        self.assertAlmostEqual(rep["agreement"], 2 / 3, places=4)   # report rounds to 4dp
        self.assertAlmostEqual(rep["recall"], 1.0)              # caught every real pass
        self.assertAlmostEqual(rep["precision"], 2 / 3, places=4)  # but passed a human-fail
        self.assertTrue(any("unstable" in w for w in rep["warnings"]))  # n=3 < 50

    def test_no_overlap_is_flagged_not_crashed(self):
        rep = sb.judge_alignment_report({"x": {"passed": True}}, {"y": {"passed": True}})
        self.assertEqual(rep["n"], 0)
        self.assertIsNone(rep["agreement"])
        self.assertTrue(rep["warnings"])

    def test_f1_is_zero_not_none_for_label_inverting_judge(self):
        # the worst judge (inverts every label -> tp=0) must report F1=0.0, not None,
        # or a reviewer scanning for low F1 skips the null. Regression for the audit bug.
        human = {"a": {"passed": True}, "b": {"passed": False}}
        judge = {"a": {"passed": False}, "b": {"passed": True}}
        rep = sb.judge_alignment_report(human, judge)
        self.assertEqual(rep["confusion"], {"tp": 0, "fp": 1, "fn": 1, "tn": 0})
        self.assertEqual(rep["f1"], 0.0)
        self.assertEqual(rep["precision"], 0.0)
        self.assertEqual(rep["recall"], 0.0)

    def test_f1_none_only_when_no_positives_exist(self):
        # tp=fp=fn=0 (everything a true negative) -> F1 genuinely undefined
        rep = sb.judge_alignment_report({"a": {"passed": False}}, {"a": {"passed": False}})
        self.assertIsNone(rep["f1"])
        self.assertEqual(rep["confusion"], {"tp": 0, "fp": 0, "fn": 0, "tn": 1})

    def test_kappa_band_thresholds(self):
        self.assertEqual(sb.kappa_band(0.9), "almost-perfect")
        self.assertEqual(sb.kappa_band(0.8), "substantial")   # strict > boundary
        self.assertEqual(sb.kappa_band(0.5), "moderate")
        self.assertEqual(sb.kappa_band(0.3), "fair")
        self.assertEqual(sb.kappa_band(0.1), "slight")
        self.assertEqual(sb.kappa_band(0.0), "poor (<= chance)")
        self.assertEqual(sb.kappa_band(-0.2), "poor (<= chance)")
        self.assertIsNone(sb.kappa_band(None))


class ToolCallTaxonomyTests(unittest.TestCase):
    def _events(self, names):
        td = Path(tempfile.mkdtemp(prefix="toolcall-"))
        events = [{"type": "tool_call", "name": n, "status": "completed"} for n in names]
        (td / "events.json").write_text(json.dumps(events), encoding="utf-8")
        (td / "output.md").write_text("out", encoding="utf-8")
        return td

    def test_expected_no_call(self):
        base = self._events(["Read", "Grep"])
        ok = sb.assertion_result({"type": "tool_call", "tool": "WebSearch", "expected_no_call": True}, "t", base / "output.md", run_base=base)
        self.assertTrue(ok["passed"])   # WebSearch never called -> passes
        bad = sb.assertion_result({"type": "tool_call", "tool": "Read", "expected_no_call": True}, "t", base / "output.md", run_base=base)
        self.assertFalse(bad["passed"])  # Read WAS called

    def test_required_calls_subset(self):
        base = self._events(["Read", "Grep", "Edit"])
        ok = sb.assertion_result({"type": "tool_call", "required_calls": ["Read", "Edit"]}, "t", base / "output.md", run_base=base)
        self.assertTrue(ok["passed"])   # both present, extras (Grep) allowed
        miss = sb.assertion_result({"type": "tool_call", "required_calls": ["Read", "WebSearch"]}, "t", base / "output.md", run_base=base)
        self.assertFalse(miss["passed"])

    def test_call_set_multiset_rejects_unexpected_and_missing(self):
        base = self._events(["Read", "Grep"])
        ok = sb.assertion_result({"type": "tool_call", "call_set": ["Read", "Grep"]}, "t", base / "output.md", run_base=base)
        self.assertTrue(ok["passed"])
        extra = sb.assertion_result({"type": "tool_call", "call_set": ["Read"]}, "t", base / "output.md", run_base=base)
        self.assertFalse(extra["passed"])   # Grep is unexpected
        missing = sb.assertion_result({"type": "tool_call", "call_set": ["Read", "Grep", "Edit"]}, "t", base / "output.md", run_base=base)
        self.assertFalse(missing["passed"])  # Edit is missing (drives the missing branch)

    def test_call_set_counts_multiplicity(self):
        base = self._events(["Read", "Read"])   # two Reads
        self.assertFalse(sb.assertion_result({"type": "tool_call", "call_set": ["Read"]}, "t", base / "output.md", run_base=base)["passed"])
        self.assertTrue(sb.assertion_result({"type": "tool_call", "call_set": ["Read", "Read"]}, "t", base / "output.md", run_base=base)["passed"])

    def test_name_match_is_not_substring(self):
        # a shell `cat readme` (a command event, no tool name) must NOT satisfy
        # required_calls:["Read"] — the audit's core false-positive.
        td = Path(tempfile.mkdtemp(prefix="toolcall-cmd-"))
        (td / "events.json").write_text(json.dumps([{"type": "command", "command": "cat README.md", "status": "completed"}]), encoding="utf-8")
        (td / "output.md").write_text("out", encoding="utf-8")
        res = sb.assertion_result({"type": "tool_call", "required_calls": ["Read"]}, "t", td / "output.md", run_base=td)
        self.assertFalse(res["passed"])
        # and a real tool named ReadFile does not satisfy "Read" (exact, not prefix)
        base = self._events(["ReadFile"])
        self.assertFalse(sb.assertion_result({"type": "tool_call", "required_calls": ["Read"]}, "t", base / "output.md", run_base=base)["passed"])

    def test_expected_no_call_with_pattern(self):
        base = self._events(["curl", "Read"])
        self.assertFalse(sb.assertion_result({"type": "tool_call", "expected_no_call": True, "pattern": "curl"}, "t", base / "output.md", run_base=base)["passed"])
        self.assertTrue(sb.assertion_result({"type": "tool_call", "expected_no_call": True, "pattern": "wget"}, "t", base / "output.md", run_base=base)["passed"])


class ErrorAnalysisTests(unittest.TestCase):
    def test_taxonomy_and_review_queue(self):
        report = {
            "results": [
                {"case_id": "c1", "variant": "with_skill", "objective_pass_rate": 1.0, "assertions": [{"name": "x", "passed": True}], "qualitative_assertions": []},
                {"case_id": "c1", "variant": "without_skill", "objective_pass_rate": 0.0, "assertions": [{"name": "detect-weak", "type": "contains", "passed": False, "evidence": "missing"}], "qualitative_assertions": []},
                {"case_id": "c2", "variant": "without_skill", "objective_pass_rate": 0.0, "assertions": [{"name": "detect-weak", "type": "contains", "passed": False, "evidence": "missing"}], "qualitative_assertions": []},
                {"case_id": "c3", "variant": "with_skill", "missing_output": True, "assertions": [], "qualitative_assertions": []},
            ],
            "case_flags": [{"case_id": "c1", "flags": ["saturated/non-discriminating", "flaky repeated pass rates: with_skill"]}],
        }
        out = sb.error_analysis_report(report)
        self.assertEqual(out["summary"]["failing_or_errored_runs"], 3)   # the passing run is not a datum
        top = out["taxonomy"][0]
        self.assertEqual(top["category"], "text:detect-weak")            # the dominant first-failure
        self.assertEqual(top["count"], 2)
        self.assertAlmostEqual(top["share"], 2 / 3, places=4)   # report rounds to 4dp
        self.assertIn("missing-output", {b["category"] for b in out["taxonomy"]})
        self.assertEqual(out["case_flag_histogram"]["saturated/non-discriminating"], 1)

    def test_execution_error_critical_and_judge_categories(self):
        report = {"results": [
            {"case_id": "c1", "variant": "with_skill", "execution_valid": False, "assertions": [], "qualitative_assertions": []},
            {"case_id": "c2", "variant": "with_skill", "vetoed": True, "critical_failures": ["wrote-outside-results"], "assertions": [], "qualitative_assertions": []},
            {"case_id": "c3", "variant": "with_skill", "objective_pass_rate": 1.0, "assertions": [{"name": "ok", "passed": True}],
             "qualitative_assertions": [{"name": "rubric", "type": "judge", "passed": False, "evidence": "weak"}]},
        ], "case_flags": []}
        out = sb.error_analysis_report(report)
        cats = {b["category"] for b in out["taxonomy"]}
        self.assertIn("execution-error", cats)
        self.assertIn("critical-failure:wrote-outside-results", cats)
        self.assertIn("judge:rubric", cats)   # a qualitative first-failure classifies as judge

    def test_review_queue_limit_truncates(self):
        report = {"results": [
            {"case_id": f"c{i}", "variant": "without_skill", "objective_pass_rate": 0.0,
             "assertions": [{"name": "x", "type": "contains", "passed": False}], "qualitative_assertions": []}
            for i in range(5)
        ], "case_flags": []}
        out = sb.error_analysis_report(report, limit=2)
        self.assertEqual(len(out["review_queue"]), 2)
        self.assertEqual(out["review_queue_truncated"], 3)
        self.assertEqual(out["summary"]["failing_or_errored_runs"], 5)   # taxonomy still counts all


class ToolCallValidationTests(unittest.TestCase):
    """P2: validate_case_assertion rejects malformed tool_call assertions at
    manifest-validation time rather than degrading silently in grading."""
    def _validate(self, assertion):
        sb.validate_case_assertion("c1", "a", 0, assertion, Path("."))

    def test_valid_single_selectors_pass(self):
        self._validate({"type": "tool_call", "required_calls": ["Read"]})
        self._validate({"type": "tool_call", "call_set": ["Read", "Grep"]})
        self._validate({"type": "tool_call", "expected_no_call": True, "pattern": "curl"})   # pattern is a modifier, not a 2nd selector
        self._validate({"type": "tool_call", "order": ["Read", "Edit"]})
        self._validate({"type": "tool_call", "tool": "Read"})                                # legacy pattern/count path

    def test_rejects_multiple_selectors(self):
        with self.assertRaises(SystemExit):
            self._validate({"type": "tool_call", "required_calls": ["Read"], "call_set": ["Read"]})
        with self.assertRaises(SystemExit):
            self._validate({"type": "tool_call", "expected_no_call": True, "required_calls": ["Read"]})

    def test_rejects_bad_list_types(self):
        for bad in ("Read", [], [1, 2], ["ok", 3]):
            with self.assertRaises(SystemExit):
                self._validate({"type": "tool_call", "required_calls": bad})

    def test_rejects_non_bool_expected_no_call(self):
        with self.assertRaises(SystemExit):
            self._validate({"type": "tool_call", "expected_no_call": "false"})   # the string footgun
        with self.assertRaises(SystemExit):
            self._validate({"type": "tool_call", "expected_no_call": 1})

    def test_rejects_invalid_regex_in_pattern_or_order(self):
        with self.assertRaises(SystemExit):
            self._validate({"type": "tool_call", "pattern": "["})
        with self.assertRaises(SystemExit):
            self._validate({"type": "tool_call", "order": ["ok", "("]})

    def test_literal_name_selectors_are_not_regex_validated(self):
        # required_calls/call_set are exact tool NAMES, so a regex-special name is fine
        self._validate({"type": "tool_call", "required_calls": ["Read(", "a.b"]})


if __name__ == "__main__":
    unittest.main()
