"""Tests for the ablation value objects — they assert the invariants are now
STRUCTURAL: the bad state is unrepresentable or unreachable, not merely
checked-after-the-fact. Each test below would have to be deleted (not just
fail) to reintroduce the corresponding class of bug.
"""
import unittest

import ablation_model as am
from manifest_contracts import CaseId, CaseKind, ExecutionVariant, RunNumber, Split


class ProvenanceSchemaTests(unittest.TestCase):
    def ident(self):
        return am.TreeIdentity(canonical="C", edited="E")

    def prov(self, **over):
        base = {"id": "no-rp", "mode": "materialized", "population": "answer",
                    "identity": self.ident(), "components": (am.Component("instructions", "section", "skills/x/SKILL.md", {"heading": "## H"}),)}
        base.update(over)
        return am.Provenance(**base)

    def test_cannot_construct_partial_provenance(self):
        # The schema is enforced by the constructor: omitting a field is a TypeError,
        # not a runtime dict that silently lacks a key (the bug that lost components
        # and parent_skill_hash one runner at a time).
        with self.assertRaises(TypeError):
            am.Provenance(id="x", mode="materialized")  # missing population/identity/components

    def test_as_dict_is_the_minimum_schema(self):
        # The persisted key set every runner records and the verifier parses.
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
        with self.assertRaises(ValueError):
            self.prov(population="trigger")                              # population is derived from components
        diff_target = self.prov(components=(am.Component("instructions", "section", "skills/x/SKILL.md", {"heading": "## OTHER"}),))
        self.assertFalse(a.matches(diff_target))                       # different component target

    def test_component_target_is_recursively_immutable(self):
        source = {"heading": "## H", "nested": {"items": ["a"]}}
        component = am.Component("instructions", "section", "skills/x/SKILL.md", source)
        source["nested"]["items"].append("mutated")
        self.assertEqual(component.target["nested"]["items"], ("a",))
        with self.assertRaises(TypeError):
            component.target["nested"]["x"] = 1
        with self.assertRaises((TypeError, ValueError)):
            am.Component("instructions", "section", "s", {"bad": {"set"}})
        for target in ({1: "not a JSON object key"}, {"rate": float("nan")}):
            with self.subTest(target=target), self.assertRaises((TypeError, ValueError)):
                am.Component("instructions", "section", "s", target)

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

    def test_empty_components_cannot_attest_a_materialized_edit(self):
        with self.assertRaises(ValueError):
            am.Provenance.from_dict(dict(self.GOOD, components=[]))

    def test_closed_provenance_vocabularies_and_identifiers(self):
        for mutation in (
            {"id": ""}, {"id": "not a slug"}, {"mode": "imaginary"},
            {"mode": "instruction_simulated"}, {"population": "judge"},
            {"skill_hash": ""}, {"parent_skill_hash": ""},
        ):
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                am.Provenance.from_dict(dict(self.GOOD, **mutation))
        for mutation in (
            {"class": "other"}, {"mechanism": "other"}, {"skill_root": ""},
            {"removed_bytes": -1}, {"removed_bytes": True},
        ):
            component = dict(self.GOOD["components"][0], **mutation)
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                am.Component.from_dict(component)

    def test_materialized_provenance_requires_edit_and_component_population(self):
        with self.assertRaisesRegex(ValueError, "edited tree"):
            am.Provenance.from_dict(dict(self.GOOD, skill_hash="C", parent_skill_hash="C"))
        discovery = {"class": "discovery", "mechanism": "frontmatter_field",
                     "skill_root": "skills/x/SKILL.md", "target": {"field": "description"}}
        with self.assertRaisesRegex(ValueError, "population"):
            am.Provenance.from_dict(dict(self.GOOD, components=[discovery]))
        with self.assertRaisesRegex(ValueError, "mix"):
            am.Provenance.from_dict(dict(self.GOOD, components=[self.GOOD["components"][0], discovery]))
        with self.assertRaisesRegex(ValueError, "only valid for the answer"):
            am.InstructionSimulated.from_dict({"id": "a", "population": "trigger"})

    def test_instruction_simulated_rejects_scalar_regressions_and_untyped_removed_component(self):
        with self.assertRaises(ValueError):
            am.InstructionSimulated.from_dict({"id": "a", "population": "answer", "expected_regressions": "abc"})
        with self.assertRaises(ValueError):
            am.InstructionSimulated.from_dict({"id": "a", "population": "answer", "removed_component": 3})

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
        # Every model-facing method is blind: there is deliberately no API that
        # hands the variant truth to the model.
        arm = am.Arm(variant_truth=self.TRUTH, blind=True)
        self.assertEqual(arm.model_visible_variant(), "with_skill")
        self.assertNotIn("no-rp", arm.upload_token())
        self.assertNotIn("ablation", arm.upload_token())

    def test_non_blind_arm_is_transparent(self):
        arm = am.Arm(variant_truth="with_skill", blind=False)
        self.assertEqual(arm.model_visible_variant(), "with_skill")
        self.assertEqual(arm.upload_token(), "with_skill")

    def test_opaque_tokens_are_deterministic_and_distinct(self):
        a = am.Arm("ablation:a", blind=True).upload_token()
        b = am.Arm("ablation:b", blind=True).upload_token()
        self.assertEqual(a, am.Arm("ablation:a", blind=True).upload_token())   # deterministic
        self.assertNotEqual(a, b)                                              # collision-free per variant


