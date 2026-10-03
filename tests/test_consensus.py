"""One rule folds several verdicts on a judge task, for repeats and for panels."""
import unittest

import judge_verdict as jv
import skill_benchmark as sb


def judge_row(passed, score=None, *, model=None, threshold=None):
    row = {"judge_task_id": "c::with_skill::run-1::q", "passed": passed, "evidence": "e",
           "returncode": 0, "judge_observation_complete": True, "availability": "complete",
           "judge_input_sha256": "sha256:" + "f" * 64, "judge_prompt_sha256": "a" * 64,
           "judge_evidence_mode": "text-only"}
    if score is not None:
        row["score"] = score
    if threshold is not None:
        row["threshold"] = threshold
        row["verdict_kind"] = "scored"
    if model is not None:
        row["judge_model"] = model
    return row


class ResolveConsensusTests(unittest.TestCase):
    def test_the_rule_table(self):
        cases = [
            # passed votes, scores, threshold, quorum -> passed, unresolved
            ([True, True, False], [], None, None, True, False),
            ([True, False, False], [], None, None, False, False),
            ([True, False], [], None, None, False, True),
            ([True, False], [0.9, 0.6], 0.7, None, True, False),
            ([True, False], [0.8, 0.5], 0.7, None, False, False),
            ([True, False], [0.9, 0.6], None, None, False, True),
            ([True, False], [4, 2], 3, None, True, False),        # median == threshold passes
            ([True, False, False], [], None, 1, True, False),
            ([True, True, False], [], None, 3, False, False),     # a quorum overrides the majority
            ([False, False], [], None, 1, False, False),
        ]
        for votes, scores, threshold, quorum, passed, unresolved in cases:
            with self.subTest(votes=votes, scores=scores, threshold=threshold, quorum=quorum):
                consensus = jv.resolve_consensus(votes, scores, threshold=threshold, quorum=quorum)
                self.assertIs(consensus.passed, passed)
                self.assertIs(consensus.unresolved, unresolved)

    def test_agreement_reports_how_the_members_split(self):
        agreement = jv.resolve_consensus([True, False, True], [1.0, 0.0, 1.0]).agreement()
        self.assertEqual(agreement, {"concur": 2, "n": 3, "concur_fraction": 0.6667,
                                     "unanimous": False, "unresolved": False})
        # All members failing is unanimous too.
        self.assertTrue(jv.resolve_consensus([False, False], []).agreement()["unanimous"])

    def test_no_votes_is_an_error(self):
        with self.assertRaises(ValueError):
            jv.resolve_consensus([], [])

    def test_an_unresolved_consensus_cannot_pass(self):
        with self.assertRaises(ValueError):
            jv.Consensus(passed=True, unresolved=True, concur=1, n=2)


class BothMergesShareTheRuleTests(unittest.TestCase):
    def test_repeats_and_a_panel_fold_the_same_votes_the_same_way(self):
        cases = [
            # (passed, score) votes, threshold -> passed, unresolved
            # A tie with no threshold was a silent fail for repeats; now both
            # merges report it as unresolved.
            ([(True, None), (False, None)], None, False, True),
            # Scores without a calibrated threshold must not pass on a default.
            ([(True, 3), (False, 2)], None, False, True),
            # A tie decided by the median score against an explicit threshold.
            ([(True, 0.9), (False, 0.6)], 0.7, True, False),
            ([(True, 1.0), (True, 0.9), (False, 0.0)], 0.7, True, False),
        ]
        for votes, threshold, passed, unresolved in cases:
            with self.subTest(votes=votes, threshold=threshold):
                repeats = sb.merge_repeated_judge_rows(
                    [judge_row(p, s, threshold=threshold) for p, s in votes])
                panel = sb.merge_cross_judge_rows(
                    [judge_row(p, s, threshold=threshold, model=f"m{i}")
                     for i, (p, s) in enumerate(votes)])
                for merged in (repeats, panel):
                    self.assertIs(merged["passed"], passed)
                    self.assertIs(merged["agreement"]["unresolved"], unresolved)
                self.assertEqual(repeats["agreement"], panel["agreement"])


if __name__ == "__main__":
    unittest.main()
