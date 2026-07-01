"""Tests for the ablation value objects — they assert the invariants are now
STRUCTURAL: the bad state is unrepresentable or unreachable, not merely
checked-after-the-fact. Each test below would have to be deleted (not just
fail) to reintroduce the corresponding class of bug.
"""
import unittest

import ablation_model as am


class ProvenanceSchemaTests(unittest.TestCase):
    def ident(self):
        return am.TreeIdentity(canonical="C", edited="E")

    def prov(self, **over):
        base = dict(id="no-rp", mode="materialized", population="answer",
                    identity=self.ident(), components=(am.Component("instructions", "section", "skills/x/SKILL.md", {"heading": "## H"}),))
        base.update(over)
        return am.Provenance(**base)

    def test_cannot_construct_partial_provenance(self):
        # The schema is enforced by the constructor: omitting a field is a TypeError,
        # not a runtime dict that silently lacks a key (the bug that lost components
        # and parent_skill_hash one runner at a time).
        with self.assertRaises(TypeError):
            am.Provenance(id="x", mode="materialized")  # missing population/identity/components

    def test_as_dict_is_the_minimum_schema(self):
        d = self.prov().as_dict()
        self.assertEqual(set(d), {"id", "mode", "population", "skill_hash", "parent_skill_hash", "components"})
        self.assertEqual(d["skill_hash"], "E")
        self.assertEqual(d["parent_skill_hash"], "C")
        self.assertEqual(d["components"][0]["class"], "instructions")

    def test_round_trips_through_dict(self):
        p = self.prov()
        self.assertEqual(am.Provenance.from_dict(p.as_dict()).as_dict(), p.as_dict())

    def test_matches_is_exact_identity(self):
        a = self.prov()
        self.assertTrue(a.matches(self.prov()))                         # same identity
        self.assertFalse(a.matches(self.prov(id="other")))             # different id
        self.assertFalse(a.matches(self.prov(population="trigger")))   # different population
        diff_target = self.prov(components=(am.Component("instructions", "section", "skills/x/SKILL.md", {"heading": "## OTHER"}),))
        self.assertFalse(a.matches(diff_target))                       # different component target

    def test_removed_bytes_is_recorded_but_not_part_of_identity(self):
        a = self.prov(components=(am.Component("instructions", "section", "skills/x/SKILL.md", {"heading": "## H"}, removed_bytes=42),))
        self.assertEqual(a.as_dict()["components"][0]["removed_bytes"], 42)   # recorded
        self.assertTrue(a.matches(self.prov()))                              # but ignored for matching


class StrictFromDictTests(unittest.TestCase):
    """from_dict is the constructor at the JSON boundary where RUNNER metadata
    actually returns. It must enforce the same required-field guarantee the direct
    constructor does (test_cannot_construct_partial_provenance) — a missing, null, or
    wrong-typed id/mode/hash/component is rejected at parse, not silently turned into
    a None-filled record the verifier has to catch much later."""

    GOOD = {"id": "x", "mode": "materialized", "population": "answer",
            "skill_hash": "E", "parent_skill_hash": "C",
            "components": [{"class": "instructions", "mechanism": "section",
                            "skill_root": "skills/x/SKILL.md", "target": {"heading": "## H"}}]}

    def test_good_record_round_trips(self):
        p = am.Provenance.from_dict(self.GOOD)
        self.assertEqual((p.id, p.mode, p.population), ("x", "materialized", "answer"))
        self.assertEqual((p.identity.edited, p.identity.canonical), ("E", "C"))
        self.assertEqual(p.components[0].mechanism, "section")

    def test_missing_required_field_raises(self):
        for key in ("id", "mode", "population", "skill_hash", "parent_skill_hash", "components"):
            d = {k: v for k, v in self.GOOD.items() if k != key}
            with self.assertRaises(ValueError, msg=f"missing {key!r} must raise"):
                am.Provenance.from_dict(d)

    def test_null_required_field_raises(self):
        for key in ("id", "mode", "population", "skill_hash", "parent_skill_hash"):
            d = dict(self.GOOD, **{key: None})
            with self.assertRaises(ValueError, msg=f"null {key!r} must raise"):
                am.Provenance.from_dict(d)

    def test_wrong_typed_field_raises(self):
        with self.assertRaises(ValueError):
            am.Provenance.from_dict(dict(self.GOOD, id=123))          # id not a str
        with self.assertRaises(ValueError):
            am.Provenance.from_dict(dict(self.GOOD, components="nope"))  # components not a list

    def test_malformed_component_raises(self):
        bad = dict(self.GOOD, components=[{"mechanism": "section"}])   # missing class/skill_root/target
        with self.assertRaises(ValueError):
            am.Provenance.from_dict(bad)
        with self.assertRaises(ValueError):                            # target wrong type
            am.Component.from_dict({"class": "instructions", "mechanism": "section",
                                    "skill_root": "s", "target": "not-a-dict"})

    def test_empty_components_is_allowed(self):
        # Present and correctly typed; non-emptiness is a semantic concern for the
        # materializer/verifier, not the parse (a recorded record may legitimately
        # carry an empty component list — see the closed-set test in test_cbc).
        self.assertEqual(am.Provenance.from_dict(dict(self.GOOD, components=[])).components, ())

    def test_instruction_simulated_requires_id_and_population(self):
        self.assertEqual(am.InstructionSimulated.from_dict({"id": "a", "population": "answer"}).id, "a")
        for key in ("id", "population"):
            with self.assertRaises(ValueError):
                am.InstructionSimulated.from_dict({k: v for k, v in {"id": "a", "population": "answer"}.items() if k != key})


