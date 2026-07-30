"""The central-module ty gate compares identities, never lossy totals."""
import unittest

from scripts.check_ty_regressions import diagnostic_identities, new_diagnostics


def diagnostic(*, rule: str, description: str, line: int) -> dict:
    return {
        "check_name": rule,
        "description": description,
        "location": {"positions": {"begin": {"line": line}}},
    }


class TyRegressionGateTests(unittest.TestCase):
    def test_moved_diagnostic_with_same_source_identity_is_retained(self):
        baseline = diagnostic_identities(
            [diagnostic(rule="r", description="same", line=1)], "value = bad()\n")
        current = diagnostic_identities(
            [diagnostic(rule="r", description="same", line=2)],
            "inserted = True\nvalue = bad()\n",
        )
        self.assertFalse(new_diagnostics(baseline, current))

    def test_equal_counts_cannot_hide_a_substituted_diagnostic(self):
        baseline = diagnostic_identities(
            [diagnostic(rule="old", description="legacy", line=1)], "old = bad()\n")
        current = diagnostic_identities(
            [diagnostic(rule="new", description="regression", line=1)], "new = bad()\n")
        regressions = new_diagnostics(baseline, current)
        self.assertEqual(sum(regressions.values()), 1)

    def test_multiplicity_prevents_duplicate_diagnostics_from_collapsing(self):
        one = diagnostic(rule="r", description="same", line=1)
        baseline = diagnostic_identities([one], "value = bad()\n")
        current = diagnostic_identities([one, one], "value = bad()\n")
        self.assertEqual(sum(new_diagnostics(baseline, current).values()), 1)

    def test_identical_source_in_a_different_scope_cannot_replace_debt(self):
        baseline = diagnostic_identities(
            [diagnostic(rule="r", description="same", line=2)],
            "def legacy():\n    return bad()\n",
        )
        current = diagnostic_identities(
            [diagnostic(rule="r", description="same", line=2)],
            "def replacement():\n    return bad()\n",
        )
        self.assertEqual(sum(new_diagnostics(baseline, current).values()), 1)


if __name__ == "__main__":
    unittest.main()