class EvidenceClassTests(unittest.TestCase):
    def test_causal_confirmation_truth_table(self):
        # CONFIRMED_CAUSAL needs verified provenance AND coverage AND an observed,
        # significant regression. Missing provenance or coverage is INDETERMINATE
        # even for a significant drop; an unobserved regression is REFUTED whatever
        # the significance machinery says; an observed but insignificant one is
        # INDETERMINATE (seen, noise not ruled out), never REFUTED.
        confirmed = am.EvidenceClass.CONFIRMED_CAUSAL
        refuted = am.EvidenceClass.REFUTED
        unsure = am.EvidenceClass.INDETERMINATE
        table = [
            # provenance, coverage, observed, significant -> verdict
            (True, True, True, True, confirmed),
            (True, True, True, False, unsure),
            (True, True, False, True, refuted),
            (True, True, False, False, refuted),
            (True, False, True, True, unsure),
            (True, False, True, False, unsure),
            (True, False, False, True, unsure),
            (True, False, False, False, unsure),
            (False, True, True, True, unsure),
            (False, True, True, False, unsure),
            (False, True, False, True, unsure),
            (False, True, False, False, unsure),
            (False, False, True, True, unsure),
            (False, False, True, False, unsure),
            (False, False, False, True, unsure),
            (False, False, False, False, unsure),
        ]
        for provenance, coverage, observed, significant, verdict in table:
            with self.subTest(provenance=provenance, coverage=coverage,
                              observed=observed, significant=significant):
                self.assertIs(am.causal_confirmation(
                    provenance_verified=provenance, has_coverage=coverage,
                    regression_observed=observed, significant=significant), verdict)

    def test_raw_measurement_is_not_a_confirmation(self):
        # The trigger path is a different type; it cannot be read as confirmed.
        self.assertFalse(am.EvidenceClass.RAW_MEASUREMENT.is_confirmation)
        self.assertTrue(am.EvidenceClass.CONFIRMED_CAUSAL.is_confirmation)

    def test_significance_must_be_explicit_and_strictly_typed(self):
        with self.assertRaises(TypeError):
            am.causal_confirmation(provenance_verified=True, has_coverage=True,
                                   regression_observed=True)
        valid = {"provenance_verified": True, "has_coverage": True,
                 "regression_observed": True, "significant": True}
        for name in valid:
            for invalid in (None, 0, 1, "false", [], {}):
                with self.subTest(name=name, invalid=invalid), self.assertRaises(TypeError):
                    am.causal_confirmation(**{**valid, name: invalid})


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

    def test_mean_rate_rejects_invalid_rate_evidence(self):
        for value in (
            True, "1.0", float("nan"), float("inf"), -1e-12, 1.0 + 1e-12,
        ):
            with self.subTest(value=value), self.assertRaisesRegex(
                ValueError, r"finite rate in \[0, 1\] or null"
            ):
                am.ResultSet([{
                    "case_id": "c1",
                    "variant": "with_skill",
                    "objective_pass_rate": value,
                    "missing_output": False,
                    "execution_valid": True,
                }]).mean_rate()

    def test_all_is_the_explicit_escape_hatch(self):
        self.assertEqual(len(am.ResultSet(self.rows()).all), 3)   # raw access is opt-in, not the default


