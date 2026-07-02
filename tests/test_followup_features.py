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
        self.assertEqual(rel["by_variant"]["without_skill"]["mean_pass_at_1"], 0.0)
        self.assertEqual(rel["by_variant"]["with_skill"]["all_runs_pass_rate"], 0.0)  # not every run passed


class TwoSamplePermutationTests(unittest.TestCase):
    def test_single_run_per_arm_never_significant(self):
        r = sb.two_sample_permutation_significance([1.0], [0.0])
        self.assertEqual(r["p_value"], 1.0)
        self.assertFalse(r["significant_at_0_05"])

    def test_clean_separation_becomes_significant_by_four_per_arm(self):
        self.assertFalse(sb.two_sample_permutation_significance([1, 1, 1], [0, 0, 0])["significant_at_0_05"])  # p=0.1
        self.assertTrue(sb.two_sample_permutation_significance([1, 1, 1, 1], [0, 0, 0, 0])["significant_at_0_05"])  # p=0.0286

    def test_deterministic_under_sampling(self):
        a, b = [1.0] * 12, [0.0] * 12   # total 24 -> sampled branch, seeded
        self.assertEqual(sb.two_sample_permutation_significance(a, b), sb.two_sample_permutation_significance(a, b))


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

    def test_call_set_multiset_rejects_unexpected(self):
        base = self._events(["Read", "Grep"])
        ok = sb.assertion_result({"type": "tool_call", "call_set": ["Read", "Grep"]}, "t", base / "output.md", run_base=base)
        self.assertTrue(ok["passed"])
        extra = sb.assertion_result({"type": "tool_call", "call_set": ["Read"]}, "t", base / "output.md", run_base=base)
        self.assertFalse(extra["passed"])   # Grep is unexpected


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


if __name__ == "__main__":
    unittest.main()
