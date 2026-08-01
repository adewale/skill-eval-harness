import unittest

import experimental_pairs as ep


class ExperimentalPairKeyTests(unittest.TestCase):
    def test_identity_dimensions_are_required_and_validated(self):
        key = ep.ExperimentalPairKey("case", "model", 2, "answer")
        self.assertIsInstance(key.case_id, ep.CaseId)
        self.assertIsInstance(key.model, ep.ModelId)
        self.assertIsInstance(key.run_number, ep.RunNumber)
        self.assertIs(key.population, ep.ExperimentalPopulation.ANSWER)
        self.assertEqual(key.to_dict(), {
            "case_id": "case", "model": "model", "run_number": 2, "population": "answer",
        })
        invalid = [
            ("", "m", 1, "answer"),
            ("c", "", 1, "answer"),
            ("c", "m", 0, "answer"),
            ("c", "m", True, "answer"),
            ("c", "m", 1, ""),
            ("c", "m", 1, "answr"),
        ]
        for args in invalid:
            with self.subTest(args=args), self.assertRaises(ValueError):
                ep.ExperimentalPairKey(*args)

    def test_row_boundary_does_not_invent_repetition_identity(self):
        with self.assertRaisesRegex(ValueError, "run_number"):
            ep.ExperimentalPairKey.from_row({"case_id": "c"}, population="answer")

    def test_parse_constructs_one_precise_identity_from_wire_values(self):
        key = ep.ExperimentalPairKey.parse("case", None, 3, "judge")
        self.assertEqual(
            key.to_dict(),
            {"case_id": "case", "model": None, "run_number": 3, "population": "judge"},
        )
        self.assertIs(key.population, ep.ExperimentalPopulation.JUDGE)


