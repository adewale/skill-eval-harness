import json
import unittest

import ablation_model
import skill_benchmark as sb
from manifest_contracts import (
    ABLATION_VARIANT_PREFIX,
    CaseId,
    CaseKind,
    CasePopulation,
    ExecutionVariant,
    ModelId,
    RunNumber,
    Split,
    ablation_id_of,
    is_ablation_variant,
)


class ManifestIdentityContractTests(unittest.TestCase):
    def test_run_identity_scalars_are_validated_and_keep_wire_shape(self):
        identity = {
            "case_id": CaseId.parse("case-1"),
            "model": ModelId.parse("model-a"),
            "run_number": RunNumber.parse(2),
        }
        self.assertEqual(
            json.dumps(identity),
            '{"case_id": "case-1", "model": "model-a", "run_number": 2}',
        )
        for value in (0, -1, True, "1", None):
            with self.subTest(run_number=value), self.assertRaises(ValueError):
                RunNumber.parse(value)

    def test_split_is_closed_and_keeps_wire_shape(self):
        splits = [Split.parse(value) for value in Split.values()]
        self.assertEqual(splits, ["tune", "holdout", "holdback"])
        self.assertEqual(json.dumps({"split": splits[0]}), '{"split": "tune"}')
        with self.assertRaises(ValueError):
            Split.parse("training")
        with self.assertRaises(ValueError):
            Split.parse(None)

    def test_case_kind_owns_answer_vs_trigger_population(self):
        self.assertIs(CaseKind("trigger").population, CasePopulation.TRIGGER)
        self.assertIs(CaseKind("pr-review").population, CasePopulation.ANSWER)
        self.assertIs(CaseKind.parse("behavior").population, CasePopulation.ANSWER)
        with self.assertRaises(ValueError):
            CaseKind.parse(3)

    def test_execution_variant_is_closed_and_decodes_ablation_once(self):
        base = [ExecutionVariant(value) for value in ExecutionVariant.base_values()]
        self.assertEqual(base, ["with_skill", "without_skill", "old_skill"])
        ablation = ExecutionVariant.ablation("no-rp")
        self.assertTrue(ablation.is_ablation)
        self.assertEqual(ablation.ablation_id, "no-rp")
        self.assertEqual(ablation_id_of(ablation), "no-rp")
        self.assertTrue(is_ablation_variant(ablation))
        with self.assertRaises(ValueError):
            ExecutionVariant("control")
        with self.assertRaises(ValueError):
            ExecutionVariant("ablation:")

    def test_ablation_model_reexports_the_canonical_compatibility_helpers(self):
        self.assertEqual(ablation_model.ABLATION_VARIANT_PREFIX, ABLATION_VARIANT_PREFIX)
        self.assertIs(ablation_model.ablation_id_of, ablation_id_of)
        self.assertIs(ablation_model.is_ablation_variant, is_ablation_variant)

    def test_task_variants_returns_typed_values_without_changing_strings(self):
        manifest = {
            "variants": ["with_skill", "without_skill"],
            "old_skill_paths": ["old/SKILL.md"],
            "ablations": [{"id": "no-rp"}],
        }
        variants = sb.task_variants(
            manifest, include_old_skill=True, include_ablations=True
        )
        self.assertTrue(all(isinstance(value, ExecutionVariant) for value in variants))
        self.assertEqual(
            variants,
            ["with_skill", "without_skill", "old_skill", "ablation:no-rp"],
        )

    def test_prepared_task_parses_identity_values_at_the_row_boundary(self):
        row = {
            "case_id": "case-1",
            "split": "tune",
            "kind": "behavior",
            "variant": "with_skill",
            "run_number": 1,
            "skill_name": "example",
            "repo_root": "/repo",
            "skill_paths": ["/repo/SKILL.md"],
            "input_files": [],
            "run_dir": "case-1/with_skill",
            "instruction": "use the skill",
            "prompt": "do the task",
            "tags": [],
        }
        task = ablation_model.PreparedTask.from_row(row)
        self.assertIsInstance(task.split, Split)
        self.assertIsInstance(task.kind, CaseKind)
        self.assertIsInstance(task.variant_truth, ExecutionVariant)
        self.assertEqual(task.harness_record(), row)

    def test_prepared_task_rejects_untyped_identity_values_at_the_boundary(self):
        row = {
            "case_id": "case-1",
            "split": "training",
            "kind": "behavior",
            "variant": "with_skill",
            "run_number": 1,
            "skill_name": "example",
            "repo_root": "/repo",
            "skill_paths": ["/repo/SKILL.md"],
            "input_files": [],
            "run_dir": "case-1/with_skill",
            "instruction": "",
            "prompt": "",
            "tags": [],
        }
        with self.assertRaisesRegex(ValueError, "split must be"):
            ablation_model.PreparedTask.from_row(row)


if __name__ == "__main__":
    unittest.main()
