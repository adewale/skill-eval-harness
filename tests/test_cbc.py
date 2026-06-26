"""Correctness-by-construction tests, written red-green-refactor.

These assert that the LAST runtime-checked bug class — a run claiming
"materialized" without a real, validated materialization — is unrepresentable in
the typed core. Each test fails if you can build the bad state.
"""
import json
import tempfile
import unittest
from pathlib import Path

import ablation_model as am
import skill_benchmark as sb


SKILL = (
    "---\nname: good-pr\ndescription: Review PRs. Use for PRs.\n---\n\n"
    "# G\n\n## Regression-proof requirement\n\nRequire a failing test.\n\n## Severity\n\nPick.\n"
)


def repo(root: Path, ablations):
    rp = root / "repo"
    sd = rp / "skills" / "good-pr"
    sd.mkdir(parents=True)
    (sd / "SKILL.md").write_text(SKILL, encoding="utf-8")
    (rp / "evals").mkdir()
    m = {"version": 1, "skill_name": "good-pr", "skill_paths": ["skills/good-pr/SKILL.md"],
         "variants": ["with_skill", "without_skill"],
         "cases": [{"id": "c", "split": "tune", "prompt": "x", "assertions": [{"name": "a", "type": "contains", "value": "x"}]}],
         "ablations": ablations}
    p = rp / "evals" / "shared-benchmark.json"
    p.write_text(json.dumps(m), encoding="utf-8")
    return p


SECTION_ABL = {"id": "no-rp", "removed_component": "rp", "mechanism": "section",
               "class": "instructions", "target": {"heading": "## Regression-proof requirement"}}


class MaterializedArmTests(unittest.TestCase):
    def arm(self, *, edited="E", blind=True, provenance=True):
        prov = am.Provenance(id="x", mode="materialized", population="answer",
                             identity=am.TreeIdentity(canonical="C", edited=edited),
                             components=(am.Component("instructions", "section", "s", {}),)) if provenance else None
        ident = prov.identity if prov else am.TreeIdentity(canonical="C", edited=edited)
        return am.Arm(variant_truth="ablation:x", blind=blind, identity=ident, provenance=prov)

    def test_materialized_without_an_edit_is_unrepresentable(self):
        # The round-3 lie: "materialized" while the original tree is mounted.
        with self.assertRaises(ValueError):
            am.MaterializedArm(arm=self.arm(edited="C"), dir="/x", skill_files={}, isolation_warnings=())

    def test_materialized_requires_provenance(self):
        with self.assertRaises(ValueError):
            am.MaterializedArm(arm=self.arm(provenance=False), dir="/x", skill_files={}, isolation_warnings=())

    def test_materialized_arm_must_be_blind(self):
        with self.assertRaises(ValueError):
            am.MaterializedArm(arm=self.arm(blind=False), dir="/x", skill_files={}, isolation_warnings=())

    def test_good_materialized_arm_serializes_legacy_dict(self):
        ma = am.MaterializedArm(arm=self.arm(), dir="/d", skill_files={"r": "/d/r/SKILL.md"}, isolation_warnings=())
        d = ma.as_legacy_dict()
        self.assertEqual(d["mode"], "materialized")
        self.assertEqual(d["dir"], "/d")
        self.assertEqual(d["skill_files"], {"r": "/d/r/SKILL.md"})
        self.assertTrue({"id", "mode", "population", "skill_hash", "parent_skill_hash", "components", "dir", "skill_files", "isolation_warnings"}.issubset(d))


