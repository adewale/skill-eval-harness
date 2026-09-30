"""A contrast varies one factor; everything it holds fixed must match between arms."""
import json
import tempfile
import unittest
from pathlib import Path

from helpers import attest_answer_design, demo_manifest, write_demo_manifest

import experimental_pairs as ep
import skill_benchmark as sb


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


class TokenOverheadPairingTests(unittest.TestCase):
    def test_token_overhead_blocks_a_pair_run_at_different_effort(self):
        # token-overhead paired runs with its own loop and never applied the
        # effort check the benchmark applies; both now use one contrast.
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            path = write_demo_manifest(root, demo_manifest())
            runs = root / "runs"
            for variant, effort in (("with_skill", "high"), ("without_skill", "low")):
                base = runs / "case-1" / variant
                base.mkdir(parents=True)
                (base / "output.md").write_text("alpha", encoding="utf-8")
                (base / "metadata.json").write_text(json.dumps({
                    "effort": {"requested": effort, "applied_by": "claude --effort"},
                    "usage_normalized": {"total_tokens": 10, "source": "provider_reported"},
                }), encoding="utf-8")
            attest_answer_design(path, runs)
            report = sb.paired_token_overhead_report(path, runs=runs)
        self.assertEqual(report["pairs"], [])
        self.assertEqual([pair["pair_status"]["reason"] for pair in report["blocked_pairs"]],
                         ["effort_mismatch"])

    def test_every_comparison_names_a_declared_contrast(self):
        self.assertIs(ep.contrast_for("with_skill", "without_skill"), ep.SKILL_PRESENCE_CONTRAST)
        self.assertIs(ep.contrast_for("with_skill", "old_skill"), ep.EDIT_CONTRAST)
        self.assertEqual(ep.contrast_for("with_skill", "ablation:x").contrast_id, "ablation:x")
        with self.assertRaisesRegex(ValueError, "no declared contrast"):
            ep.contrast_for("without_skill", "with_skill")


if __name__ == "__main__":
    unittest.main()
