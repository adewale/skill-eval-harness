"""A contrast varies one factor; everything it holds fixed must match between arms."""
import json
import tempfile
import unittest
from pathlib import Path

from helpers import (
    attest_answer_design,
    demo_manifest,
    result_row,
    run_cli,
    write_demo_manifest,
    write_run,
)

import experimental_pairs as ep
import skill_benchmark as sb
from completion_contracts import BACKEND_DEFAULT


def row(case, variant, *, effort=None, run=1):
    """A result row; effort None predates recording, BACKEND_DEFAULT ran at the default."""
    out = {"case_id": case, "variant": variant, "run_number": run}
    if effort == BACKEND_DEFAULT:
        out["effort"] = {"requested": None}
    elif effort is not None:
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
            (BACKEND_DEFAULT, BACKEND_DEFAULT, None),  # both ran at the backend default
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


class EditContrastTests(unittest.TestCase):
    """did-my-skill-edit-regress: the edit's effect from one run, not two."""

    def rows(self, rates):
        out = []
        for index, (current, previous) in enumerate(rates):
            case = f"c{index}"
            out.append(result_row(case, "with_skill", rate=current, run_number=1))
            out.append(result_row(case, "old_skill", rate=previous, run_number=1))
            out.append(result_row(case, "without_skill", rate=0.0, run_number=1))
        return out

    def test_the_edit_is_paired_against_the_previous_revision(self):
        summary = sb.paired_edit_summary(self.rows([(0.0, 1.0)] * 6 + [(1.0, 1.0)]))
        self.assertEqual(summary["contrast_id"], "skill_edit")
        self.assertEqual(summary["availability"], "complete")
        self.assertAlmostEqual(summary["delta"], -6 / 7)
        self.assertTrue(summary["significance"]["significant_at_0_05"])
        self.assertLess(summary["interval"]["upper"], 0)
        self.assertEqual(len(summary["regressed_cases"]), 6)
        self.assertEqual(summary["regressed_cases"][0]["previous"], 1.0)
        # without_skill rows are not part of this contrast.
        self.assertEqual(summary["pairing"]["eligible_pairs"], 7)

    def test_the_edit_noise_check_measures_headroom_on_the_previous_revision(self):
        # Headroom is what the revision being replaced left to gain:
        # previous rates 0.25 x3, 0.5 x4 average 2.75/7, so 1 - 2.75/7 = 4.25/7.
        # (The current rates average 6.5/7, which would leave 0.5/7.)
        summary = sb.paired_edit_summary(self.rows(
            [(1.0, 0.25)] * 3 + [(1.0, 0.5)] * 3 + [(0.5, 0.5)]))
        self.assertAlmostEqual(summary["noise_check"]["headroom"], 4.25 / 7, places=6)

    def test_no_old_skill_arm_means_no_edit_summary(self):
        rows = [row for row in self.rows([(1.0, 0.0)]) if row["variant"] != "old_skill"]
        self.assertIsNone(sb.paired_edit_summary(rows))

    def test_a_missing_previous_arm_is_named(self):
        rows = self.rows([(1.0, 1.0)] * 2)
        rows = [row for row in rows if not (row["case_id"] == "c1" and row["variant"] == "old_skill")]
        rows.append(result_row("c9", "old_skill", rate=1.0, run_number=1))
        summary = sb.paired_edit_summary(rows)
        self.assertEqual(summary["availability"], "partial")
        self.assertEqual(summary["pairing"]["blocked_reason_counts"],
                         {"missing_old_skill": 1, "missing_with_skill": 1})
        # A partial comparison withholds its headline, as paired_summary does.
        self.assertIsNone(summary["delta"])
        self.assertEqual(summary["observed_delta"], 0.0)
        self.assertEqual(summary["significance"]["reason"], "incomplete_pairing")

    def test_benchmark_reports_the_edit_when_the_old_skill_arm_ran(self):
        cases = [{"id": f"c{i}", "split": "tune", "kind": "behavior", "prompt": "Do it.",
                  "assertions": [{"name": "has-alpha", "type": "contains", "value": "alpha"}]}
                 for i in range(2)]
        manifest = demo_manifest(cases=cases, old_skill_paths=["old/SKILL.md"],
                                 optional_variants=["old_skill"])
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            path = write_demo_manifest(root, manifest)
            (root / "repo" / "old").mkdir()
            (root / "repo" / "old" / "SKILL.md").write_text(
                "---\nname: demo\ndescription: Old\n---\n", encoding="utf-8")
            runs = root / "runs"
            for case, current in (("c0", "beta"), ("c1", "alpha")):
                write_run(runs / case / "with_skill", current)
                write_run(runs / case / "old_skill", "alpha")
                write_run(runs / case / "without_skill", "none")
            arms = ["with_skill", "without_skill", "old_skill"]
            attest_answer_design(path, runs, variants=arms)
            edit = sb.build_benchmark_report(path, runs, variants_arg=arms)["paired_edit_summary"]
            plain = sb.build_benchmark_report(path, runs)
        self.assertEqual(edit["delta"], -0.5)
        self.assertEqual([case["case_id"] for case in edit["regressed_cases"]], ["c0"])
        # Without the old arm selected, the report carries no edit block at all.
        self.assertNotIn("paired_edit_summary", plain)

    def test_ungraded_evidence_withholds_the_edit_as_it_withholds_the_lift(self):
        # Seven cases, each with a script oracle that does not run without
        # --allow-scripts: no row is fully graded, so neither comparison may
        # publish a headline built from the contains assertion alone.
        cases = [{"id": f"c{i}", "split": "tune", "kind": "behavior", "prompt": "Do it.",
                  "assertions": [{"name": "has-alpha", "type": "contains", "value": "alpha"},
                                 {"name": "oracle", "type": "script", "command": ["true"]}]}
                 for i in range(7)]
        manifest = demo_manifest(cases=cases, old_skill_paths=["old/SKILL.md"],
                                 optional_variants=["old_skill"])
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            path = write_demo_manifest(root, manifest)
            (root / "repo" / "old").mkdir()
            (root / "repo" / "old" / "SKILL.md").write_text(
                "---\nname: demo\ndescription: Old\n---\n", encoding="utf-8")
            runs = root / "runs"
            for i in range(7):
                write_run(runs / f"c{i}" / "with_skill", "alpha")
                write_run(runs / f"c{i}" / "old_skill", "none")
                write_run(runs / f"c{i}" / "without_skill", "none")
            arms = ["with_skill", "without_skill", "old_skill"]
            attest_answer_design(path, runs, variants=arms)
            out = root / "benchmark.json"
            code, _, stderr = run_cli("benchmark", path, "--runs", runs, *(
                flag for arm in arms for flag in ("--variant", arm)), "--out", out)
            report = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual(code, 0, stderr)
        self.assertEqual(report["incomplete_reasons"], ["grading_evidence_incomplete"])
        lift, edit = report["paired_summary"], report["paired_edit_summary"]
        self.assertIsNone(lift["absolute_delta"])
        self.assertEqual(edit["availability"], "partial")
        self.assertEqual(edit["design_coverage_reason"], "grading_evidence_incomplete")
        self.assertEqual((edit["delta"], edit["observed_delta"]), (None, 1.0))
        self.assertEqual((edit["current_objective_pass_rate"],
                          edit["observed_current_objective_pass_rate"]), (None, 1.0))
        self.assertFalse(edit["significance"]["significant_at_0_05"])
        self.assertEqual(edit["significance"]["reason"], "grading_evidence_incomplete")
        self.assertTrue(edit["observed_significance"]["significant_at_0_05"])


if __name__ == "__main__":
    unittest.main()
