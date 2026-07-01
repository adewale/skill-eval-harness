"""Tests for the eval-framework roadmap features (docs/eval-framework-roadmap-spec.md).

Organized by spec item number. House rules hold throughout: no live model, no
network — fixtures and mocks only. The confidence floor has its own file
(tests/test_confidence_floor.py); detector-level fixtures live under
tests/fixtures/detectors/ per the CF.1 registration contract.
"""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import skill_benchmark as sb


def write_manifest(root: Path, manifest: dict) -> Path:
    repo = root / "repo"
    (repo / "skill").mkdir(parents=True, exist_ok=True)
    (repo / "skill" / "SKILL.md").write_text("---\nname: demo\ndescription: Demo\n---\n", encoding="utf-8")
    (repo / "evals").mkdir(exist_ok=True)
    path = repo / "evals" / "shared-benchmark.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    return path


def base_manifest(**overrides) -> dict:
    manifest = {
        "version": 1,
        "skill_name": "demo",
        "skill_paths": ["skill/SKILL.md"],
        "variants": ["with_skill", "without_skill"],
        "cases": [{
            "id": "case-1",
            "split": "tune",
            "kind": "behavior",
            "prompt": "Do the task.",
            "assertions": [{"name": "has-alpha", "type": "contains", "value": "alpha"}],
        }],
        "ablations": [],
    }
    manifest.update(overrides)
    return manifest


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


class ReportFormatsTests(unittest.TestCase):
    """1.2 — JUnit XML and GitHub job-summary serialization of benchmark.json."""

    REPORT = {
        "skill_name": "demo",
        "summary": {
            "with_skill": {"cases": 1, "runs": 2, "missing_outputs": 0, "execution_errors": 0, "mean_objective_pass_rate": 1.0, "mean_combined_pass_rate": 1.0},
            "without_skill": {"cases": 1, "runs": 2, "missing_outputs": 1, "execution_errors": 0, "mean_objective_pass_rate": 0.5, "mean_combined_pass_rate": 0.5},
        },
        "paired_summary": {
            "with_skill_objective_pass_rate": 1.0,
            "without_skill_objective_pass_rate": 0.5,
            "absolute_delta": 0.5,
            "normalized_gain": 1.0,
            "negative_delta_cases": [],
        },
        "case_flags": [{"case_id": "case-1", "flags": ["flaky repeated pass rates: without_skill"], "with_skill": 1.0, "without_skill": 0.5}],
        "results": [
            {"case_id": "case-1", "variant": "with_skill", "run_number": 1, "missing_output": False, "execution_valid": True,
             "assertions": [{"name": "has-alpha", "passed": True, "evidence": "contains 'alpha'"}], "metadata": {"elapsed_ms": 1500}},
            {"case_id": "case-1", "variant": "without_skill", "run_number": 1, "missing_output": False, "execution_valid": True,
             "assertions": [{"name": "has-alpha", "passed": False, "evidence": "missing 'alpha'"}], "metadata": {"elapsed_ms": 500}},
            {"case_id": "case-1", "variant": "without_skill", "run_number": 2, "missing_output": True, "execution_valid": True,
             "assertions": [], "metadata": {}, "run_base": "runs/case-1/without_skill/run-2"},
        ],
    }

    def test_junit_shape(self):
        xml = sb.junit_xml_from_report(self.REPORT)
        self.assertIn('<?xml version="1.0" encoding="UTF-8"?>', xml)
        self.assertIn('testsuite name="skill-eval:demo"', xml)
        self.assertIn('tests="3"', xml)
        self.assertIn('failures="2"', xml)
        self.assertIn('classname="demo.case-1"', xml)
        self.assertIn('name="with_skill/run-1"', xml)
        self.assertIn("has-alpha: missing 'alpha'", xml)
        self.assertIn("missing output", xml)
        self.assertIn('property name="absolute_delta" value="0.5000"', xml)

    def test_junit_is_well_formed(self):
        import xml.etree.ElementTree as ET
        root = ET.fromstring(sb.junit_xml_from_report(self.REPORT))
        self.assertEqual(root.tag, "testsuite")
        self.assertEqual(len(root.findall("testcase")), 3)

    def test_github_summary_markdown_and_annotations(self):
        md = sb.github_summary_from_report(self.REPORT)
        self.assertIn("# Skill eval — demo", md)
        self.assertIn("= **0.50**", md)
        self.assertIn("| with_skill | 1 | 2 | 1.00 | 1.00 | 0 | 0 |", md)
        self.assertIn("::warning title=skill-eval case case-1::flaky repeated pass rates: without_skill", md)
        self.assertNotIn("::error", md)

    def test_github_summary_flags_negative_lift_as_error(self):
        report = json.loads(json.dumps(self.REPORT))
        report["paired_summary"]["absolute_delta"] = -0.25
        report["paired_summary"]["negative_delta_cases"] = [{"case_id": "case-1", "with_skill": 0.25, "without_skill": 0.5, "delta": -0.25}]
        md = sb.github_summary_from_report(report)
        self.assertIn("::error", md)
        self.assertIn("Negative-delta cases", md)

    def test_report_command_round_trip(self):
        with tempfile.TemporaryDirectory() as td:
            bench = Path(td) / "benchmark.json"
            bench.write_text(json.dumps(self.REPORT), encoding="utf-8")
            out = Path(td) / "junit.xml"
            rc = sb.report_command(SimpleNamespace(benchmark=str(bench), format="junit", out=str(out)))
            self.assertEqual(rc, 0)
            self.assertIn("testsuite", out.read_text(encoding="utf-8"))


