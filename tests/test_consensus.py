"""One rule folds several verdicts on a judge task, for repeats and for panels."""
import unittest

import judge_verdict as jv


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
            ([True, False, False], [], None, 1, True, False),
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

    def test_no_votes_is_an_error(self):
        with self.assertRaises(ValueError):
            jv.resolve_consensus([], [])

    def test_an_unresolved_consensus_cannot_pass(self):
        with self.assertRaises(ValueError):
            jv.Consensus(passed=True, unresolved=True, concur=1, n=2)


if __name__ == "__main__":
    unittest.main()