class PairConstructionTests(unittest.TestCase):
    @staticmethod
    def arm(case="c", model="m", run=1, population="answer", arm="with_skill",
            eligible=True, reason=None):
        key = ep.ExperimentalPairKey(case, model, run, population)
        return ep.ExperimentalArm(key, arm, {"value": arm}, eligible, reason)

    def test_only_exact_identity_match_constructs_a_pair(self):
        dimensions = [
            ("other", "m", 1, "answer"),
            ("c", "other", 1, "answer"),
            ("c", "m", 2, "answer"),
            ("c", "m", 1, "trigger"),
        ]
        for case, model, run, population in dimensions:
            with self.subTest(difference=(case, model, run, population)):
                result = ep.construct_pairs([
                    self.arm(),
                    self.arm(case, model, run, population, "without_skill"),
                ])
                self.assertEqual(len(result.pairs), 0)
                self.assertEqual(len(result.blocked), 2)
        matched = ep.construct_pairs([self.arm(), self.arm(arm="without_skill")])
        self.assertEqual(len(matched.pairs), 1)
        self.assertEqual(len(matched.blocked), 0)

    def test_duplicate_arm_is_rejected_instead_of_overwritten(self):
        with self.assertRaisesRegex(ValueError, "duplicate experimental arm"):
            ep.construct_pairs([self.arm(), self.arm(), self.arm(arm="without_skill")])

    def test_missing_and_ineligible_arms_are_explicitly_blocked(self):
        missing = ep.construct_pairs([self.arm()])
        self.assertEqual(missing.blocked[0].reason, "missing_without_skill")
        ineligible = ep.construct_pairs([
            self.arm(eligible=False, reason="unscorable_arm"),
            self.arm(arm="without_skill"),
        ])
        self.assertEqual(ineligible.blocked[0].reason, "unscorable_arm")
        self.assertEqual(ineligible.diagnostics(), {
            "contrast_id": "skill_presence",
            "eligible_pairs": 0,
            "blocked_pairs": 1,
            "blocked_reason_counts": {"unscorable_arm": 1},
        })

    def test_arm_state_is_correct_by_construction(self):
        key = ep.ExperimentalPairKey("c", None, 1, "answer")
        with self.assertRaises(ValueError):
            ep.ExperimentalArm(key, "with_skill", {}, True, "contradiction")
        with self.assertRaises(ValueError):
            ep.ExperimentalArm(key, "without_skill", {}, False, None)
        with self.assertRaises(ValueError):
            ep.construct_pairs([ep.ExperimentalArm(key, "ablation", {})])
        with self.assertRaises(TypeError):
            ep.ExperimentalArm(None, "with_skill", {})  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            ep.ExperimentalArm(key, "with_skill", {}, eligible=1)  # type: ignore[arg-type]

    def test_rows_reject_duplicate_missing_and_coerced_identity(self):
        row = {"case_id": "c", "model": "m", "run_number": 1, "variant": "with_skill"}
        with self.assertRaises(ValueError):
            ep.pairs_from_rows([row, row], population="answer")
        with self.assertRaisesRegex(ValueError, "run_number"):
            ep.pairs_from_rows([{k: v for k, v in row.items() if k != "run_number"}], population="answer")
        with self.assertRaisesRegex(ValueError, "case_id must be a string"):
            ep.pairs_from_rows([{**row, "case_id": 1}], population="answer")
        with self.assertRaisesRegex(ValueError, "population.*conflicts"):
            ep.pairs_from_rows([{**row, "population": "trigger"}], population="answer")

    def test_declared_contrast_supports_non_presence_coordinates(self):
        contrast = ep.ContrastSpec(
            contrast_id="native-vs-forced",
            treatment_arm=ep.ExperimentalArmId("native"),
            control_arm=ep.ExperimentalArmId("forced"),
            treatment=ep.TreatmentCoordinate((
                ep.FactorCoordinate(ep.ExperimentalFactor.ACTIVATION, "native"),
                ep.FactorCoordinate(ep.ExperimentalFactor.SKILL_SET, "all"),
            )),
            control=ep.TreatmentCoordinate((
                ep.FactorCoordinate(ep.ExperimentalFactor.ACTIVATION, "forced"),
                ep.FactorCoordinate(ep.ExperimentalFactor.SKILL_SET, "all"),
            )),
        )
        key = ep.ExperimentalPairKey("c", "m", 1, "answer")
        construction = ep.construct_pairs([
            ep.ExperimentalArm(key, "native", {"answer": "n"}),
            ep.ExperimentalArm(key, "forced", {"answer": "f"}),
        ], contrast=contrast)

        self.assertEqual(construction.contrast, contrast)
        self.assertEqual(construction.pairs[0].treatment.payload["answer"], "n")
        self.assertEqual(construction.pairs[0].control.payload["answer"], "f")
        with self.assertRaises(AttributeError):
            _ = construction.pairs[0].with_skill

    def test_equivalent_default_contrast_retains_stable_compatibility_identity(self):
        reconstructed = ep.ContrastSpec(
            contrast_id=ep.SKILL_PRESENCE_CONTRAST.contrast_id,
            treatment_arm=ep.SKILL_PRESENCE_CONTRAST.treatment_arm,
            control_arm=ep.SKILL_PRESENCE_CONTRAST.control_arm,
            treatment=ep.SKILL_PRESENCE_CONTRAST.treatment,
            control=ep.SKILL_PRESENCE_CONTRAST.control,
        )
        pair = ep.construct_pairs([
            self.arm(), self.arm(arm="without_skill"),
        ], contrast=reconstructed).pairs[0]

        self.assertEqual(pair.with_skill.arm, "with_skill")
        self.assertEqual(pair.without_skill.arm, "without_skill")

    def test_contrast_identity_survives_blocked_diagnostics(self):
        construction = ep.construct_pairs([self.arm()])

        self.assertEqual(construction.blocked[0].contrast_id, "skill_presence")
        self.assertEqual(construction.blocked[0].to_dict()["contrast_id"], "skill_presence")
        self.assertEqual(construction.diagnostics()["contrast_id"], "skill_presence")

    def test_pair_construction_rejects_directly_assembled_contradictions(self):
        key = ep.ExperimentalPairKey("c", None, 1, "answer")
        pair = ep.construct_pairs([
            ep.ExperimentalArm(key, "with_skill", {}),
            ep.ExperimentalArm(key, "without_skill", {}),
        ]).pairs[0]
        with self.assertRaises(ValueError):
            ep.PairConstruction(
                ep.SKILL_PRESENCE_CONTRAST,
                (pair,),
                (ep.BlockedExperimentalPair(key, "also_blocked", "skill_presence"),),
            )


if __name__ == "__main__":
    unittest.main()