class ExecutionValidTests(unittest.TestCase):
    """execution_valid reads what runners persist: metadata flags and the
    synthetic-failure body a runner writes when it never got a real answer. The
    marker strings are literal on-disk values; changing one would let old
    failure runs grade as genuine answers."""

    def test_infrastructure_failures_are_never_scorable(self):
        rows = [
            # (metadata, output body, valid)
            ({"returncode": 0}, "a real answer", True),
            (None, "answer with no metadata", True),
            ({}, "An answer that quotes [CODEX FAILURE: x] mid-text.", True),
            ({"artifact_contract_version": 1, "artifact_set_complete": True}, "x", True),
            ({"returncode": 1}, "x", False),
            ({"timed_out": True}, "x", False),
            ({"timeout": True}, "x", False),
            ({"timed_out": "yes"}, "x", False),
            ({"provider_response_complete": False}, "x", False),
            ({"artifact_set_complete": False}, "x", False),
            ({"artifact_contract_version": 1}, "x", False),
            ({}, "[CODEX FAILURE: returncode=1]\n\n", False),
            ({}, "[JETTY FAILURE: trajectory failed before producing output]\n", False),
            ({}, "[CLAUDE FAILURE: provider produced no final answer]", False),
            ({}, "[VIBE FAILURE: returncode=127]", False),
            ({}, "[GEMINI FAILURE: returncode=1]", False),
            ({}, "[TIMEOUT: no final assistant message captured]", False),
            ({}, "  \n[CODEX FAILURE: returncode=1]", False),
        ]
        for metadata, text, valid in rows:
            with self.subTest(metadata=metadata, text=text):
                self.assertIs(am.execution_valid(metadata, text), valid)


