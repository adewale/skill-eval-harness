"""One vocabulary for problems (findings), one for what fails a command (gate policy).

Flags used to be matched by substring, so renaming one silently turned off
the findings built on it; gates disagreed about incomplete evidence. These
tests pin the typed vocabulary and the fail-closed policy."""
import unittest

import findings as fd
import gate_policy as gp


class CaseFlagTests(unittest.TestCase):
    def test_every_flag_round_trips_through_its_wire_text(self):
        for flag in fd.CaseFlag:
            with self.subTest(flag=flag):
                wire = flag.render("with_skill") if flag.detailed else flag.render()
                self.assertIs(fd.CaseFlag.parse(wire), flag)

    def test_a_flag_is_recognised_by_value_not_by_substring(self):
        # "saturated" inside another flag's text must not read as SATURATED.
        self.assertIsNone(fd.CaseFlag.parse("base-saturated in a note"))
        self.assertIsNone(fd.CaseFlag.parse("no objective lift, probably"))
        self.assertIs(fd.CaseFlag.parse("flaky repeated pass rates: with_skill"), fd.CaseFlag.FLAKY)
        self.assertIsNone(fd.CaseFlag.parse("flaky repeated pass rates"))

    def test_detail_is_required_exactly_where_the_wire_carries_one(self):
        with self.assertRaises(ValueError):
            fd.CaseFlag.FLAKY.render()
        with self.assertRaises(ValueError):
            fd.CaseFlag.SATURATED.render("x")

    def test_in_row_ignores_unknown_and_non_string_flags(self):
        flags = fd.CaseFlag.in_row(["no objective lift", "made up", 3,
                                    "critical-failure: with_skill (a)"])
        self.assertEqual(flags, {fd.CaseFlag.NO_OBJECTIVE_LIFT, fd.CaseFlag.CRITICAL_FAILURE})
        self.assertEqual(fd.CaseFlag.in_row(None), frozenset())


class FindingKindTests(unittest.TestCase):
    def test_an_unregistered_kind_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "unregistered finding kind"):
            fd.Finding("made-up-kind", "message")

    def test_severity_defaults_to_the_kind_and_can_be_overridden(self):
        finding = fd.Finding(fd.FindingKind.FLOOR_EVAL, "case c fails in both arms", {"case_id": "c"})
        self.assertEqual(finding.as_dict(), {
            "kind": "floor-eval", "severity": "recommended",
            "message": "case c fails in both arms", "evidence": {"case_id": "c"}})
        raised = fd.Finding(fd.FindingKind.FLOOR_EVAL, "m", severity="required")
        self.assertEqual(raised.as_dict()["severity"], "required")

    def test_only_eval_and_grader_findings_rate_the_marks(self):
        for kind in fd.FindingKind:
            if kind.mark is not None:
                with self.subTest(kind=kind):
                    self.assertIn(kind.subject, {fd.Subject.EVAL, fd.Subject.GRADER, fd.Subject.RUN})
        self.assertIsNone(fd.FindingKind.NO_LIFT_EVAL.mark)


class EvalHealthTests(unittest.TestCase):
    def test_a_finding_makes_its_mark_a_concern_and_unavailable_is_not_ok(self):
        health = fd.eval_health(
            [{"kind": "floor-eval", "severity": "recommended", "message": "m"},
             {"kind": "no-lift-eval", "severity": "recommended", "message": "m"},
             {"kind": "not-a-kind", "message": "ignored"}],
            observed={fd.EvalMark.HEADROOM: True, fd.EvalMark.GRADER: True},
            notes={fd.EvalMark.REALISTIC: ["activation is forced"]})
        by_id = {entry["id"]: entry for entry in health["marks"]}
        self.assertEqual(by_id["baseline-headroom"]["status"], "concern")
        self.assertEqual(by_id["baseline-headroom"]["finding_kinds"], ["floor-eval"])
        self.assertEqual(by_id["grader-correct"]["status"], "ok")
        self.assertEqual(by_id["noise-below-min-lift"]["status"], "unavailable")
        self.assertEqual(by_id["realistic-cases"]["notes"], ["activation is forced"])
        self.assertEqual([entry["mark"] for entry in health["marks"]], [1, 2, 3, 4, 5])
        self.assertEqual(health["counts"], {"ok": 1, "concern": 1, "unavailable": 3})


class GatePolicyTests(unittest.TestCase):
    def test_incomplete_evidence_fails_closed_even_with_no_findings(self):
        decision = gp.READINESS.decide([], complete=False, incomplete_reason="benchmark partial")
        self.assertTrue(decision.failed)
        self.assertEqual(decision.exit_code, 1)
        self.assertEqual(decision.reasons, ("benchmark partial",))
        self.assertFalse(gp.READINESS.decide([], complete=True).failed)

    def test_a_preset_fails_on_its_kinds_only(self):
        judge = {"kind": "judge-is-model-under-test", "severity": "required", "message": "m"}
        taxonomy = {"kind": "missing-domain-taxonomy", "severity": "recommended", "message": "m"}
        self.assertTrue(gp.SELF_JUDGING.decide([judge]).failed)
        self.assertFalse(gp.SELF_JUDGING.decide([taxonomy]).failed)

    def test_fail_on_reads_kinds_severities_and_presets(self):
        policy = gp.parse_fail_on(["required", "floor-eval,strict-judge"])
        self.assertIn(fd.Severity.REQUIRED, policy.severities)
        self.assertIn(fd.FindingKind.FLOOR_EVAL, policy.kinds)
        self.assertIn(fd.FindingKind.JUDGE_IS_MODEL_UNDER_TEST, policy.kinds)
        required = {"kind": "missing-hidden-splits", "severity": "required", "message": "m"}
        recommended = {"kind": "missing-success-goals", "severity": "recommended", "message": "m"}
        self.assertTrue(policy.decide([required]).failed)
        self.assertFalse(policy.decide([recommended]).failed)

    def test_an_unknown_token_is_an_error_not_a_gate_that_never_fires(self):
        with self.assertRaisesRegex(ValueError, "unknown --fail-on token"):
            gp.parse_fail_on(["florr-eval"])
        with self.assertRaises(ValueError):
            gp.parse_fail_on([" , "])

    def test_every_judge_negative_control_is_gated(self):
        import skill_benchmark as sb
        for name in sb.JUDGE_NEGATIVE_CONTROLS:
            with self.subTest(control=name):
                finding = {"kind": f"passes-{name}-control", "message": "m"}
                self.assertTrue(gp.JUDGE_ROBUSTNESS.decide([finding]).failed)

    def test_severity_on_the_record_beats_the_kind_default(self):
        policy = gp.parse_fail_on(["required"])
        downgraded = {"kind": "missing-hidden-splits", "severity": "recommended", "message": "m"}
        self.assertFalse(policy.decide([downgraded]).failed)


if __name__ == "__main__":
    unittest.main()
