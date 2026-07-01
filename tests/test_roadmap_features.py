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