class PreparedTaskTests(unittest.TestCase):
    """The prepared row is parsed once at the JSONL boundary into typed identity
    values, and it OWNS blinding: the only model-facing variant comes from its
    Arm. Both distinct blinds are honored: the experiment-blind (materialized ->
    present as with_skill) and the path-hygiene blind (any ablation -> opaque
    upload token)."""

    BASE_ROW = {
        "case_id": "c", "split": "tune", "kind": "behavior", "variant": "with_skill",
        "run_number": 1, "skill_name": "s", "repo_root": "/repo",
        "skill_paths": ["skills/s/SKILL.md"], "input_files": [],
        "run_dir": "c/with_skill/run-1", "instruction": "", "prompt": "p", "tags": [],
    }

    def mat_task(self):
        prov = am.Provenance(id="no-rp", mode="materialized", population="answer",
                             identity=am.TreeIdentity(canonical="C", edited="E"),
                             components=(am.Component("instructions", "section", "s", {}),))
        return am.PreparedTask(case_id="c", split="tune", kind="behavior", variant_truth="ablation:no-rp",
                               run_number=1, skill_name="good-pr", repo_root="/r", skill_paths=("/m/SKILL.md",),
                               input_files=(), run_dir="c/ablation:no-rp", instruction="Use the skill under test (good-pr).",
                               prompt="Review.", tags=(), ablation=prov, skill_tree_hash="C")

    def sim_task(self):
        sim = am.InstructionSimulated(id="no-rp", population="answer", removed_component="rp")
        return am.PreparedTask(case_id="c", split="tune", kind="behavior", variant_truth="ablation:no-rp",
                               run_number=1, skill_name="good-pr", repo_root="/r", skill_paths=("/m/SKILL.md",),
                               input_files=(), run_dir="c/ablation:no-rp", instruction="...directive...",
                               prompt="Review.", tags=(), ablation=sim)

    def test_draft_can_be_partial_but_execution_validation_is_strict(self):
        draft = am.PreparedTaskDraft.from_row({"variant": "with_skill", "prompt": "review"})
        self.assertEqual(draft.prompt, "review")
        with self.assertRaises(ValueError):
            draft.validate()

    def test_row_boundary_parses_typed_identity_and_round_trips(self):
        task = am.PreparedTask.from_row(self.BASE_ROW)
        self.assertIsInstance(task.case_id, CaseId)
        self.assertIsInstance(task.split, Split)
        self.assertIsInstance(task.kind, CaseKind)
        self.assertIsInstance(task.variant_truth, ExecutionVariant)
        self.assertIsInstance(task.run_number, RunNumber)
        self.assertEqual(task.harness_record(), self.BASE_ROW)             # wire shape unchanged
        for pt in (self.mat_task(), self.sim_task()):
            back = am.PreparedTask.from_row(pt.harness_record())
            self.assertEqual(back.variant_truth, pt.variant_truth)
            self.assertEqual(type(back.ablation), type(pt.ablation))       # record type survives the round trip
            self.assertEqual(back.is_blind, pt.is_blind)
            self.assertEqual(back.harness_record(), pt.harness_record())   # serialization is stable

    def test_invalid_rows_are_rejected_by_the_guard_that_owns_them(self):
        materialized = self.mat_task().harness_record()
        rejected = [
            (self.BASE_ROW, {"case_id": ""}, "case id must be a non-empty string"),
            (self.BASE_ROW, {"split": "training"}, "split must be one of"),
            (self.BASE_ROW, {"kind": "trigger"}, "answer-population only"),
            (self.BASE_ROW, {"variant": "unknown"}, "not a supported arm"),
            (self.BASE_ROW, {"run_number": 0}, "run number must be positive"),
            (self.BASE_ROW, {"run_number": True}, "run number must be an integer"),
            (self.BASE_ROW, {"run_number": "1"}, "'run_number' must be int"),
            (self.BASE_ROW, {"run_dir": "../escape"}, "run_dir must be a safe non-root relative path"),
            (self.BASE_ROW, {"run_dir": "/absolute"}, "run_dir must be a safe non-root relative path"),
            (self.BASE_ROW, {"run_dir": "."}, "run_dir must be a safe non-root relative path"),
            (self.BASE_ROW, {"skill_paths": "not-a-list"}, "'skill_paths' must be a list of strings"),
            (self.BASE_ROW, {"variant": "without_skill"}, "run_dir arm disagrees with variant"),
            (self.BASE_ROW, {"variant": "without_skill", "run_dir": "c/without_skill/run-1"},
             "without_skill task cannot carry skill paths"),
            (materialized, {"skill_paths": []}, "requires mounted skill paths"),
            (materialized, {"skill_tree_hash": "OTHER"}, "canonical hash must match provenance parent"),
            (materialized, {"skill_tree_hash": None}, "canonical hash must match provenance parent"),
        ]
        with self.assertRaisesRegex(ValueError, "missing run_number"):
            am.PreparedTask.from_row({key: value for key, value in self.BASE_ROW.items() if key != "run_number"})
        for row, mutation, message in rejected:
            with self.subTest(mutation=mutation), self.assertRaisesRegex(ValueError, message):
                am.PreparedTask.from_row({**row, **mutation})

    def test_materialized_arm_presents_as_with_skill(self):
        pt = self.mat_task()
        self.assertTrue(pt.is_materialized_ablation)
        self.assertTrue(pt.is_blind)
        self.assertEqual(pt.model_facing_variant(), "with_skill")             # experiment-blind
        self.assertEqual(pt.harness_record()["variant"], "ablation:no-rp")    # truth on the row

    def test_instruction_simulated_arm_is_transparent(self):
        pt = self.sim_task()
        self.assertFalse(pt.is_materialized_ablation)
        self.assertFalse(pt.is_blind)
        self.assertEqual(pt.model_facing_variant(), "ablation:no-rp")         # model is told what to simulate

    def test_upload_token_is_opaque_for_any_ablation(self):
        for pt in (self.mat_task(), self.sim_task()):
            tok = pt.upload_token()
            self.assertNotIn("no-rp", tok)
            self.assertNotIn("ablation", tok)


if __name__ == "__main__":
    unittest.main()
