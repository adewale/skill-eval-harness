"""The central-module ty gate compares identities, never lossy totals."""
import unittest

from scripts.check_ty_regressions import diagnostic_identities, new_diagnostics


def diagnostic(
    *, rule: str, description: str, line: int,
    fingerprint: str = "stable-fingerprint", path: str = "skill_benchmark.py",
    column: int = 5,
) -> dict:
    return {
        "check_name": rule,
        "description": description,
        "fingerprint": fingerprint,
        "location": {
            "path": path,
            "positions": {"begin": {"line": line, "column": column}},
        },
    }


class TyRegressionGateTests(unittest.TestCase):
    def test_moved_diagnostic_with_same_source_identity_is_retained(self):
        baseline = diagnostic_identities(
            [diagnostic(rule="r", description="same", line=2)],
            "def f():\n    value = bad()\n")
        current = diagnostic_identities(
            [diagnostic(rule="r", description="same", line=3)],
            "def f():\n\n    value = bad()\n",
        )
        self.assertFalse(new_diagnostics(baseline, current))

    def test_unrelated_statement_insertion_does_not_replace_the_occurrence(self):
        baseline = diagnostic_identities(
            [diagnostic(rule="r", description="same", line=2)],
            "def f():\n    return bad()\n",
        )
        current = diagnostic_identities(
            [diagnostic(rule="r", description="same", line=3)],
            "def f():\n    unrelated = True\n    return bad()\n",
        )
        self.assertFalse(new_diagnostics(baseline, current))

    def test_identical_source_moving_to_another_control_flow_branch_is_new(self):
        baseline = diagnostic_identities(
            [diagnostic(rule="r", description="same", line=3)],
            "def f(flag):\n    if flag:\n        return bad()\n",
        )
        current = diagnostic_identities(
            [diagnostic(rule="r", description="same", line=4)],
            "def f(flag):\n    if flag:\n        pass\n    else:\n        return bad()\n",
        )
        self.assertEqual(sum(new_diagnostics(baseline, current).values()), 1)

    def test_equal_counts_cannot_hide_a_substituted_diagnostic(self):
        baseline = diagnostic_identities(
            [diagnostic(
                rule="old", description="legacy", line=1,
                fingerprint="old-fingerprint")],
            "old = bad()\n")
        current = diagnostic_identities(
            [diagnostic(
                rule="new", description="regression", line=1,
                fingerprint="new-fingerprint")],
            "new = bad()\n")
        regressions = new_diagnostics(baseline, current)
        self.assertEqual(sum(regressions.values()), 1)

    def test_multiplicity_prevents_duplicate_diagnostics_from_collapsing(self):
        one = diagnostic(
            rule="r", description="same", line=1, fingerprint="first")
        baseline = diagnostic_identities([one], "value = bad()\n")
        current = diagnostic_identities([
            one,
            diagnostic(
                rule="r", description="same", line=1,
                fingerprint="second"),
        ], "value = bad()\n")
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

    def test_same_source_and_scope_with_a_new_fingerprint_is_a_regression(self):
        source = "def f():\n    bad = source()\n    bad = source()\n"
        baseline = diagnostic_identities(
            [diagnostic(
                rule="r", description="same", line=2,
                fingerprint="original-occurrence")],
            source,
        )
        current = diagnostic_identities(
            [diagnostic(
                rule="r", description="same", line=3,
                fingerprint="original-occurrence")],
            source,
        )
        self.assertEqual(sum(new_diagnostics(baseline, current).values()), 1)

    def test_fingerprint_churn_does_not_reclassify_the_same_occurrence(self):
        source = "def f():\n    return bad()\n"
        baseline = diagnostic_identities(
            [diagnostic(
                rule="r", description="same", line=2,
                fingerprint="base-fingerprint")],
            source,
        )
        current = diagnostic_identities(
            [diagnostic(
                rule="r", description="same", line=2,
                fingerprint="head-fingerprint")],
            source,
        )
        self.assertFalse(new_diagnostics(baseline, current))

    def test_malformed_path_or_duplicate_run_fingerprint_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "path"):
            diagnostic_identities(
                [diagnostic(
                    rule="r", description="same", line=1,
                    path="other.py")],
                "value = bad()\n",
            )
        repeated = diagnostic(
            rule="r", description="same", line=1,
            fingerprint="duplicate")
        with self.assertRaisesRegex(ValueError, "duplicate fingerprint"):
            diagnostic_identities([repeated, repeated], "value = bad()\n")


if __name__ == "__main__":
    unittest.main()
