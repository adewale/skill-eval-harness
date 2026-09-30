"""A contrast varies one factor; everything it holds fixed must match between arms."""
import unittest

import experimental_pairs as ep


def row(case, variant, *, effort=None, run=1):
    out = {"case_id": case, "variant": variant, "run_number": run}
    if effort is not None:
        out["effort"] = {"requested": effort}
    return out


class HeldFixedTests(unittest.TestCase):
    def pairs(self, rows, contrast=ep.SKILL_PRESENCE_CONTRAST):
        return ep.pairs_from_rows(rows, population=ep.ExperimentalPopulation.ANSWER,
                                  contrast=contrast)

    def test_a_pair_at_different_effort_is_blocked_with_the_factor_named(self):
        cases = [
            ("high", "high", None),
            ("high", "low", "effort_mismatch"),
            ("high", None, "effort_unrecorded_on_one_arm"),
            (None, None, None),  # both predate effort recording: still a pair
        ]
        for left, right, reason in cases:
            with self.subTest(left=left, right=right):
                construction = self.pairs([row("c", "with_skill", effort=left),
                                           row("c", "without_skill", effort=right)])
                if reason is None:
                    self.assertEqual(len(construction.pairs), 1)
                else:
                    self.assertEqual([item.reason for item in construction.blocked], [reason])

    def test_a_held_fixed_factor_cannot_be_named_twice(self):
        with self.assertRaises(ValueError):
            ep.ContrastSpec("x", ep.ExperimentalArmId("a"), ep.ExperimentalArmId("b"),
                            ep.SKILL_PRESENCE_CONTRAST.treatment,
                            ep.SKILL_PRESENCE_CONTRAST.control,
                            held_fixed=(ep.HeldFixedFactor.EFFORT, ep.HeldFixedFactor.EFFORT))


class ArmContrastTests(unittest.TestCase):
    def test_an_ablation_pairs_with_the_full_skill_under_its_own_name(self):
        contrast = ep.ablation_contrast("ablation:no-rp")
        construction = ep.pairs_from_rows(
            [row("c", "with_skill"), row("c", "ablation:no-rp"), row("d", "with_skill")],
            population=ep.ExperimentalPopulation.ANSWER, contrast=contrast)
        self.assertEqual(len(construction.pairs), 1)
        self.assertEqual(construction.pairs[0].control.arm, "ablation:no-rp")
        # A missing ablation arm is named as such, not as a missing without_skill arm.
        self.assertEqual([item.reason for item in construction.blocked], ["missing_ablation:no-rp"])
        self.assertEqual(construction.diagnostics()["contrast_id"], "ablation:no-rp")

    def test_only_an_ablation_arm_makes_an_ablation_contrast(self):
        with self.assertRaises(ValueError):
            ep.ablation_contrast("with_skill")

    def test_the_edit_contrast_pairs_the_current_and_previous_skill(self):
        construction = ep.pairs_from_rows(
            [row("c", "with_skill"), row("c", "old_skill"), row("c", "without_skill")],
            population=ep.ExperimentalPopulation.ANSWER, contrast=ep.EDIT_CONTRAST)
        self.assertEqual(len(construction.pairs), 1)
        self.assertEqual(construction.pairs[0].control.arm, "old_skill")
        with self.assertRaises(AttributeError):
            _ = construction.pairs[0].without_skill


if __name__ == "__main__":
    unittest.main()