class ValidatedAblationTests(unittest.TestCase):
    def test_gate_pile_is_a_constructor(self):
        # An invalid ablation cannot be validated — the gates are the constructor.
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            bad = {"id": "bad", "removed_component": "x", "mechanism": "section", "class": "discovery",
                   "target": {"heading": "## Severity"}}   # section cannot be class discovery
            p = repo(root, [])   # valid manifest; the bad ablation is validated directly below
            manifest = sb.validate_manifest(p)
            repo_root = sb.repo_root_for_manifest(p)
            with self.assertRaises(sb.AblationError):
                sb.ValidatedAblation.validate(repo_root, manifest, bad)

    def test_materialize_only_takes_a_validated_ablation_and_yields_an_arm(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            p = repo(root, [SECTION_ABL])
            manifest = sb.validate_manifest(p)
            repo_root = sb.repo_root_for_manifest(p)
            validated = sb.ValidatedAblation.validate(repo_root, manifest, SECTION_ABL)
            ma = sb.materialize(validated, root / "out")
            self.assertIsInstance(ma, am.MaterializedArm)
            self.assertTrue(ma.arm.identity.is_edited)             # a real edit happened
            self.assertEqual(ma.arm.provenance.mode, "materialized")

    def test_typed_path_and_legacy_facade_agree(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            p = repo(root, [SECTION_ABL])
            manifest = sb.validate_manifest(p)
            repo_root = sb.repo_root_for_manifest(p)
            typed = sb.materialize(sb.ValidatedAblation.validate(repo_root, manifest, SECTION_ABL), root / "a").as_legacy_dict()
            legacy = sb.materialize_ablation(repo_root, manifest, SECTION_ABL, root / "b")
            keys = ("id", "mode", "population", "skill_hash", "parent_skill_hash", "components")
            self.assertEqual({k: typed[k] for k in keys}, {k: legacy[k] for k in keys})


class AblationRecordTests(unittest.TestCase):
    """Move B: 'an ablation record on a row' is a CLOSED set of typed shapes, not an
    ad-hoc dict that each stage hand-builds slightly differently. Materialized is a
    Provenance; instruction-simulated is its sibling InstructionSimulated; there is
    no third shape, so the report's parse is total."""

    def test_record_is_a_closed_discriminated_set(self):
        mat = am.ablation_record_from_dict({"id": "x", "mode": "materialized", "population": "answer",
                                            "skill_hash": "E", "parent_skill_hash": "C", "components": []})
        sim = am.ablation_record_from_dict({"id": "x", "mode": "instruction_simulated", "population": "answer"})
        self.assertIsInstance(mat, am.Provenance)
        self.assertIsInstance(sim, am.InstructionSimulated)
        with self.assertRaises(ValueError):
            am.ablation_record_from_dict({"id": "x", "mode": "make-believe"})   # no third inhabitant

    def test_instruction_simulated_is_not_a_provenance(self):
        sim = am.InstructionSimulated(id="x", population="answer", removed_component="rp",
                                      expected_regressions=("accepts weak tests",))
        self.assertNotIsInstance(sim, am.Provenance)        # cannot be read as a materialization
        d = sim.as_dict()
        self.assertEqual(d["mode"], "instruction_simulated")
        self.assertNotIn("skill_hash", d)                   # no altered tree to attest
        self.assertNotIn("components", d)
        self.assertEqual(am.InstructionSimulated.from_dict(d), sim)   # round-trips

    def test_minimal_instruction_simulated_is_three_keys(self):
        # The prepared-row form is exactly {id, mode, population} — unchanged shape.
        self.assertEqual(am.InstructionSimulated(id="a", population="answer").as_dict(),
                         {"id": "a", "mode": "instruction_simulated", "population": "answer"})


class PreparedTaskTests(unittest.TestCase):
    """Move C: the prepared row OWNS blinding. The only model-facing variant comes
    from its Arm, so a blind arm cannot leak the hypothesis no matter which exporter
    reads it — and the two DISTINCT blinds are both honored: the experiment-blind
    (materialized -> present as with_skill) and the path-hygiene blind (any ablation
    -> opaque upload token)."""

    def mat_row(self):
        prov = am.Provenance(id="no-rp", mode="materialized", population="answer",
                             identity=am.TreeIdentity(canonical="C", edited="E"),
                             components=(am.Component("instructions", "section", "s", {}),))
        return am.PreparedTask(case_id="c", split="tune", kind="behavior", variant_truth="ablation:no-rp",
                               run_number=1, skill_name="good-pr", repo_root="/r", skill_paths=("/m/SKILL.md",),
                               input_files=(), run_dir="c/ablation:no-rp", instruction="Use the skill under test (good-pr).",
                               prompt="Review.", tags=(), ablation=prov, skill_tree_hash="C")

    def sim_row(self):
        sim = am.InstructionSimulated(id="no-rp", population="answer", removed_component="rp")
        return am.PreparedTask(case_id="c", split="tune", kind="behavior", variant_truth="ablation:no-rp",
                               run_number=1, skill_name="good-pr", repo_root="/r", skill_paths=("/m/SKILL.md",),
                               input_files=(), run_dir="c/ablation:no-rp", instruction="...directive...",
                               prompt="Review.", tags=(), ablation=sim)

    def test_materialized_arm_presents_as_with_skill(self):
        pt = self.mat_row()
        self.assertTrue(pt.is_materialized_ablation)
        self.assertTrue(pt.is_blind)
        self.assertEqual(pt.model_facing_variant(), "with_skill")             # experiment-blind
        self.assertEqual(pt.harness_record()["variant"], "ablation:no-rp")    # truth on the row

    def test_instruction_simulated_arm_is_transparent(self):
        pt = self.sim_row()
        self.assertFalse(pt.is_materialized_ablation)
        self.assertFalse(pt.is_blind)
        self.assertEqual(pt.model_facing_variant(), "ablation:no-rp")         # model is told what to simulate

    def test_upload_token_is_opaque_for_any_ablation(self):
        for pt in (self.mat_row(), self.sim_row()):
            tok = pt.upload_token()
            self.assertNotIn("no-rp", tok)
            self.assertNotIn("ablation", tok)

    def test_no_model_facing_method_leaks_truth_for_a_blind_arm(self):
        pt = self.mat_row()
        for out in (pt.model_facing_variant(), pt.upload_token()):
            self.assertNotIn("no-rp", out)
        self.assertIn("no-rp", pt.harness_record()["variant"])               # truth reachable only on the harness side

    def test_round_trips_through_the_row(self):
        for pt in (self.mat_row(), self.sim_row()):
            back = am.PreparedTask.from_row(pt.harness_record())
            self.assertEqual(back.variant_truth, pt.variant_truth)
            self.assertEqual(type(back.ablation), type(pt.ablation))          # record type survives the round trip
            self.assertEqual(back.is_blind, pt.is_blind)
            self.assertEqual(back.harness_record(), pt.harness_record())      # serialization is stable


class MaterializeCarriesTypedArmTests(unittest.TestCase):
    """Move A: materialize_declared_ablations carries MaterializedArm objects, not
    re-parsed dicts, so prepare reads typed provenance instead of indexing string
    keys — the drop-then-reparse that re-created the original bug shape is gone."""

    def test_declared_ablations_are_typed_materialized_arms(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            p = repo(root, [SECTION_ABL])
            manifest = sb.validate_manifest(p)
            repo_root = sb.repo_root_for_manifest(p)
            trees = sb.materialize_declared_ablations(repo_root, manifest, root / "abl")
            self.assertIsInstance(trees["no-rp"], am.MaterializedArm)
            self.assertIsInstance(trees["no-rp"].arm.provenance, am.Provenance)
            self.assertTrue(trees["no-rp"].arm.identity.is_edited)
            self.assertTrue(trees["no-rp"].skill_files)

    def test_prepared_rows_use_the_typed_provenance(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            p = repo(root, [SECTION_ABL])
            manifest = sb.validate_manifest(p)
            rows = sb.prepared_task_rows(p, manifest, include_ablations=True, ablation_dir=root / "abl")
            arow = next(r for r in rows if r["variant"] == "ablation:no-rp")
            wrow = next(r for r in rows if r["variant"] == "with_skill")
            self.assertEqual(set(arow["ablation"]), {"id", "mode", "population", "skill_hash", "parent_skill_hash", "components"})
            self.assertEqual(wrow["skill_tree_hash"], arow["ablation"]["parent_skill_hash"])   # both arms, same revision


class OldSkillParityTests(unittest.TestCase):
    """The old_skill arm must mount the OLD skill under EVERY runner — resolved ONCE
    on the row (prepared_task_rows) and consumed by both Codex and Jetty, the same
    A-shape as MaterializedArm. Before the fix the row carried the CURRENT skill, so
    only Jetty (which re-resolved from the manifest) was right; Codex silently
    measured with_skill mislabeled as old_skill."""

    CUR = "---\nname: good-pr\ndescription: Review PRs. Use for PRs.\n---\n\n# Current\n\nCURRENT-MARKER\n"
    OLD = "---\nname: good-pr\ndescription: Review PRs. Use for PRs.\n---\n\n# Old\n\nOLD-MARKER\n"

    def repo(self, root: Path):
        rp = root / "repo"
        cur = rp / "skills" / "good-pr"; cur.mkdir(parents=True)
        (cur / "SKILL.md").write_text(self.CUR, encoding="utf-8")
        old = rp / "old-skills" / "good-pr"; old.mkdir(parents=True)
        (old / "SKILL.md").write_text(self.OLD, encoding="utf-8")
        (rp / "evals").mkdir()
        m = {"version": 1, "skill_name": "good-pr",
             "skill_paths": ["skills/good-pr/SKILL.md"],
             "old_skill_paths": ["old-skills/good-pr/SKILL.md"],
             "variants": ["with_skill", "without_skill"],
             "cases": [{"id": "c", "split": "tune", "prompt": "x", "assertions": [{"name": "a", "type": "contains", "value": "x"}]}],
             "ablations": []}
        p = rp / "evals" / "shared-benchmark.json"; p.write_text(json.dumps(m), encoding="utf-8")
        return p

    def old_row(self, p):
        manifest = sb.validate_manifest(p)
        rows = sb.prepared_task_rows(p, manifest, include_old_skill=True)
        return manifest, next(r for r in rows if r["variant"] == "old_skill")

    def test_row_skill_paths_point_at_the_old_skill(self):
        with tempfile.TemporaryDirectory() as td:
            p = self.repo(Path(td))
            _, row = self.old_row(p)
            self.assertTrue(all("old-skills" in sp for sp in row["skill_paths"]))   # not the current tree
            self.assertIn("OLD-MARKER", Path(row["skill_paths"][0]).read_text(encoding="utf-8"))

    def test_codex_mounts_the_old_skill_not_the_current(self):
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as ws:
            p = self.repo(Path(td))
            _, row = self.old_row(p)
            skill_rel, _ = sb.codex_skill_workspace(row, Path(ws))
            mounted = (Path(ws) / skill_rel[0]).read_text(encoding="utf-8")
            self.assertIn("OLD-MARKER", mounted)
            self.assertNotIn("CURRENT-MARKER", mounted)   # the silent bug: used to mount the current skill

    def test_jetty_uploads_the_old_skill(self):
        # Guard: build_jetty_payload now consumes the row's resolved paths; it must
        # still upload the OLD files (it was already correct via manifest re-resolution).
        with tempfile.TemporaryDirectory() as td:
            p = self.repo(Path(td))
            manifest, row = self.old_row(p)
            payload = sb.build_jetty_payload(row, manifest, collection="c", task_prefix=None,
                                             agent="claude-code", model="m", model_provider="anthropic", snapshot="s")
            old_files = [f for f in payload["upload_plan"]["files"] if f["role"] == "old_skill"]
            self.assertTrue(old_files)
            self.assertIn("OLD-MARKER", Path(old_files[0]["local_path"]).read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
