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


if __name__ == "__main__":
    unittest.main()
