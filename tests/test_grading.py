"""Grading: assertion families, severity/veto, dependencies, multi-turn, tool-call taxonomy.

Classes moved verbatim from the PR-named test files (test_audit_fixes,
test_roadmap_features, test_followup_features, test_external_review_gaps,
test_cbc) and test_skill_benchmark, which accreted by merge rather than by
subject; docstrings citing finding/roadmap ids are preserved.
"""
import argparse
import contextlib
import io
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import skill_benchmark as sb
import run_pi_trigger_eval as tr
import ablation_model as am
from helpers import (
    CODEX_CRASH_OUTPUT as CRASH,
    CONTAINS_APPROVED_CASE as CASE,
    demo_manifest as base_manifest,
    good_pr_manifest as _manifest,
    load_example_module,
    make_eval_repo,
    report_fixture,
    write_demo_manifest as write_manifest,
    write_good_pr_skill as _skill,
    write_run,
)

ROOT = Path(__file__).resolve().parents[1]


class GoldenOutputAssertionTests(unittest.TestCase):
    """1.6 — golden_output: reference-file equality with explicit normalization."""

    def grade_one(self, assertion: dict, output: str, reference: str | None, tmp: Path) -> dict:
        manifest_dir = tmp / "manifest"
        manifest_dir.mkdir(parents=True, exist_ok=True)
        if reference is not None:
            ref = manifest_dir / "golden" / "expected.md"
            ref.parent.mkdir(parents=True, exist_ok=True)
            ref.write_text(reference, encoding="utf-8")
        run = tmp / "run"
        run.mkdir(exist_ok=True)
        (run / "output.md").write_text(output, encoding="utf-8")
        return sb.assertion_result(assertion, output, run / "output.md", run_base=run, manifest_dir=manifest_dir)

    def test_exact_match_passes(self):
        with tempfile.TemporaryDirectory() as td:
            r = self.grade_one({"type": "golden_output", "reference": "golden/expected.md"}, "a\nb\n", "a\nb\n", Path(td))
        self.assertTrue(r["passed"])

    def test_normalized_text_match_passes_where_exact_fails(self):
        with tempfile.TemporaryDirectory() as td:
            exact = self.grade_one({"type": "golden_output", "reference": "golden/expected.md"}, "a   b", "a b\n", Path(td))
            normalized = self.grade_one({"type": "golden_output", "reference": "golden/expected.md", "normalize": "text"}, "a   b", "a b\n", Path(td))
        self.assertFalse(exact["passed"])
        self.assertTrue(normalized["passed"])

    def test_trim_normalization(self):
        with tempfile.TemporaryDirectory() as td:
            r = self.grade_one({"type": "golden_output", "reference": "golden/expected.md", "normalize": "trim"}, "\n\na b\n\n", "a b", Path(td))
        self.assertTrue(r["passed"])

    def test_mismatch_evidence_contains_unified_diff(self):
        with tempfile.TemporaryDirectory() as td:
            r = self.grade_one({"type": "golden_output", "reference": "golden/expected.md"}, "alpha gamma\n", "alpha beta\n", Path(td))
        self.assertFalse(r["passed"])
        self.assertIn("reference/golden/expected.md", r["evidence"])
        self.assertIn("-alpha beta", r["evidence"])
        self.assertIn("+alpha gamma", r["evidence"])

    def test_named_artifact_comparison(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            manifest_dir = tmp / "manifest"
            (manifest_dir / "golden").mkdir(parents=True)
            (manifest_dir / "golden" / "expected.json").write_text('{"a": 1}', encoding="utf-8")
            run = tmp / "run"
            (run / "outputs").mkdir(parents=True)
            (run / "output.md").write_text("see artifact", encoding="utf-8")
            (run / "outputs" / "result.json").write_text('{"a": 1}', encoding="utf-8")
            r = sb.assertion_result(
                {"type": "golden_output", "reference": "golden/expected.json", "artifact": "outputs/result.json"},
                "see artifact", run / "output.md", run_base=run, manifest_dir=manifest_dir)
        self.assertTrue(r["passed"], r["evidence"])

    def test_missing_reference_fails_closed_and_validates(self):
        with tempfile.TemporaryDirectory() as td:
            r = self.grade_one({"type": "golden_output", "reference": "golden/expected.md"}, "a", None, Path(td))
            self.assertFalse(r["passed"])
            self.assertIn("missing reference", r["evidence"])
            manifest = base_manifest()
            manifest["cases"][0]["assertions"] = [{"type": "golden_output", "reference": "golden/expected.md"}]
            path = write_manifest(Path(td), manifest)
            validated = sb.validate_manifest(path)
            self.assertEqual(validated["skill_name"], "demo")


class GradedScoringSeverityTests(unittest.TestCase):
    """2.2 — graded scoring, three-tier severity, veto, and statistical lift."""

    def grade(self, case: dict, output: str, judge_results: dict | None = None, strict: bool = False) -> dict:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            (base / "output.md").write_text(output, encoding="utf-8")
            result, _ = sb.grade_case_variant(
                case, "with_skill", output, base / "output.md", {},
                run_base=base, judge_results=judge_results or {}, strict=strict)
        return result

    def behavior_case(self, assertions: list[dict], **extra) -> dict:
        return {"id": "case-x", "split": "tune", "kind": "behavior", "prompt": "p", "assertions": assertions, **extra}

    def test_default_severities_keep_binary_behavior(self):
        case = self.behavior_case([
            {"name": "a", "type": "contains", "value": "alpha"},
            {"name": "b", "type": "contains", "value": "beta"},
        ])
        result = self.grade(case, "alpha only")
        self.assertEqual(result["objective_total"], 2)
        self.assertEqual(result["objective_pass_rate"], 0.5)
        self.assertFalse(result["vetoed"])
        self.assertEqual([a["severity"] for a in result["assertions"]], ["gate", "gate"])

    def test_soft_assertion_leaves_pass_rate_and_fills_scored_bucket(self):
        case = self.behavior_case([
            {"name": "hard", "type": "contains", "value": "alpha"},
            {"name": "nice", "type": "contains", "value": "beta", "severity": "soft"},
        ])
        result = self.grade(case, "alpha only")
        self.assertEqual(result["objective_total"], 1)   # soft left the denominator
        self.assertEqual(result["objective_pass_rate"], 1.0)
        self.assertEqual(result["soft_total"], 1)
        self.assertEqual(result["graded_score"], 0.0)    # the miss shows up as a low score

    def test_strict_promotes_soft_to_gate(self):
        case = self.behavior_case([
            {"name": "hard", "type": "contains", "value": "alpha"},
            {"name": "nice", "type": "contains", "value": "beta", "severity": "soft"},
        ])
        result = self.grade(case, "alpha only", strict=True)
        self.assertEqual(result["objective_total"], 2)
        self.assertEqual(result["objective_pass_rate"], 0.5)

    def test_critical_failure_vetoes_case_and_withholds_graded_score(self):
        case = self.behavior_case([
            {"name": "no-stray-writes", "type": "excludes_any", "values": ["WROTE OUTSIDE RESULTS"], "severity": "critical"},
            {"name": "drama", "type": "contains", "value": "drama", "severity": "soft"},
            {"name": "core", "type": "contains", "value": "alpha"},
        ])
        result = self.grade(case, "alpha drama WROTE OUTSIDE RESULTS")
        self.assertTrue(result["vetoed"])
        self.assertEqual(result["critical_failures"], ["no-stray-writes"])
        self.assertEqual(result["objective_pass_rate"], 0.0)   # veto, despite core+drama passing
        self.assertEqual(result["combined_pass_rate"], 0.0)
        self.assertIsNone(result["graded_score"])              # no graded mean absorbs a catastrophe

    def test_passing_critical_assertion_counts_normally(self):
        case = self.behavior_case([
            {"name": "no-stray-writes", "type": "excludes_any", "values": ["WROTE OUTSIDE RESULTS"], "severity": "critical"},
            {"name": "core", "type": "contains", "value": "alpha"},
        ])
        result = self.grade(case, "alpha, clean run")
        self.assertFalse(result["vetoed"])
        self.assertEqual(result["objective_pass_rate"], 1.0)

    def test_at_least_floor_decides_scored_assertion(self):
        case = self.behavior_case([{"name": "scored", "type": "contains", "value": "alpha", "atLeast": 0.5}])
        result = self.grade(case, "no match")
        self.assertEqual(result["assertions"][0]["severity"], "soft")   # atLeast implies soft
        self.assertFalse(result["assertions"][0]["passed"])             # 0.0 < 0.5

    def test_graded_dimensions_judge_merge(self):
        assertion = {
            "name": "poster-quality", "type": "judge",
            "graded_dimensions": [
                {"name": "drama", "scale": "1-5", "rubric": "5 = one dominant element; 1 = uniform grid"},
                {"name": "hierarchy", "scale": "1-5", "rubric": "5 = obvious reading order; 1 = flat"},
            ],
        }
        case = self.behavior_case([assertion])
        jid = sb.judge_task_id("case-x", "with_skill", 1, assertion)
        judged = {jid: {"judge_task_id": jid, "dimension_scores": {"drama": 5, "hierarchy": 4}, "rationale": "strong"}}
        result = self.grade(case, "poster text", judge_results=judged)
        entry = result["qualitative_assertions"][0]
        self.assertEqual(entry["dimension_scores"], {"drama": 5.0, "hierarchy": 4.0})
        self.assertAlmostEqual(entry["score"], 0.875)   # mean of (5→1.0, 4→0.75)
        self.assertTrue(entry["passed"])                # >= default threshold 4 (0.75)
        self.assertIn("dimension scores", entry["evidence"])
        self.assertEqual(result["graded_score"], 0.875)

    def test_graded_dimensions_below_threshold_fail(self):
        assertion = {"name": "q", "type": "judge", "graded_dimensions": [{"name": "d", "rubric": "anchored"}]}
        case = self.behavior_case([assertion])
        jid = sb.judge_task_id("case-x", "with_skill", 1, assertion)
        judged = {jid: {"judge_task_id": jid, "dimension_scores": {"d": 2}}}
        result = self.grade(case, "text", judge_results=judged)
        self.assertFalse(result["qualitative_assertions"][0]["passed"])

    def test_dynamic_rubric_minimum_criteria_cutoff(self):
        assertion = {"name": "dyn", "type": "judge", "dynamic_rubric": {"instruction": "draft criteria", "minimum_criteria": 3}}
        case = self.behavior_case([assertion])
        jid = sb.judge_task_id("case-x", "with_skill", 1, assertion)
        met3 = {jid: {"criteria": [{"name": "a", "met": True}, {"name": "b", "met": True}, {"name": "c", "met": True}, {"name": "d", "met": False}]}}
        met2 = {jid: {"criteria": [{"name": "a", "met": True}, {"name": "b", "met": True}, {"name": "c", "met": False}, {"name": "d", "met": False}]}}
        self.assertTrue(self.grade(case, "t", judge_results=met3)["qualitative_assertions"][0]["passed"])
        self.assertFalse(self.grade(case, "t", judge_results=met2)["qualitative_assertions"][0]["passed"])

    def test_reference_floor_flags_low_dimension(self):
        assertion = {"name": "q", "type": "judge", "graded_dimensions": [{"name": "drama", "rubric": "anchored"}, {"name": "craft", "rubric": "anchored"}]}
        case = self.behavior_case([assertion], reference_graded_score=4)
        jid = sb.judge_task_id("case-x", "with_skill", 1, assertion)
        judged = {jid: {"dimension_scores": {"drama": 5, "craft": 2}}}
        result = self.grade(case, "t", judge_results=judged)
        self.assertIn("q:craft", result["below_reference_floor"])

    def test_sign_flip_significance_known_pairs(self):
        significant = sb.sign_flip_significance([1.0] * 8)
        flat = sb.sign_flip_significance([0.0] * 8)
        mixed = sb.sign_flip_significance([0.5, -0.5, 0.25, -0.25])
        self.assertEqual(significant["method"], "sign-flip-exact")
        self.assertLessEqual(significant["p_value"], 0.05)
        self.assertTrue(significant["significant_at_0_05"])
        self.assertEqual(flat["p_value"], 1.0)
        self.assertGreater(mixed["p_value"], 0.05)

    def test_sign_flip_sampled_is_deterministic(self):
        deltas = [0.1 * (1 if i % 3 else -1) for i in range(20)]
        self.assertEqual(sb.sign_flip_significance(deltas), sb.sign_flip_significance(deltas))
        self.assertEqual(sb.sign_flip_significance(deltas)["method"], "sign-flip-sampled")

    def test_paired_summary_carries_significance_and_graded_channel(self):
        results = []
        for case_id in ["c1", "c2", "c3"]:
            for variant, rate, graded in [("with_skill", 1.0, 0.9), ("without_skill", 0.0, 0.3)]:
                results.append({
                    "case_id": case_id, "variant": variant, "run_number": 1, "missing_output": False,
                    "execution_valid": True, "objective_pass_rate": rate, "graded_score": graded, "metadata": {},
                })
        paired = sb.build_paired_summary(results)
        self.assertEqual(paired["absolute_delta"], 1.0)
        self.assertIn("significance", paired)
        self.assertEqual(paired["significance"]["n"], 3)
        self.assertAlmostEqual(paired["graded"]["delta"], 0.6)
        self.assertIn("significance", paired["graded"])

    def test_validation_rejects_bad_shapes(self):
        bad_shapes = [
            {"assertions": [{"type": "contains", "value": "x", "severity": "fatal"}]},
            {"assertions": [{"type": "judge", "graded_dimensions": []}]},
            {"assertions": [{"type": "judge", "graded_dimensions": [{"name": "d"}]}]},
            {"assertions": [{"type": "judge", "dynamic_rubric": {"minimum_criteria": 3}}]},
            {"assertions": [{"type": "contains", "value": "x"}], "reference_score": 2},
        ]
        for shape in bad_shapes:
            manifest = base_manifest()
            manifest["cases"][0].update(shape)
            with tempfile.TemporaryDirectory() as td:
                path = write_manifest(Path(td), manifest)
                with self.assertRaises(SystemExit, msg=json.dumps(shape)):
                    sb.validate_manifest(path)

    def test_unchanged_binary_manifest_grades_identically(self):
        # The regression the spec requires: a version-1 manifest with no
        # severity/score fields must produce the same pass rates as before 2.2.
        case = self.behavior_case([
            {"name": "a", "type": "contains", "value": "alpha"},
            {"name": "q", "type": "judge", "rubric": ["complete"]},
        ])
        result = self.grade(case, "alpha")
        self.assertEqual(result["objective_passed"], 1)
        self.assertEqual(result["objective_total"], 1)
        self.assertEqual(result["objective_pass_rate"], 1.0)
        self.assertEqual(result["combined_pass_rate"], 1.0)
        self.assertEqual(result["deferred_judge_tasks"], 1)


class SimilarityScorerTests(unittest.TestCase):
    """1.4 — deterministic difflib similarity with a threshold and a score."""

    def result(self, output: str, threshold: float | None = None) -> dict:
        assertion = {"type": "similarity", "expected": "alpha beta gamma"}
        if threshold is not None:
            assertion["threshold"] = threshold
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            (base / "output.md").write_text(output, encoding="utf-8")
            return sb.assertion_result(assertion, output, base / "output.md", run_base=base)

    def test_identical_scores_one(self):
        r = self.result("alpha beta gamma")
        self.assertTrue(r["passed"])
        self.assertEqual(r["score"], 1.0)

    def test_threshold_boundaries(self):
        exact = self.result("alpha beta gamma", threshold=1.0)
        self.assertTrue(exact["passed"])
        near = self.result("alpha beta gamma!", threshold=1.0)
        self.assertFalse(near["passed"])
        self.assertGreater(near["score"], 0.9)
        loose = self.result("alpha beta gamma!", threshold=0.9)
        self.assertTrue(loose["passed"])

    def test_default_severity_is_soft(self):
        self.assertEqual(sb.assertion_severity({"type": "similarity"}), "soft")


class GradedScriptOracleTests(unittest.TestCase):
    """1.8 — a script oracle may print {"score", "max_score"}; exit code still
    decides passed, the score only feeds the graded channel."""

    def run_script(self, code: str) -> dict:
        assertion = {"type": "script", "command": ["python3", "-c", code]}
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            (base / "output.md").write_text("x", encoding="utf-8")
            return sb.assertion_result(assertion, "x", base / "output.md", run_base=base, allow_scripts=True, manifest_dir=base)

    def test_score_line_is_parsed_and_normalized(self):
        r = self.run_script("print('{\"score\": 6, \"max_score\": 7}')")
        self.assertTrue(r["passed"])
        self.assertAlmostEqual(r["score"], 6 / 7, places=4)

    def test_no_score_line_stays_binary(self):
        r = self.run_script("print('all good')")
        self.assertTrue(r["passed"])
        self.assertEqual(r["score"], 1.0)   # binary mirror of passed

    def test_malformed_score_line_falls_back_to_exit_code(self):
        r = self.run_script("print('{\"score\": \"six\"}')")
        self.assertTrue(r["passed"])
        self.assertEqual(r["score"], 1.0)

    def test_failing_exit_code_beats_perfect_score(self):
        r = self.run_script("print('{\"score\": 7, \"max_score\": 7}'); raise SystemExit(1)")
        self.assertFalse(r["passed"])
        self.assertEqual(r["score"], 1.0)   # graded value preserved, passed decided by exit

    def test_parse_helper_edge_cases(self):
        self.assertIsNone(sb.parse_script_score_line(""))
        self.assertIsNone(sb.parse_script_score_line("not json"))
        self.assertEqual(sb.parse_script_score_line('{"score": 0.5}'), 0.5)
        self.assertEqual(sb.parse_script_score_line('{"score": 9, "max_score": 3}'), 1.0)  # clamped


class OracleTierTests(unittest.TestCase):
    """1.7 — oracle-strength labeling, report share, and the weak-only warning."""

    def test_tier_defaults_by_type(self):
        self.assertEqual(sb.oracle_tier({"type": "contains"}), "strong")
        self.assertEqual(sb.oracle_tier({"type": "command_ran"}), "strong")
        self.assertEqual(sb.oracle_tier({"type": "script"}), "demo")
        self.assertEqual(sb.oracle_tier({"type": "judge"}), "live")
        self.assertEqual(sb.oracle_tier({"type": "factuality"}), "live")
        self.assertEqual(sb.oracle_tier({"type": "script", "oracle": "strong"}), "strong")   # rendered-artifact oracle

    def test_invalid_tier_dies(self):
        manifest = base_manifest()
        manifest["cases"][0]["assertions"][0]["oracle"] = "mighty"
        with tempfile.TemporaryDirectory() as td:
            path = write_manifest(Path(td), manifest)
            with self.assertRaises(SystemExit):
                sb.validate_manifest(path)

    def test_report_carries_strong_pass_share(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            manifest = base_manifest()
            manifest["cases"][0]["assertions"] = [
                {"name": "strong-check", "type": "contains", "value": "alpha"},
                {"name": "demo-check", "type": "script", "command": ["python3", "-c", "raise SystemExit(0)"]},
            ]
            path = write_manifest(root, manifest)
            runs = root / "runs"
            for variant in ["with_skill", "without_skill"]:
                base = runs / "case-1" / variant
                base.mkdir(parents=True)
                (base / "output.md").write_text("alpha", encoding="utf-8")
            report = sb.build_benchmark_report(path, runs, allow_scripts=True)
        strength = report["oracle_strength"]["case-1"]
        self.assertEqual(strength["strong_pass_share"], 0.5)
        self.assertEqual(strength["passed_by_tier"], {"demo": 2, "strong": 2})

    def test_weak_oracle_only_audit_finding(self):
        with tempfile.TemporaryDirectory() as td:
            manifest = base_manifest()
            manifest["cases"][0]["assertions"] = [{"type": "judge", "rubric": ["good"]}]
            path = write_manifest(Path(td), manifest)
            report = sb.audit_manifest_report(path)
            kinds = [f["kind"] for f in report["findings"]]
            self.assertIn("weak-oracle-only", kinds)
            manifest["cases"][0]["assertions"].append({"type": "contains", "value": "alpha"})
            path = write_manifest(Path(td), manifest)
            report = sb.audit_manifest_report(path)
            kinds = [f["kind"] for f in report["findings"]]
            self.assertNotIn("weak-oracle-only", kinds)


class EmbeddingSimilarityTests(unittest.TestCase):
    """4.1 — embedding-backed similarity, strictly opt-in via --embed-cmd."""

    IDENTICAL_CMD = "python3 -c \"import sys,json; json.load(sys.stdin); print(json.dumps({'embeddings': [[1.0, 0.0], [1.0, 0.0]]}))\""
    ORTHOGONAL_CMD = "python3 -c \"import sys,json; json.load(sys.stdin); print(json.dumps({'embeddings': [[1.0, 0.0], [0.0, 1.0]]}))\""

    def result(self, embed_cmd: str | None, cmd_threshold: float = 0.8) -> dict:
        assertion = {"type": "similarity", "mode": "embedding", "expected": "target text", "threshold": cmd_threshold}
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            (base / "output.md").write_text("candidate text", encoding="utf-8")
            return sb.assertion_result(assertion, "candidate text", base / "output.md", run_base=base, embed_cmd=embed_cmd)

    def test_skips_without_opt_in(self):
        r = self.result(None)
        self.assertFalse(r["passed"])
        self.assertIn("--embed-cmd", r["evidence"])

    def test_mocked_identical_embeddings_pass(self):
        r = self.result(self.IDENTICAL_CMD)
        self.assertTrue(r["passed"], r["evidence"])
        self.assertEqual(r["score"], 1.0)

    def test_mocked_orthogonal_embeddings_fail(self):
        r = self.result(self.ORTHOGONAL_CMD)
        self.assertFalse(r["passed"])
        self.assertEqual(r["score"], 0.0)

    def test_malformed_embedder_fails_closed(self):
        r = self.result("python3 -c \"print('not json at all')\"")
        self.assertFalse(r["passed"])
        self.assertIn("JSON", r["evidence"])

    def test_cosine_similarity(self):
        self.assertAlmostEqual(sb.cosine_similarity([1, 0], [1, 0]), 1.0)
        self.assertAlmostEqual(sb.cosine_similarity([1, 0], [0, 1]), 0.0)
        self.assertEqual(sb.cosine_similarity([0, 0], [1, 1]), 0.0)

    def test_invalid_mode_dies(self):
        manifest = base_manifest()
        manifest["cases"][0]["assertions"] = [{"type": "similarity", "expected": "x", "mode": "vibes"}]
        with tempfile.TemporaryDirectory() as td:
            path = write_manifest(Path(td), manifest)
            with self.assertRaises(SystemExit):
                sb.validate_manifest(path)


class MultiTurnCaseTests(unittest.TestCase):
    """3.1 — scripted send/respond sequences with per-turn grading."""

    def multi_turn_case(self) -> dict:
        return {
            "id": "conv-1", "split": "tune", "kind": "behavior",
            "turns": [
                {"prompt": "Ask a clarifying question about scope.",
                 "assertions": [{"name": "asks-question", "type": "contains", "value": "?"}]},
                {"prompt": "Now give the final plan mentioning alpha.",
                 "assertions": [{"name": "mentions-alpha", "type": "contains", "value": "alpha"}]},
            ],
            "assertions": [{"name": "final-has-plan", "type": "contains", "value": "plan"}],
        }

    def write_turn_run(self, base: Path, turn_texts: list[str]) -> None:
        for n, turn_text in enumerate(turn_texts, 1):
            turn_dir = base / f"turn-{n}"
            turn_dir.mkdir(parents=True, exist_ok=True)
            (turn_dir / "output.md").write_text(turn_text, encoding="utf-8")

    def test_per_turn_grading_and_aggregate(self):
        case = self.multi_turn_case()
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            self.write_turn_run(base, ["What is the scope?", "The plan: use alpha."])
            result, _ = sb.grade_case_variant(case, "with_skill", None, base / "output.md", {}, run_base=base)
        self.assertFalse(result["missing_output"])   # final turn stands in for output.md
        self.assertEqual(result["objective_total"], 3)
        self.assertEqual(result["objective_pass_rate"], 1.0)
        self.assertEqual(result["turns"], [
            {"turn": 1, "missing_output": False, "passed": 1, "total": 1},
            {"turn": 2, "missing_output": False, "passed": 1, "total": 1},
        ])
        names = [a["name"] for a in result["assertions"]]
        self.assertIn("turn-1: asks-question", names)
        self.assertIn("final-has-plan", names)

    def test_failing_turn_lowers_aggregate(self):
        case = self.multi_turn_case()
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            self.write_turn_run(base, ["I will just do it.", "The plan: use alpha."])
            result, _ = sb.grade_case_variant(case, "with_skill", None, base / "output.md", {}, run_base=base)
        self.assertAlmostEqual(result["objective_pass_rate"], 2 / 3)
        self.assertEqual(result["turns"][0]["passed"], 0)

    def test_missing_turn_fails_closed(self):
        case = self.multi_turn_case()
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            self.write_turn_run(base, ["What is the scope?"])   # turn 2 never ran
            result, _ = sb.grade_case_variant(case, "with_skill", None, base / "output.md", {}, run_base=base)
        self.assertTrue(result["turns"][1]["missing_output"])
        self.assertEqual(result["turns"][1]["passed"], 0)
        by_name = {a["name"]: a for a in result["assertions"]}
        self.assertFalse(by_name["turn-2: mentions-alpha"]["passed"])

    def test_single_shot_case_is_untouched(self):
        case = {"id": "c", "split": "tune", "kind": "behavior", "prompt": "p",
                "assertions": [{"name": "a", "type": "contains", "value": "alpha"}]}
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            (base / "output.md").write_text("alpha", encoding="utf-8")
            result, _ = sb.grade_case_variant(case, "with_skill", "alpha", base / "output.md", {}, run_base=base)
        self.assertNotIn("turns", result)
        self.assertEqual(result["objective_pass_rate"], 1.0)

    def test_validation_and_row_carry_turns(self):
        manifest = base_manifest()
        manifest["cases"] = [self.multi_turn_case()]
        with tempfile.TemporaryDirectory() as td:
            path = write_manifest(Path(td), manifest)
            loaded = sb.validate_manifest(path)   # no top-level prompt needed
            rows = sb.prepared_task_rows(path, loaded, split="tune")
        self.assertEqual(rows[0]["turns"], ["Ask a clarifying question about scope.", "Now give the final plan mentioning alpha."])
        self.assertIn("clarifying question", rows[0]["prompt"])   # first turn is the prompt surface

    def test_subagent_runner_drives_the_sequence(self):
        manifest = base_manifest()
        manifest["cases"] = [self.multi_turn_case()]
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            path = write_manifest(root, manifest)
            loaded = sb.validate_manifest(path)
            tasks = sb.prepared_task_rows(path, loaded, split="tune")
            runs = root / "runs"
            transcripts: list[list] = []

            def agent(*, prompt, workspace, model, tool_executor, history=None):
                transcripts.append(list(history or []))
                n = len(history or []) + 1
                return {"answer": f"turn {n} answer? plan alpha"}

            sb.run_subagent_tasks([t for t in tasks if t["variant"] == "with_skill"], runs, agent)
            base = runs / "conv-1" / "with_skill"
            self.assertEqual((base / "turn-1" / "output.md").read_text(encoding="utf-8"), "turn 1 answer? plan alpha")
            self.assertEqual((base / "turn-2" / "output.md").read_text(encoding="utf-8"), "turn 2 answer? plan alpha")
            self.assertEqual((base / "output.md").read_text(encoding="utf-8"), "turn 2 answer? plan alpha")
        self.assertEqual(transcripts[0], [])
        self.assertEqual(len(transcripts[1]), 1)   # second turn saw the first exchange


class ReviewFixRegressionTests(unittest.TestCase):
    """Regression tests for the PR #22 review findings — each encodes one
    reported defect so it cannot return."""

    def graded_case(self) -> tuple[dict, dict]:
        assertion = {"name": "quality", "type": "judge",
                     "graded_dimensions": [{"name": "drama", "rubric": "5 = anchored; 1 = flat"},
                                           {"name": "craft", "rubric": "5 = anchored; 1 = flat"}]}
        case = {"id": "c", "split": "tune", "kind": "behavior", "prompt": "p", "assertions": [assertion]}
        return case, assertion

    def test_p1_judge_command_carries_graded_payload_end_to_end(self):
        # run_one_judge_task must persist dimension_scores/criteria and compute
        # the verdict via the same owner the merge uses — previously the row
        # kept only passed/score/evidence, so graded judging merged as failed.
        case, assertion = self.graded_case()
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            (base / "output.md").write_text("poster", encoding="utf-8")
            _, tasks = sb.grade_case_variant(case, "with_skill", "poster", base / "output.md", {}, run_base=base)
            judge_cmd = "python3 -c \"print('{\\\"dimension_scores\\\": {\\\"drama\\\": 5, \\\"craft\\\": 4}, \\\"rationale\\\": \\\"strong\\\"}')\""
            row = sb.run_one_judge_task(tasks[0], judge_cmd=judge_cmd)
            self.assertEqual(row["dimension_scores"], {"drama": 5, "craft": 4})
            self.assertTrue(row["passed"])
            self.assertAlmostEqual(row["score"], 0.875)
            merged, _ = sb.grade_case_variant(case, "with_skill", "poster", base / "output.md", {}, run_base=base,
                                              judge_results={row["judge_task_id"]: row})
        entry = merged["qualitative_assertions"][0]
        self.assertTrue(entry["passed"])
        self.assertEqual(entry["dimension_scores"], {"drama": 5.0, "craft": 4.0})
        self.assertEqual(merged["graded_score"], 0.875)

    def test_p1_dynamic_rubric_criteria_survive_the_judge_command(self):
        assertion = {"name": "dyn", "type": "judge", "dynamic_rubric": {"instruction": "draft criteria", "minimum_criteria": 2}}
        case = {"id": "c", "split": "tune", "kind": "behavior", "prompt": "p", "assertions": [assertion]}
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            (base / "output.md").write_text("t", encoding="utf-8")
            _, tasks = sb.grade_case_variant(case, "with_skill", "t", base / "output.md", {}, run_base=base)
            judge_cmd = "python3 -c \"print('{\\\"criteria\\\": [{\\\"name\\\": \\\"a\\\", \\\"met\\\": true}, {\\\"name\\\": \\\"b\\\", \\\"met\\\": true}]}')\""
            row = sb.run_one_judge_task(tasks[0], judge_cmd=judge_cmd)
        self.assertEqual(len(row["criteria"]), 2)
        self.assertTrue(row["passed"])
        self.assertEqual(row["score"], 1.0)

    def test_p1_judge_task_ids_are_model_scoped(self):
        # Without the model segment, case-1/m1/with_skill and case-1/m2/with_skill
        # shared an ID and the last verdict silently applied to both models.
        assertion = {"name": "q", "type": "judge", "rubric": ["good"]}
        self.assertNotEqual(
            sb.judge_task_id("case-1", "with_skill", 1, assertion, model="m1"),
            sb.judge_task_id("case-1", "with_skill", 1, assertion, model="m2"))
        self.assertEqual(sb.judge_task_id("case-1", "with_skill", 1, assertion),
                         "case-1::with_skill::run-1::q")   # single-model shape unchanged
        case = {"id": "case-1", "split": "tune", "kind": "behavior", "prompt": "p", "assertions": [assertion]}
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            (base / "output.md").write_text("t", encoding="utf-8")
            verdicts = {
                sb.judge_task_id("case-1", "with_skill", 1, assertion, model="m1"): {"passed": True, "score": 5},
                sb.judge_task_id("case-1", "with_skill", 1, assertion, model="m2"): {"passed": False, "score": 1},
            }
            m1, _ = sb.grade_case_variant(case, "with_skill", "t", base / "output.md", {}, run_base=base, judge_results=verdicts, model="m1")
            m2, _ = sb.grade_case_variant(case, "with_skill", "t", base / "output.md", {}, run_base=base, judge_results=verdicts, model="m2")
        self.assertTrue(m1["qualitative_assertions"][0]["passed"])
        self.assertFalse(m2["qualitative_assertions"][0]["passed"])

    def test_p1_collect_judge_tasks_scopes_ids_by_layout_model(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            manifest = base_manifest()
            manifest["cases"][0]["assertions"].append({"name": "q", "type": "judge", "rubric": ["good"]})
            path = write_manifest(root, manifest)
            runs = root / "runs"
            for model in ["m1", "m2"]:
                base = runs / "case-1" / model / "with_skill"
                base.mkdir(parents=True)
                (base / "output.md").write_text("alpha", encoding="utf-8")
            tasks = sb.collect_judge_tasks(path, runs, variants=["with_skill"])
        ids = {t["judge_task_id"] for t in tasks}
        self.assertEqual(ids, {"case-1::m1::with_skill::run-1::q", "case-1::m2::with_skill::run-1::q"})
        self.assertEqual({t.get("model") for t in tasks}, {"m1", "m2"})

    def test_p2_soft_judge_failure_leaves_combined_rate(self):
        assertion = {"name": "q", "type": "judge", "rubric": ["good"]}
        case = {"id": "c", "split": "tune", "kind": "behavior", "prompt": "p",
                "assertions": [{"name": "a", "type": "contains", "value": "alpha"}, assertion]}
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            (base / "output.md").write_text("alpha", encoding="utf-8")
            jid = sb.judge_task_id("c", "with_skill", 1, assertion)
            soft, _ = sb.grade_case_variant(case, "with_skill", "alpha", base / "output.md", {}, run_base=base,
                                            judge_results={jid: {"passed": False, "score": 0.0}})
            case_gate = json.loads(json.dumps(case))
            case_gate["assertions"][1]["severity"] = "gate"
            gate, _ = sb.grade_case_variant(case_gate, "with_skill", "alpha", base / "output.md", {}, run_base=base,
                                            judge_results={jid: {"passed": False, "score": 0.0}})
        self.assertEqual(soft["combined_pass_rate"], 1.0)   # soft failure feeds graded only
        self.assertEqual(soft["graded_score"], 0.0)
        self.assertEqual(gate["combined_pass_rate"], 0.5)   # gate judge stays in the rate

    def test_p2_junit_counts_qualitative_gate_failures_not_soft(self):
        result = {"case_id": "c", "variant": "with_skill", "run_number": 1, "missing_output": False,
                  "execution_valid": True, "metadata": {},
                  "assertions": [{"name": "a", "passed": True, "severity": "gate"}],
                  "qualitative_assertions": [
                      {"name": "gate-judge", "passed": False, "severity": "gate", "evidence": "weak"},
                      {"name": "soft-judge", "passed": False, "severity": "soft", "evidence": "meh"},
                  ]}
        lines = sb.result_failure_lines(result)
        self.assertEqual(lines, ["gate-judge: weak"])
        report = {"skill_name": "d", "summary": {}, "paired_summary": {}, "case_flags": [], "results": [result]}
        self.assertIn('failures="1"', sb.junit_xml_from_report(report))

    def test_p2_tool_call_matches_normalized_tool_call_events(self):
        events = {"schema_version": 2, "source": "subagent", "events": [
            {"type": "tool_call", "name": "Read", "input_summary": "skills/demo/refs.md", "status": "completed"},
            {"type": "tool_call", "name": "WebSearch", "input_summary": "query", "status": "in_progress"},
        ]}
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            (base / "output.md").write_text("t", encoding="utf-8")
            (base / "events.json").write_text(json.dumps(events), encoding="utf-8")
            hit = sb.assertion_result({"type": "tool_call", "tool": "Read"}, "t", base / "output.md", run_base=base)
            in_progress_only = sb.assertion_result({"type": "tool_call", "tool": "WebSearch"}, "t", base / "output.md", run_base=base)
        self.assertTrue(hit["passed"], hit["evidence"])
        self.assertFalse(in_progress_only["passed"])   # a started-but-unfinished call is not a call that ran

    def test_p2_turn_assertions_are_validated(self):
        manifest = base_manifest()
        manifest["cases"] = [{
            "id": "conv", "split": "tune", "kind": "behavior",
            "turns": [{"prompt": "ask", "assertions": [{"type": "no_such_type", "value": "x"}]}],
        }]
        with tempfile.TemporaryDirectory() as td:
            path = write_manifest(Path(td), manifest)
            with self.assertRaises(SystemExit):
                sb.validate_manifest(path)


class AssertionDependenciesTests(unittest.TestCase):
    """G2 — depends_on / staged grading. Mutation-killing: exact totals, and the
    critical-tie both-directions (a running critical vetoes; a skipped one does not)."""

    def _grade(self, assertions, text="alpha", judge_results=None):
        case = {"id": "c", "split": "tune", "kind": "behavior", "assertions": assertions}
        result, tasks = sb.grade_case_variant(case, "with_skill", text, Path("out.md"), {}, judge_results=judge_results or {})
        return result, tasks

    # --- validation ---
    def test_shape_rejected(self):
        p = Path("x")
        for bad in ([], 5, ["ok", 1], ""):
            with self.assertRaises(SystemExit):
                sb.validate_case_assertion("c", "a", 0, {"type": "contains", "value": "x", "depends_on": bad}, p)
        sb.validate_case_assertion("c", "a", 0, {"type": "contains", "value": "x", "depends_on": "pre"}, p)   # ok

    def test_scope_unknown_ambiguous_and_cycle_rejected(self):
        p = Path("x")
        A = lambda **kw: {"type": "contains", "value": "x", **kw}
        with self.assertRaises(SystemExit):   # unknown target
            sb.validate_depends_on_scope("c", [A(name="dep", depends_on="missing")], p)
        with self.assertRaises(SystemExit):   # self-cycle
            sb.validate_depends_on_scope("c", [A(name="a", depends_on="a")], p)
        with self.assertRaises(SystemExit):   # 2-cycle
            sb.validate_depends_on_scope("c", [A(name="a", depends_on="b"), A(name="b", depends_on="a")], p)
        with self.assertRaises(SystemExit):   # ambiguous target (duplicate label)
            sb.validate_depends_on_scope("c", [A(name="pre"), A(name="pre", value="y"), A(name="dep", depends_on="pre")], p)
        sb.validate_depends_on_scope("c", [A(name="pre"), A(name="dep", depends_on="pre")], p)   # valid graph

    def test_turn_depends_on_rejected_at_validate(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "repo" / "skill").mkdir(parents=True)
            (root / "repo" / "skill" / "SKILL.md").write_text("---\nname: d\ndescription: D\n---\n", encoding="utf-8")
            (root / "repo" / "evals").mkdir()
            p = root / "repo" / "evals" / "shared-benchmark.json"
            p.write_text(json.dumps({"version": 1, "skill_name": "d", "skill_paths": ["skill/SKILL.md"],
                "variants": ["with_skill", "without_skill"], "ablations": [],
                "cases": [{"id": "c", "split": "tune", "kind": "behavior",
                           "turns": [{"prompt": "p", "assertions": [{"name": "t", "type": "contains", "value": "x", "depends_on": "other"}]}]}]}), encoding="utf-8")
            with self.assertRaises(SystemExit):
                sb.validate_manifest(p)

    # --- grading ---
    def test_dependent_counted_when_prereq_passes(self):
        result, _ = self._grade([{"name": "pre", "type": "contains", "value": "alpha"},
                                 {"name": "dep", "type": "contains", "value": "alpha", "depends_on": "pre"}])
        self.assertEqual(result["skipped_total"], 0)
        self.assertEqual((result["objective_total"], result["objective_passed"]), (2, 2))
        self.assertEqual(result["objective_pass_rate"], 1.0)

    def test_dependent_SKIPPED_not_zeroed_when_prereq_fails(self):
        result, _ = self._grade([{"name": "pre", "type": "contains", "value": "zzz"},       # FAILS
                                 {"name": "dep", "type": "contains", "value": "alpha", "depends_on": "pre"}])
        # mutation-killing: skip (not zero, not second-failure) => total drops to 1
        self.assertEqual(result["objective_total"], 1)
        self.assertEqual(result["objective_passed"], 0)
        self.assertEqual(result["objective_pass_rate"], 0.0)
        self.assertEqual(result["skipped_total"], 1)
        dep = next(r for r in result["assertions"] if r["name"] == "dep")
        self.assertTrue(dep["skipped"])
        self.assertIn("pre", dep["skip_reason"])

    def test_running_critical_failure_vetoes(self):
        result, _ = self._grade([{"name": "crit", "type": "contains", "value": "zzz", "severity": "critical"}])
        self.assertTrue(result["vetoed"])          # a critical that RUNS and fails still vetoes

    def test_skipped_critical_dependent_does_NOT_veto(self):
        # THE keystone: a critical dependent whose gate prerequisite failed is skipped,
        # so it must NOT collapse the run (a never-run assertion cannot veto).
        result, _ = self._grade([{"name": "pre", "type": "contains", "value": "zzz"},        # gate FAIL
                                 {"name": "dep", "type": "contains", "value": "alpha", "depends_on": "pre", "severity": "critical"}])
        self.assertFalse(result["vetoed"])
        self.assertEqual(result["critical_total"], 0)   # dep excluded from critical_rows
        self.assertEqual(result["skipped_total"], 1)

    def test_transitive_skip(self):
        result, _ = self._grade([{"name": "a", "type": "contains", "value": "zzz"},                    # FAIL
                                 {"name": "b", "type": "contains", "value": "alpha", "depends_on": "a"},
                                 {"name": "c", "type": "contains", "value": "alpha", "depends_on": "b"}])
        self.assertEqual(result["skipped_total"], 2)
        self.assertEqual(result["objective_total"], 1)   # only a counts
        self.assertEqual({r["name"] for r in result["assertions"] if r.get("skipped")}, {"b", "c"})

    def test_qualitative_prerequisite_skips_objective_dependent(self):
        jassert = {"name": "jpre", "type": "judge", "severity": "gate"}
        expanded = sb.expand_judge_preset(jassert)
        jid = sb.judge_task_id("c", "with_skill", 1, expanded)
        result, _ = self._grade([jassert, {"name": "dep", "type": "contains", "value": "alpha", "depends_on": "jpre"}],
                                judge_results={jid: {"judge_task_id": jid, "passed": False, "score": 0}})
        dep = next(r for r in result["assertions"] if r["name"] == "dep")
        self.assertTrue(dep["skipped"])                  # resolved on the verdict-loaded pass
        self.assertEqual(result["objective_total"], 0)   # dep skipped out
        self.assertEqual((result["qualitative_total"], result["qualitative_passed"]), (1, 0))

    def test_no_depends_on_is_byte_identical_grading(self):
        result, _ = self._grade([{"name": "a", "type": "contains", "value": "alpha"},
                                 {"name": "b", "type": "contains", "value": "alpha"}])
        self.assertEqual(result["skipped_total"], 0)
        self.assertEqual(result["objective_total"], 2)
        self.assertFalse(any(r.get("skipped") for r in result["assertions"]))

    def test_inline_suppresses_judge_task_for_skipped_dependent(self):
        # objective prereq fails (graded first); a JUDGE dependent must be skipped
        # WITHOUT emitting a judge task -- the inline short-circuit, not just the post-pass.
        case = {"id": "c", "split": "tune", "kind": "behavior", "assertions": [
            {"name": "pre", "type": "contains", "value": "zzz"},
            {"name": "jdep", "type": "judge", "depends_on": "pre"}]}
        result, tasks = sb.grade_case_variant(case, "with_skill", "alpha", Path("o.md"), {})
        self.assertEqual(result["deferred_judge_tasks"], 0)   # no judge call spent on the skipped dependent
        self.assertEqual(len(tasks), 0)
        jdep = next(r for r in result["qualitative_assertions"] if r["name"] == "jdep")
        self.assertTrue(jdep["skipped"])
        self.assertEqual(result["qualitative_total"], 0)      # skipped judge dependent drops out of the qual denominator

    def test_forward_reference_to_preset_prerequisite_resolves_both_orders(self):
        # A preset prerequisite's row name is rewritten (-> "factuality") while depends_on
        # targets the author's label ("grounded"). A forward reference (dependent listed
        # first) must STILL skip, not spuriously veto. Regression for the order-dependent
        # bug: the post-pass keyed row_by_label on the emitted name, missing the preset.
        A = {"type": "factuality", "description": "grounded"}
        B = {"name": "B", "type": "contains", "value": "ZZZ_absent", "depends_on": "grounded", "critical": True}
        jid = sb.judge_task_id("c", "with_skill", 1, sb.expand_judge_preset(A))
        verdict = {jid: {"judge_task_id": jid, "passed": False, "score": 1}}
        for order, assertions in (("in_order", [A, B]), ("forward", [B, A])):
            result, _ = self._grade(assertions, judge_results=verdict)
            skipped = {r.get("name") for r in result["assertions"] if r.get("skipped")}
            self.assertEqual(skipped, {"B"}, order)            # dependent skipped in BOTH orders
            self.assertFalse(result.get("vetoed"), order)      # never a spurious critical veto
            self.assertEqual(result["skipped_total"], 1, order)

    def test_reverse_order_transitive_chain_needs_fixed_point(self):
        # c->b->a declared in REVERSE: resolving c needs b resolved first, so one pass is
        # insufficient — the fixed-point loop must iterate. (test_transitive_skip declares
        # in dependency order, which the inline short-circuit alone would satisfy.)
        result, _ = self._grade([{"name": "c", "type": "contains", "value": "alpha", "depends_on": "b"},
                                 {"name": "b", "type": "contains", "value": "alpha", "depends_on": "a"},
                                 {"name": "a", "type": "contains", "value": "zzz"}])   # a FAILS
        self.assertEqual({r["name"] for r in result["assertions"] if r.get("skipped")}, {"b", "c"})
        self.assertEqual(result["objective_total"], 1)   # only a counts
        self.assertEqual(result["skipped_total"], 2)

    def test_skipped_soft_dependent_excluded_from_soft_total(self):
        result, _ = self._grade([{"name": "pre", "type": "contains", "value": "zzz"},   # FAIL
                                 {"name": "s", "type": "contains", "value": "alpha", "severity": "soft", "depends_on": "pre"}])
        self.assertEqual(result["soft_total"], 0)   # skipped soft dependent drops out of the soft denominator
        self.assertEqual({r["name"] for r in result["assertions"] if r.get("skipped")}, {"s"})


class ToolCallTaxonomyTests(unittest.TestCase):
    def _events(self, names):
        tmp = tempfile.TemporaryDirectory(prefix="toolcall-")
        self.addCleanup(tmp.cleanup)
        td = Path(tmp.name)
        events = [{"type": "tool_call", "name": n, "status": "completed"} for n in names]
        (td / "events.json").write_text(json.dumps(events), encoding="utf-8")
        (td / "output.md").write_text("out", encoding="utf-8")
        return td

    def test_expected_no_call(self):
        base = self._events(["Read", "Grep"])
        ok = sb.assertion_result({"type": "tool_call", "tool": "WebSearch", "expected_no_call": True}, "t", base / "output.md", run_base=base)
        self.assertTrue(ok["passed"])   # WebSearch never called -> passes
        bad = sb.assertion_result({"type": "tool_call", "tool": "Read", "expected_no_call": True}, "t", base / "output.md", run_base=base)
        self.assertFalse(bad["passed"])  # Read WAS called

    def test_required_calls_subset(self):
        base = self._events(["Read", "Grep", "Edit"])
        ok = sb.assertion_result({"type": "tool_call", "required_calls": ["Read", "Edit"]}, "t", base / "output.md", run_base=base)
        self.assertTrue(ok["passed"])   # both present, extras (Grep) allowed
        miss = sb.assertion_result({"type": "tool_call", "required_calls": ["Read", "WebSearch"]}, "t", base / "output.md", run_base=base)
        self.assertFalse(miss["passed"])

    def test_call_set_multiset_rejects_unexpected_and_missing(self):
        base = self._events(["Read", "Grep"])
        ok = sb.assertion_result({"type": "tool_call", "call_set": ["Read", "Grep"]}, "t", base / "output.md", run_base=base)
        self.assertTrue(ok["passed"])
        extra = sb.assertion_result({"type": "tool_call", "call_set": ["Read"]}, "t", base / "output.md", run_base=base)
        self.assertFalse(extra["passed"])   # Grep is unexpected
        missing = sb.assertion_result({"type": "tool_call", "call_set": ["Read", "Grep", "Edit"]}, "t", base / "output.md", run_base=base)
        self.assertFalse(missing["passed"])  # Edit is missing (drives the missing branch)

    def test_call_set_counts_multiplicity(self):
        base = self._events(["Read", "Read"])   # two Reads
        self.assertFalse(sb.assertion_result({"type": "tool_call", "call_set": ["Read"]}, "t", base / "output.md", run_base=base)["passed"])
        self.assertTrue(sb.assertion_result({"type": "tool_call", "call_set": ["Read", "Read"]}, "t", base / "output.md", run_base=base)["passed"])

    def test_name_match_is_not_substring(self):
        # a shell `cat readme` (a command event, no tool name) must NOT satisfy
        # required_calls:["Read"] — the audit's core false-positive.
        tmp = tempfile.TemporaryDirectory(prefix="toolcall-cmd-")
        self.addCleanup(tmp.cleanup)
        td = Path(tmp.name)
        (td / "events.json").write_text(json.dumps([{"type": "command", "command": "cat README.md", "status": "completed"}]), encoding="utf-8")
        (td / "output.md").write_text("out", encoding="utf-8")
        res = sb.assertion_result({"type": "tool_call", "required_calls": ["Read"]}, "t", td / "output.md", run_base=td)
        self.assertFalse(res["passed"])
        # and a real tool named ReadFile does not satisfy "Read" (exact, not prefix)
        base = self._events(["ReadFile"])
        self.assertFalse(sb.assertion_result({"type": "tool_call", "required_calls": ["Read"]}, "t", base / "output.md", run_base=base)["passed"])

    def test_expected_no_call_with_pattern(self):
        base = self._events(["curl", "Read"])
        self.assertFalse(sb.assertion_result({"type": "tool_call", "expected_no_call": True, "pattern": "curl"}, "t", base / "output.md", run_base=base)["passed"])
        self.assertTrue(sb.assertion_result({"type": "tool_call", "expected_no_call": True, "pattern": "wget"}, "t", base / "output.md", run_base=base)["passed"])


class ToolCallValidationTests(unittest.TestCase):
    """P2: validate_case_assertion rejects malformed tool_call assertions at
    manifest-validation time rather than degrading silently in grading."""
    def _validate(self, assertion):
        sb.validate_case_assertion("c1", "a", 0, assertion, Path("."))

    def test_valid_single_selectors_pass(self):
        self._validate({"type": "tool_call", "required_calls": ["Read"]})
        self._validate({"type": "tool_call", "call_set": ["Read", "Grep"]})
        self._validate({"type": "tool_call", "expected_no_call": True, "pattern": "curl"})   # pattern is a modifier, not a 2nd selector
        self._validate({"type": "tool_call", "order": ["Read", "Edit"]})
        self._validate({"type": "tool_call", "tool": "Read"})                                # legacy pattern/count path

    def test_rejects_multiple_selectors(self):
        with self.assertRaises(SystemExit):
            self._validate({"type": "tool_call", "required_calls": ["Read"], "call_set": ["Read"]})
        with self.assertRaises(SystemExit):
            self._validate({"type": "tool_call", "expected_no_call": True, "required_calls": ["Read"]})

    def test_rejects_bad_list_types(self):
        for bad in ("Read", [], [1, 2], ["ok", 3]):
            with self.assertRaises(SystemExit):
                self._validate({"type": "tool_call", "required_calls": bad})

    def test_rejects_non_bool_expected_no_call(self):
        with self.assertRaises(SystemExit):
            self._validate({"type": "tool_call", "expected_no_call": "false"})   # the string footgun
        with self.assertRaises(SystemExit):
            self._validate({"type": "tool_call", "expected_no_call": 1})

    def test_rejects_invalid_regex_in_pattern_or_order(self):
        with self.assertRaises(SystemExit):
            self._validate({"type": "tool_call", "pattern": "["})
        with self.assertRaises(SystemExit):
            self._validate({"type": "tool_call", "order": ["ok", "("]})

    def test_literal_name_selectors_are_not_regex_validated(self):
        # required_calls/call_set are exact tool NAMES, so a regex-special name is fine
        self._validate({"type": "tool_call", "required_calls": ["Read(", "a.b"]})


class TriggerNotGradedIntoAnswerTests(unittest.TestCase):
    """The answer benchmark must not fold kind:'trigger' cases into its paired
    pass-rate: a trigger case is a discovery (autonomous-load) measurement, a
    different population from a with/without answer comparison. Grading its
    content here would let a user compare that number to a Pi trigger pass-rate as
    if they were the same metric — the exact cross-population conflation the spec
    warns against. The report also stamps population='answer' so the two report
    kinds can't be confused in the emitted JSON."""

    def test_prepare_emits_no_answer_runner_rows_for_trigger_cases(self):
        # The answer-path preparer withholds trigger cases from the forced-load
        # runners (they can't measure autonomous discovery) — so the guard in
        # build_benchmark_report is defense-in-depth, not the sole enforcement.
        with tempfile.TemporaryDirectory() as td:
            rp = Path(td) / "repo"; _skill(rp)
            cases = [
                {"id": "ans", "split": "tune", "kind": "pr-review", "prompt": "review",
                 "assertions": [{"name": "k", "type": "contains", "value": "X"}]},
                {"id": "trg", "split": "tune", "kind": "trigger", "prompt": "would you load?",
                 "assertions": [{"name": "k", "type": "contains", "value": "X"}]},
            ]
            p = _manifest(rp, cases)
            rows = sb.prepared_task_rows(p, sb.validate_manifest(p))
            case_ids = {r["case_id"] for r in rows}
            self.assertIn("ans", case_ids)
            self.assertNotIn("trg", case_ids)          # no with_skill/without_skill row for a trigger case

    def test_trigger_case_excluded_and_population_stamped(self):
        with tempfile.TemporaryDirectory() as td:
            rp = Path(td) / "repo"; _skill(rp)
            cases = [
                {"id": "ans", "split": "tune", "kind": "pr-review", "prompt": "review",
                 "assertions": [{"name": "k", "type": "contains", "value": "GOOD"}]},
                {"id": "trg", "split": "tune", "kind": "trigger", "prompt": "would you load?",
                 "assertions": [{"name": "k", "type": "contains", "value": "GOOD"}]},
            ]
            p = _manifest(rp, cases)
            runs = Path(td) / "runs"
            for cid in ("ans", "trg"):
                for v in ("with_skill", "without_skill"):
                    write_run(runs / cid / v, "GOOD result", metadata={}, metrics={})
            report = sb.build_benchmark_report(p, runs, split="tune",
                                               variants_arg=["with_skill", "without_skill"])
            self.assertEqual(report["population"], "answer")
            self.assertEqual(report["skipped_trigger_cases"], ["trg"])
            self.assertTrue(all(r["case_id"] != "trg" for r in report["results"]))
            self.assertTrue(any(r["case_id"] == "ans" for r in report["results"]))


if __name__ == "__main__":
    unittest.main()