class TreeIdentityTests(unittest.TestCase):
    def test_same_revision_compares_canonical_only(self):
        base = am.TreeIdentity(canonical="C", edited="E1")
        self.assertTrue(base.same_revision_as(am.TreeIdentity(canonical="C", edited="E2")))   # same parent, different edit
        self.assertFalse(base.same_revision_as(am.TreeIdentity(canonical="OTHER", edited="E1")))
        self.assertFalse(am.TreeIdentity(canonical="", edited="").same_revision_as(am.TreeIdentity(canonical="", edited="")))  # empty != known

    def test_is_edited(self):
        self.assertTrue(am.TreeIdentity("C", "E").is_edited)
        self.assertFalse(am.TreeIdentity("C", "C").is_edited)   # with_skill arm


class ArmBlindingTests(unittest.TestCase):
    TRUTH = "ablation:no-rp"

    def test_blind_arm_never_exposes_truth_to_the_model(self):
        arm = am.Arm(variant_truth=self.TRUTH, blind=True)
        # Every model-facing method is blind...
        self.assertEqual(arm.model_visible_variant(), "with_skill")
        self.assertNotIn("no-rp", arm.upload_token())
        self.assertNotIn("ablation", arm.upload_token())
        # ...while the harness-only record carries the truth.
        self.assertEqual(arm.harness_record()["variant"], self.TRUTH)

    def test_non_blind_arm_is_transparent(self):
        arm = am.Arm(variant_truth="with_skill", blind=False)
        self.assertEqual(arm.model_visible_variant(), "with_skill")
        self.assertEqual(arm.upload_token(), "with_skill")

    def test_blinding_is_structural_no_model_method_returns_the_truth(self):
        # The guarantee: scanning every model-facing method's output, the truth
        # appears in none of them. There is deliberately no API that hands the
        # variant truth to the model, so a leak cannot be added by "forgetting".
        arm = am.Arm(variant_truth=self.TRUTH, blind=True)
        model_facing = [arm.model_visible_variant(), arm.upload_token()]
        for out in model_facing:
            self.assertNotIn("no-rp", out)
        self.assertIn("no-rp", arm.harness_record()["variant"])   # truth is reachable only here

    def test_opaque_tokens_are_deterministic_and_distinct(self):
        a = am.Arm("ablation:a", blind=True).upload_token()
        b = am.Arm("ablation:b", blind=True).upload_token()
        self.assertEqual(a, am.Arm("ablation:a", blind=True).upload_token())   # deterministic
        self.assertNotEqual(a, b)                                              # collision-free per variant


class EvidenceClassTests(unittest.TestCase):
    def test_confirmed_causal_only_reachable_through_the_guard(self):
        # CONFIRMED_CAUSAL requires verified provenance AND coverage AND the observed regression.
        self.assertEqual(am.causal_confirmation(provenance_verified=True, has_coverage=True, regression_observed=True),
                         am.EvidenceClass.CONFIRMED_CAUSAL)
        # Missing any precondition cannot reach a confirmation.
        self.assertEqual(am.causal_confirmation(provenance_verified=False, has_coverage=True, regression_observed=True),
                         am.EvidenceClass.INDETERMINATE)
        self.assertEqual(am.causal_confirmation(provenance_verified=True, has_coverage=False, regression_observed=True),
                         am.EvidenceClass.INDETERMINATE)
        self.assertEqual(am.causal_confirmation(provenance_verified=True, has_coverage=True, regression_observed=False),
                         am.EvidenceClass.REFUTED)

    def test_raw_measurement_is_not_a_confirmation(self):
        # The trigger path is a different type; it cannot be read as confirmed.
        self.assertFalse(am.EvidenceClass.RAW_MEASUREMENT.is_confirmation)
        self.assertTrue(am.EvidenceClass.CONFIRMED_CAUSAL.is_confirmation)


class ResultSetTests(unittest.TestCase):
    def rows(self):
        return [
            {"case_id": "c1", "variant": "with_skill", "objective_pass_rate": 1.0, "missing_output": False, "execution_valid": True},
            {"case_id": "c1", "variant": "with_skill", "objective_pass_rate": 0.0, "missing_output": False, "execution_valid": False},  # infra failure
            {"case_id": "c1", "variant": "with_skill", "objective_pass_rate": 0.0, "missing_output": True},                              # missing output
        ]

    def test_grouping_excludes_non_scorable_by_default(self):
        groups = am.ResultSet(self.rows()).by_case_variant()
        self.assertEqual(len(groups["c1"]["with_skill"]), 1)   # only the one good run

    def test_mean_rate_ignores_non_scorable(self):
        self.assertEqual(am.ResultSet(self.rows()).mean_rate(), 1.0)   # the 0.0 crash/missing do not drag it down

    def test_all_is_the_explicit_escape_hatch(self):
        self.assertEqual(len(am.ResultSet(self.rows()).all), 3)   # raw access is opt-in, not the default


if __name__ == "__main__":
    unittest.main()