class JudgeConfigSlotTests(unittest.TestCase):
    """1.3 — judge config slot and the judge-is-not-the-model-under-test guard."""

    def test_manifest_judge_block_validates(self):
        with tempfile.TemporaryDirectory() as td:
            path = write_manifest(Path(td), base_manifest(judge={"model": "judge-model-x"}))
            manifest = sb.validate_manifest(path)
            self.assertEqual(manifest["judge"]["model"], "judge-model-x")

    def test_bad_judge_block_dies(self):
        with tempfile.TemporaryDirectory() as td:
            path = write_manifest(Path(td), base_manifest(judge={"model": 7}))
            with self.assertRaises(SystemExit):
                sb.validate_manifest(path)

    def test_effective_judge_model_prefers_cli_then_manifest(self):
        manifest = base_manifest(judge={"model": "manifest-judge"})
        self.assertEqual(sb.effective_judge_model(manifest, "cli-judge"), "cli-judge")
        self.assertEqual(sb.effective_judge_model(manifest, None), "manifest-judge")
        self.assertIsNone(sb.effective_judge_model(base_manifest(), None))

    def audit(self, root: Path, manifest: dict) -> dict:
        path = write_manifest(root, manifest)
        return sb.audit_manifest_report(path)

    def test_guard_fires_when_judge_matches_jetty_model(self):
        with tempfile.TemporaryDirectory() as td:
            report = self.audit(Path(td), base_manifest(judge={"model": "same-model"}, jetty={"model": "same-model"}))
        kinds = [f["kind"] for f in report["findings"]]
        self.assertIn("judge-is-model-under-test", kinds)

    def test_guard_silent_when_models_differ(self):
        with tempfile.TemporaryDirectory() as td:
            report = self.audit(Path(td), base_manifest(judge={"model": "judge-a"}, jetty={"model": "under-test-b"}))
        kinds = [f["kind"] for f in report["findings"]]
        self.assertNotIn("judge-is-model-under-test", kinds)

    def test_guard_reads_run_metadata_models(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            path = write_manifest(root, base_manifest(judge={"model": "model-under-test"}))
            runs = root / "runs"
            for variant, text in [("with_skill", "alpha"), ("without_skill", "beta")]:
                base = runs / "case-1" / variant
                base.mkdir(parents=True)
                (base / "output.md").write_text(text, encoding="utf-8")
                (base / "metadata.json").write_text(json.dumps({"model": "model-under-test"}), encoding="utf-8")
            report = sb.audit_manifest_report(path, runs=str(runs))
        kinds = [f["kind"] for f in report["findings"]]
        self.assertIn("judge-is-model-under-test", kinds)

    def test_strict_judge_exits_nonzero(self):
        with tempfile.TemporaryDirectory() as td:
            path = write_manifest(Path(td), base_manifest(judge={"model": "m"}, jetty={"model": "m"}))
            args = SimpleNamespace(
                manifest=str(path), skill_path=None, runs=None, split=None, format="json",
                out=str(Path(td) / "audit.json"), min_positive=0, min_negative=0, min_adversarial=0,
                min_trigger_pos=0, min_trigger_neg=0, leakage_min_chars=4,
                fail_on_blockers=False, strict_judge=True)
            self.assertEqual(sb.audit_manifest(args), 1)
            args.strict_judge = False
            self.assertEqual(sb.audit_manifest(args), 0)


class MultiModelFanOutTests(unittest.TestCase):
    """2.1 — model as a third fan-out axis beside variant and run_number."""

    def two_case_manifest(self) -> dict:
        manifest = base_manifest()
        manifest["cases"].append({
            "id": "case-2",
            "split": "tune",
            "kind": "behavior",
            "prompt": "Do the other task.",
            "assertions": [{"name": "has-beta", "type": "contains", "value": "beta"}],
        })
        return manifest

    def test_row_count_is_cases_by_variants_by_runs_by_models(self):
        with tempfile.TemporaryDirectory() as td:
            path = write_manifest(Path(td), self.two_case_manifest())
            manifest = sb.validate_manifest(path)
            rows = sb.prepared_task_rows(path, manifest, split="tune", runs_per_variant=2, models=["m1", "m2"])
        self.assertEqual(len(rows), 2 * 2 * 2 * 2)
        run_dirs = {r["run_dir"] for r in rows}
        self.assertIn("case-1/m1/with_skill/run-1", run_dirs)
        self.assertIn("case-2/m2/without_skill/run-2", run_dirs)
        self.assertEqual({r["model"] for r in rows}, {"m1", "m2"})

    def test_single_model_keeps_legacy_run_dir_but_carries_model(self):
        with tempfile.TemporaryDirectory() as td:
            path = write_manifest(Path(td), base_manifest())
            manifest = sb.validate_manifest(path)
            rows = sb.prepared_task_rows(path, manifest, split="tune", models=["only-model"])
        self.assertEqual({r["run_dir"] for r in rows}, {"case-1/with_skill", "case-1/without_skill"})
        self.assertTrue(all(r["model"] == "only-model" for r in rows))

    def test_no_models_is_byte_identical_to_legacy_rows(self):
        with tempfile.TemporaryDirectory() as td:
            path = write_manifest(Path(td), base_manifest())
            manifest = sb.validate_manifest(path)
            legacy = sb.prepared_task_rows(path, manifest, split="tune")
            explicit = sb.prepared_task_rows(path, manifest, split="tune", models=[])
        self.assertEqual(json.dumps(legacy), json.dumps(explicit))
        self.assertTrue(all("model" not in r for r in legacy))

    def test_model_root_discovery_handles_both_layouts(self):
        with tempfile.TemporaryDirectory() as td:
            runs = Path(td)
            (runs / "case-1" / "with_skill").mkdir(parents=True)
            (runs / "case-1" / "m1" / "with_skill").mkdir(parents=True)
            (runs / "case-1" / "m2" / "without_skill").mkdir(parents=True)
            roots = sb.discover_case_model_roots(runs, "case-1", ["with_skill", "without_skill"])
        labels = [m for m, _ in roots]
        self.assertEqual(labels, [None, "m1", "m2"])

    def make_two_model_runs(self, root: Path) -> tuple[Path, Path]:
        path = write_manifest(root, base_manifest())
        runs = root / "runs"
        # m1: the skill lifts (with passes, without fails); m2: flat (both fail).
        outputs = {
            ("m1", "with_skill"): "alpha",
            ("m1", "without_skill"): "nope",
            ("m2", "with_skill"): "nope",
            ("m2", "without_skill"): "nope",
        }
        for (model, variant), text in outputs.items():
            base = runs / "case-1" / model / variant
            base.mkdir(parents=True)
            (base / "output.md").write_text(text, encoding="utf-8")
            (base / "metadata.json").write_text(json.dumps({"model": model, "total_tokens": 10}), encoding="utf-8")
        return path, runs

    def test_report_groups_by_model_and_pairs_lift_per_model(self):
        with tempfile.TemporaryDirectory() as td:
            path, runs = self.make_two_model_runs(Path(td))
            report = sb.build_benchmark_report(path, runs)
        self.assertEqual(set(report["by_model"]), {"m1", "m2"})
        self.assertEqual(report["by_model"]["m1"]["with_skill"]["mean_objective_pass_rate"], 1.0)
        self.assertEqual(report["by_model"]["m2"]["with_skill"]["mean_objective_pass_rate"], 0.0)
        paired = report["paired_summary"]
        self.assertEqual(paired["by_model"]["m1"]["absolute_delta"], 1.0)
        self.assertEqual(paired["by_model"]["m2"]["absolute_delta"], 0.0)
        # Headline pools the per-(case, model) pairs: (1.0 + 0.0) / 2 vs 0.0.
        self.assertEqual(paired["with_skill_objective_pass_rate"], 0.5)
        self.assertEqual(paired["without_skill_objective_pass_rate"], 0.0)
        self.assertEqual(paired["absolute_delta"], 0.5)
        models_on_results = {r["model"] for r in report["results"]}
        self.assertEqual(models_on_results, {"m1", "m2"})

    def test_legacy_single_layout_report_shape_unchanged(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            path = write_manifest(root, base_manifest())
            runs = root / "runs"
            for variant, text in [("with_skill", "alpha"), ("without_skill", "nope")]:
                base = runs / "case-1" / variant
                base.mkdir(parents=True)
                (base / "output.md").write_text(text, encoding="utf-8")
            report = sb.build_benchmark_report(path, runs)
        self.assertEqual(report["by_model"], {})
        self.assertNotIn("by_model", report["paired_summary"])
        self.assertEqual(report["paired_summary"]["absolute_delta"], 1.0)


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


class JudgePresetTests(unittest.TestCase):
    """1.1 — factuality preset expands to a canned anchored rubric."""

    def test_factuality_type_expands_with_rubric_and_threshold(self):
        expanded = sb.expand_judge_preset({"type": "factuality"})
        self.assertTrue(expanded["rubric"])
        self.assertEqual(expanded["threshold"], 4)
        self.assertEqual(expanded["name"], "factuality")

    def test_explicit_fields_win_over_preset(self):
        expanded = sb.expand_judge_preset({"type": "judge", "preset": "factuality", "threshold": 5, "name": "custom"})
        self.assertEqual(expanded["threshold"], 5)
        self.assertEqual(expanded["name"], "custom")
        self.assertTrue(expanded["rubric"])

    def test_factuality_emits_judge_task_with_rubric_and_merges_results(self):
        case = {"id": "c", "split": "tune", "kind": "behavior", "prompt": "p",
                "assertions": [{"type": "factuality"}]}
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            (base / "output.md").write_text("claims", encoding="utf-8")
            result, tasks = sb.grade_case_variant(case, "with_skill", "claims", base / "output.md", {}, run_base=base)
            self.assertEqual(len(tasks), 1)
            self.assertTrue(tasks[0]["assertion"]["rubric"])   # canned rubric rides the judge task
            jid = tasks[0]["judge_task_id"]
            merged, _ = sb.grade_case_variant(case, "with_skill", "claims", base / "output.md", {}, run_base=base,
                                              judge_results={jid: {"passed": True, "score": 5, "rationale": "grounded"}})
        self.assertEqual(merged["qualitative_passed"], 1)
        self.assertEqual(merged["qualitative_assertions"][0]["oracle"], "live")

    def test_unknown_preset_dies_in_validation(self):
        manifest = base_manifest()
        manifest["cases"][0]["assertions"] = [{"type": "judge", "preset": "vibes"}]
        with tempfile.TemporaryDirectory() as td:
            path = write_manifest(Path(td), manifest)
            with self.assertRaises(SystemExit):
                sb.validate_manifest(path)


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


class OTelNormalizationTests(unittest.TestCase):
    """2.4 — OTel GenAI semantic-convention attributes on normalized traces."""

    def test_command_event_carries_execute_tool_attributes(self):
        records = [{"type": "command", "command": "python -m pytest", "exit_code": 0}]
        events_doc, metrics = sb.normalize_trace_records(records, source="codex")
        self.assertEqual(events_doc["schema_version"], 2)
        self.assertEqual(metrics["schema_version"], 2)
        otel = events_doc["events"][0]["otel"]
        self.assertEqual(otel["gen_ai.operation.name"], "execute_tool")
        self.assertEqual(otel["gen_ai.tool.name"], "bash")
        self.assertIn("pytest", otel["gen_ai.tool.call.arguments"])
        self.assertEqual(otel["process.exit_code"], 0)

    def test_usage_lands_in_otel_metrics(self):
        records = [{"type": "usage", "usage": {"input_tokens": 100, "output_tokens": 40}}]
        events_doc, metrics = sb.normalize_trace_records(records, source="pi")
        self.assertEqual(metrics["otel"], {"gen_ai.usage.input_tokens": 100, "gen_ai.usage.output_tokens": 40})
        self.assertEqual(events_doc["events"][0]["otel"]["gen_ai.usage.input_tokens"], 100)

    def test_message_and_error_attributes(self):
        records = [{"type": "agent_message", "content": "hello"}, {"type": "error", "message": "boom"}]
        events_doc, _ = sb.normalize_trace_records(records, source="generic")
        self.assertEqual(events_doc["events"][0]["otel"].get("gen_ai.operation.name"), "chat")
        self.assertIn("error.type", events_doc["events"][1]["otel"])

    def test_pre_bump_events_json_still_grades(self):
        # Backward compatibility: a version-1 events.json (no otel keys) grades.
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            (base / "output.md").write_text("done", encoding="utf-8")
            (base / "events.json").write_text(json.dumps({
                "schema_version": 1, "source": "old",
                "events": [{"type": "command", "command": "python -m pytest -q", "status": "completed"}],
            }), encoding="utf-8")
            r = sb.assertion_result({"type": "command_ran", "pattern": "pytest"}, "done", base / "output.md", run_base=base)
        self.assertTrue(r["passed"])


class SubagentRunnerTests(unittest.TestCase):
    """2.7 — the built-in subagent runner writes the run-output contract."""

    def make_tasks(self, root: Path) -> list[dict]:
        path = write_manifest(root, base_manifest())
        manifest = sb.validate_manifest(path)
        return sb.prepared_task_rows(path, manifest, split="tune")

    def test_mock_subagent_writes_the_contract(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            tasks = self.make_tasks(root)
            runs = root / "runs"
            seen_prompts: list[str] = []

            def agent(*, prompt, workspace, model, tool_executor):
                seen_prompts.append(prompt)
                return {"answer": "alpha response", "usage": {"total_tokens": 42},
                        "trace": [{"type": "command", "command": "ls", "status": "completed"}]}

            rc = sb.run_subagent_tasks(tasks, runs, agent, model="sub-model")
            self.assertEqual(rc, 0)
            for variant in ["with_skill", "without_skill"]:
                base = runs / "case-1" / variant
                self.assertEqual((base / "output.md").read_text(encoding="utf-8"), "alpha response")
                meta = json.loads((base / "metadata.json").read_text(encoding="utf-8"))
                self.assertEqual(meta["provider"], "subagent")
                self.assertEqual(meta["model"], "sub-model")
                events = json.loads((base / "events.json").read_text(encoding="utf-8"))
                self.assertEqual(events["source"], "subagent")
                metrics = json.loads((base / "metrics.json").read_text(encoding="utf-8"))
                self.assertEqual(metrics["total_tokens"], 42)
        without_prompt = next(p for p in seen_prompts if "Do not use any skill" in p)
        self.assertNotIn("skills/", without_prompt)

    def test_subagent_failure_writes_failure_marker(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            tasks = self.make_tasks(root)[:1]

            def exploding(*, prompt, workspace, model, tool_executor):
                raise RuntimeError("backend down")

            sb.run_subagent_tasks(tasks, root / "runs", exploding)
            base = root / "runs" / "case-1" / "with_skill"
            self.assertIn(str(sb.CLAUDE_FAILURE), (base / "output.md").read_text(encoding="utf-8"))
            meta = json.loads((base / "metadata.json").read_text(encoding="utf-8"))
            self.assertEqual(meta["returncode"], 1)

    def test_subagent_is_a_registered_workspace_builder(self):
        self.assertIn("subagent", sb.WORKSPACE_BUILDERS)


class ToolReplayTests(unittest.TestCase):
    """2.3 — record/replay of tool I/O for deterministic re-runs."""

    def agent_using_tools(self, replies: list):
        def agent(*, prompt, workspace, model, tool_executor):
            a = tool_executor("search", {"q": "alpha"})
            b = tool_executor("search", {"q": "beta"})
            replies.append((a, b))
            return {"answer": f"{a} then {b}"}
        return agent

    def test_record_then_replay_round_trip_is_identical(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            tasks = SubagentRunnerTests().make_tasks(root)[:1]
            runs = root / "runs"
            live_calls = {"n": 0}

            def live(payload):
                live_calls["n"] += 1
                return f"live-{payload['q']}-{live_calls['n']}"

            sb.run_subagent_tasks(tasks, runs, self.agent_using_tools([]), live_tools={"search": live}, replay_mode="record")
            base = runs / "case-1" / "with_skill"
            first = (base / "output.md").read_text(encoding="utf-8")
            self.assertTrue((base / "tool-replay.json").is_file())
            self.assertEqual(live_calls["n"], 2)

            def poisoned(payload):
                raise AssertionError("replay must not hit the live tool")

            sb.run_subagent_tasks(tasks, runs, self.agent_using_tools([]), live_tools={"search": poisoned}, replay_mode="replay")
            second = (base / "output.md").read_text(encoding="utf-8")
        self.assertEqual(first, second)
        self.assertEqual(first, "live-alpha-1 then live-beta-2")

    def test_strict_errors_on_unrecorded_tool_call(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            tasks = SubagentRunnerTests().make_tasks(root)[:1]
            runs = root / "runs"
            sb.run_subagent_tasks(tasks, runs, self.agent_using_tools([]), replay_mode="strict")
            output = (runs / "case-1" / "with_skill" / "output.md").read_text(encoding="utf-8")
        self.assertIn("tool replay miss", output)

    def test_auto_mode_records_then_replays(self):
        with tempfile.TemporaryDirectory() as td:
            store_path = Path(td) / "tool-replay.json"
            store = sb.ToolReplayStore(store_path, "auto")
            self.assertEqual(store.mode, "record")
            store.resolve("t", {"x": 1}, live=lambda p: "out")
            store.save()
            replayer = sb.ToolReplayStore(store_path, "auto")
            self.assertEqual(replayer.mode, "replay")
            self.assertEqual(replayer.resolve("t", {"x": 1}), "out")


class HeldOutRubricTests(unittest.TestCase):
    """2.7b — held-out grading criteria stay invisible to generation."""

    RUBRIC = "Uses a concrete counter-example when rejecting a design"

    def manifest_with_holdout(self, leak_into_skill: bool = False) -> dict:
        manifest = base_manifest()
        manifest["cases"].append({
            "id": "held-1", "split": "holdout", "kind": "behavior", "prompt": "Review the design.",
            "review_rubric": [self.RUBRIC],
            "assertions": [{"name": "quality", "type": "judge", "rubric": [self.RUBRIC]}],
        })
        return manifest

    def test_audit_flags_rubric_leaked_into_skill(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            path = write_manifest(root, self.manifest_with_holdout())
            skill = path.parent.parent / "skill" / "SKILL.md"
            skill.write_text(f"---\nname: demo\ndescription: Demo\n---\nAlways: {self.RUBRIC}\n", encoding="utf-8")
            report = sb.audit_manifest_report(path)
        kinds = [f["kind"] for f in report["findings"]]
        self.assertIn("held-out-rubric-leak", kinds)

    def test_audit_silent_when_rubric_stays_hidden(self):
        with tempfile.TemporaryDirectory() as td:
            path = write_manifest(Path(td), self.manifest_with_holdout())
            report = sb.audit_manifest_report(path)
        kinds = [f["kind"] for f in report["findings"]]
        self.assertNotIn("held-out-rubric-leak", kinds)

    def test_generation_payload_never_carries_held_out_rubric(self):
        with tempfile.TemporaryDirectory() as td:
            path = write_manifest(Path(td), self.manifest_with_holdout())
            manifest = sb.validate_manifest(path)
            rows = sb.prepared_task_rows(path, manifest)
        for row in rows:
            self.assertNotIn(self.RUBRIC, json.dumps(row))

    def test_report_separates_held_out_from_tune_visible(self):
        results = [
            {"case_id": "t", "variant": "with_skill", "split": "tune", "missing_output": False, "execution_valid": True,
             "qualitative_total": 1, "qualitative_pass_rate": 1.0, "graded_score": 0.9, "metadata": {}},
            {"case_id": "h", "variant": "with_skill", "split": "holdout", "missing_output": False, "execution_valid": True,
             "qualitative_total": 1, "qualitative_pass_rate": 0.0, "graded_score": 0.4, "metadata": {}},
        ]
        visibility = sb.qualitative_by_visibility(results)
        self.assertEqual(visibility["tune_visible"]["mean_qualitative_pass_rate"], 1.0)
        self.assertEqual(visibility["held_out"]["mean_qualitative_pass_rate"], 0.0)
        self.assertEqual(visibility["held_out"]["mean_graded_score"], 0.4)


class GuideHintTests(unittest.TestCase):
    """1.5 follow-on — the authoring guide's rules surface where checkable."""

    def test_leakage_finding_points_at_the_guide(self):
        with tempfile.TemporaryDirectory() as td:
            manifest = base_manifest()
            manifest["cases"][0]["prompt"] = "Please mention alpha in your answer."
            path = write_manifest(Path(td), manifest)
            findings = sb.prompt_assertion_leakage_findings(manifest, path)
        self.assertTrue(findings)
        self.assertIn("docs/authoring-evals.md", findings[0]["guide"])

    def test_fixture_recommendations_point_at_the_guide(self):
        recs = sb.fixture_recommendations(base_manifest())
        self.assertTrue(recs)
        self.assertTrue(all("docs/authoring-evals.md" in r["guide"] for r in recs))


if __name__ == "__main__":
    unittest.main()
