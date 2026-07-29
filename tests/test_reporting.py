"""Benchmark report views: formats, per-model analysis, trend/staleness, token-overhead, viewer artifacts.

Classes moved verbatim from the PR-named test files (test_audit_fixes,
test_roadmap_features, test_followup_features, test_external_review_gaps,
test_cbc) and test_skill_benchmark, which accreted by merge rather than by
subject; docstrings citing finding/roadmap ids are preserved.
"""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from helpers import (
    CODEX_CRASH_OUTPUT as CRASH,
)
from helpers import (
    CONTAINS_APPROVED_CASE as CASE,
)
from helpers import (
    demo_manifest as base_manifest,
)
from helpers import (
    good_pr_manifest as _manifest,
)
from helpers import (
    report_fixture,
    result_row,
    trace_event,
    write_run,
)
from helpers import (
    write_demo_manifest as write_manifest,
)
from helpers import (
    write_good_pr_skill as _skill,
)

import skill_benchmark as sb

ROOT = Path(__file__).resolve().parents[1]


def make_two_model_runs(root: Path) -> tuple[Path, Path]:
    """A two-model run tree: m1 lifts (with passes, without fails); m2 is flat.
    Module-level so sibling test classes never instantiate a TestCase to borrow it."""
    path = write_manifest(root, base_manifest())
    runs = root / "runs"
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

    def test_report_groups_by_model_and_pairs_lift_per_model(self):
        with tempfile.TemporaryDirectory() as td:
            path, runs = make_two_model_runs(Path(td))
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


class PerModelAnalysisTests(unittest.TestCase):
    """3.2 — model ranking, lift losers, slice-lift concentration."""

    def test_model_ranking_and_losers(self):
        paired = {
            "absolute_delta": 0.4,
            "by_model": {
                "m-good": {"absolute_delta": 0.8, "with_skill_objective_pass_rate": 0.9, "without_skill_objective_pass_rate": 0.1,
                           "significance": {"significant_at_0_05": True}},
                "m-flat": {"absolute_delta": 0.0, "with_skill_objective_pass_rate": 0.5, "without_skill_objective_pass_rate": 0.5,
                           "significance": {"significant_at_0_05": False}},
            },
        }
        analysis = sb.model_analysis_from_paired(paired)
        self.assertEqual([r["model"] for r in analysis["ranking"]], ["m-good", "m-flat"])
        self.assertTrue(analysis["ranking"][0]["significant_at_0_05"])
        self.assertEqual(analysis["lift_losers"], ["m-flat"])

    def test_unmatched_models_cannot_produce_slice_lift(self):
        base = {"case_id": "c", "run_number": 1, "domain": "docs",
                "missing_output": False, "execution_valid": True, "metadata": {}}
        rows = [
            {**base, "variant": "with_skill", "model": "a", "objective_pass_rate": 0.0},
            {**base, "variant": "without_skill", "model": "b", "objective_pass_rate": 1.0},
        ]
        domain = sb.build_slice_summary(rows, ["with_skill", "without_skill"])["domain"]["docs"]
        self.assertNotIn("lift", domain)
        self.assertEqual(domain["pairing"]["eligible_pairs"], 0)
        self.assertEqual(domain["pairing"]["blocked_pairs"], 2)

    def test_no_model_axis_yields_empty_analysis(self):
        self.assertEqual(sb.model_analysis_from_paired({"absolute_delta": 0.5}), {})

    def test_slice_lift_concentration(self):
        results = []
        for case_id, domain, w, n in [("c1", "docs", 1.0, 0.0), ("c2", "testing", 0.5, 0.5)]:
            for variant, rate in [("with_skill", w), ("without_skill", n)]:
                results.append({"case_id": case_id, "variant": variant, "run_number": 1,
                                "domain": domain, "missing_output": False,
                                "execution_valid": True, "objective_pass_rate": rate, "metadata": {}})
        summary = sb.build_slice_summary(results, ["with_skill", "without_skill"])
        docs = summary["domain"]["docs"]
        testing = summary["domain"]["testing"]
        self.assertEqual(docs["lift"], 1.0)
        self.assertEqual(testing["lift"], 0.0)
        # Overall lift = 0.5, so the docs slice concentrates it at 2x.
        self.assertEqual(docs["lift_concentration"], 2.0)
        self.assertEqual(testing["lift_concentration"], 0.0)

    def test_benchmark_report_carries_model_analysis(self):
        with tempfile.TemporaryDirectory() as td:
            path, runs = make_two_model_runs(Path(td))
            report = sb.build_benchmark_report(path, runs)
        self.assertEqual([r["model"] for r in report["model_analysis"]["ranking"]], ["m1", "m2"])
        self.assertEqual(report["model_analysis"]["lift_losers"], ["m2"])


class ServedReportArtifactTests(unittest.TestCase):
    """2.8 — artifact embedding/categorization and the feedback round trip."""

    def test_artifact_categorization(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            png = root / "chart.png"
            png.write_bytes(b"\x89PNG\r\n\x1a\nfakebytes")
            pdf = root / "report.pdf"
            pdf.write_bytes(b"%PDF-1.4 fake")
            xlsx = root / "data.xlsx"
            xlsx.write_bytes(b"PK fake")
            note = root / "notes.md"
            note.write_text("hello <world>", encoding="utf-8")
            self.assertEqual(sb.encode_artifact(png)["kind"], "image")
            self.assertIn("data:image/png;base64,", sb.encode_artifact(png)["html"])
            self.assertEqual(sb.encode_artifact(pdf)["kind"], "pdf")
            self.assertEqual(sb.encode_artifact(xlsx)["kind"], "spreadsheet")
            text_artifact = sb.encode_artifact(note)
            self.assertEqual(text_artifact["kind"], "text")
            self.assertIn("&lt;world&gt;", text_artifact["html"])

    def test_viewer_html_embeds_artifacts_and_landmarks(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td) / "runs" / "case-1" / "with_skill"
            (base / "outputs").mkdir(parents=True)
            (base / "output.md").write_text("alpha", encoding="utf-8")
            (base / "outputs" / "picture.png").write_bytes(b"tinypng")
            report = {
                "generated_at": 1, "summary": {"with_skill": {"runs": 1}},
                "paired_summary": {"absolute_delta": 1.0},
                "results": [{"case_id": "case-1", "variant": "with_skill", "run_number": 1,
                             "objective_pass_rate": 1.0, "run_base": str(base), "assertions": [], "qualitative_assertions": []}],
            }
            html_text = sb.viewer_html(report)
        self.assertIn("Skill Eval Review", html_text)
        self.assertIn("Paired lift", html_text)
        self.assertIn("data:image/png;base64,", html_text)

    def test_serve_mode_adds_feedback_form_and_static_does_not(self):
        report = {"generated_at": 1, "summary": {}, "results": []}
        self.assertIn("/feedback", sb.viewer_html(report, serve_mode=True))
        self.assertNotIn("/feedback", sb.viewer_html(report, serve_mode=False))

    def test_feedback_round_trip_replaces_by_key(self):
        with tempfile.TemporaryDirectory() as td:
            ws = Path(td)
            sb.persist_feedback(ws, {"case_id": "c1", "variant": "with_skill", "verdict": "bad", "note": "meh"})
            sb.persist_feedback(ws, {"case_id": "c2", "variant": "with_skill", "verdict": "good"})
            sb.persist_feedback(ws, {"case_id": "c1", "variant": "with_skill", "verdict": "good", "note": "fixed"})
            doc = json.loads((ws / "feedback.json").read_text(encoding="utf-8"))
        self.assertEqual(len(doc["entries"]), 2)
        c1 = next(e for e in doc["entries"] if e["case_id"] == "c1")
        self.assertEqual(c1["verdict"], "good")


class IterationWorkflowTests(unittest.TestCase):
    """2.9 — iteration-N convention and the previous-workspace diff."""

    def test_iteration_dir_helpers(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self.assertEqual(sb.next_iteration_dir(root).name, "iteration-1")
            (root / "iteration-1").mkdir()
            (root / "iteration-3").mkdir()
            (root / "not-an-iteration").mkdir()
            self.assertEqual([p.name for p in sb.iteration_dirs(root)], ["iteration-1", "iteration-3"])
            self.assertEqual(sb.next_iteration_dir(root).name, "iteration-4")

    def test_benchmark_report_diff(self):
        previous = {
            "summary": {"with_skill": {"mean_objective_pass_rate": 0.5, "mean_combined_pass_rate": 0.5}},
            "results": [{"case_id": "c1", "variant": "with_skill", "objective_pass_rate": 0.5}],
            "case_flags": [{"case_id": "c1", "flags": ["no objective lift"]}],
        }
        current = {
            "summary": {"with_skill": {"mean_objective_pass_rate": 1.0, "mean_combined_pass_rate": 1.0}},
            "results": [{"case_id": "c1", "variant": "with_skill", "objective_pass_rate": 1.0},
                        {"case_id": "c2", "variant": "with_skill", "objective_pass_rate": 0.0}],
            "case_flags": [{"case_id": "c2", "flags": ["with-skill failure"]}],
        }
        diff = sb.benchmark_report_diff(previous, current)
        self.assertEqual(diff["variant_deltas"]["with_skill"]["mean_objective_pass_rate"]["delta"], 0.5)
        self.assertEqual(diff["case_deltas"], [{"case_id": "c1", "variant": "with_skill", "before": 0.5, "after": 1.0, "delta": 0.5}])
        self.assertEqual(diff["new_flags"], ["c2::with-skill failure"])
        self.assertEqual(diff["resolved_flags"], ["c1::no objective lift"])

    def test_viewer_embeds_diff_for_previous_workspace(self):
        report = {"generated_at": 1, "summary": {}, "results": []}
        previous = {"summary": {}, "results": [], "case_flags": [{"case_id": "c", "flags": ["flaky repeated pass rates: with_skill"]}]}
        html_text = sb.viewer_html(report, previous_report=previous)
        self.assertIn("Diff vs previous workspace", html_text)
        self.assertIn("resolved_flags", html_text)


class TrendTrackingTests(unittest.TestCase):
    """2.6 — history store, series, diffs, severity-weighted ranking."""

    def test_history_append_and_ordering(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            history = root / "history"
            for i, rates in enumerate([{"c1": (0.5, 0.5)}, {"c1": (1.0, 0.5)}], 1):
                report_path = root / f"b{i}.json"
                report_path.write_text(json.dumps(report_fixture(rates)), encoding="utf-8")
                sb.append_history_report(history, report_path)
            entries = sb.load_history_reports(history)
            self.assertEqual([label for label, _ in entries], ["run-001.json", "run-002.json"])
            trend_report = sb.build_trend_report(entries)
        self.assertEqual(trend_report["runs"], 2)
        self.assertEqual(trend_report["series"][0]["lift"], 0.0)
        self.assertEqual(trend_report["series"][1]["lift"], 0.5)
        self.assertEqual(len(trend_report["diffs"]), 1)
        self.assertTrue(trend_report["diffs"][0]["diff"]["case_deltas"])

    def test_severity_weighted_ranking(self):
        # A critical failure in 1 of 2 runs (0.5 x 3 = 1.5) outranks a soft
        # failure in 2 of 2 runs (1.0 x 1 = 1.0).
        fail_critical = {"case_id": "c-crit", "variant": "with_skill", "missing_output": False, "execution_valid": True,
                         "objective_pass_rate": 0.0, "metadata": {},
                         "assertions": [{"name": "guard", "passed": False, "severity": "critical"}], "qualitative_assertions": []}
        fail_soft = {"case_id": "c-soft", "variant": "with_skill", "missing_output": False, "execution_valid": True,
                     "objective_pass_rate": 1.0, "metadata": {},
                     "assertions": [{"name": "styling", "passed": False, "severity": "soft"}], "qualitative_assertions": []}
        run1 = {"results": [fail_critical, fail_soft]}
        run2 = {"results": [fail_soft]}
        ranked = sb.severity_weighted_failures([run1, run2])
        self.assertEqual(ranked[0]["assertion"], "guard")
        self.assertEqual(ranked[0]["rank"], 1.5)
        self.assertEqual(ranked[1]["assertion"], "styling")
        self.assertEqual(ranked[1]["rank"], 1.0)


class StalenessPruneTests(unittest.TestCase):
    """1.9 — flat-forever cases become prune candidates; one run never flags."""

    def test_always_flat_case_is_flagged_and_discriminating_case_is_not(self):
        flat_then_flat = [report_fixture({"c-flat": (1.0, 1.0), "c-live": (1.0, 0.0)}),
                          report_fixture({"c-flat": (1.0, 1.0), "c-live": (1.0, 1.0)})]
        candidates = sb.stale_case_candidates(flat_then_flat)
        self.assertEqual([c["case_id"] for c in candidates], ["c-flat"])
        self.assertEqual(candidates[0]["runs_observed"], 2)

    def test_single_run_never_flags(self):
        self.assertEqual(sb.stale_case_candidates([report_fixture({"c-flat": (1.0, 1.0)})]), [])

    def test_trend_report_carries_prune_candidates(self):
        entries = [("run-001.json", report_fixture({"c-flat": (1.0, 1.0)})),
                   ("run-002.json", report_fixture({"c-flat": (1.0, 1.0)}))]
        self.assertEqual(sb.build_trend_report(entries)["prune_candidates"][0]["case_id"], "c-flat")


class LivingEvalLoopTests(unittest.TestCase):
    """2.10 — flags become candidate seeds; generation is opt-in and mocked;
    a candidate never enters a manifest on its own."""

    def setup_repo(self, root: Path) -> tuple[Path, Path]:
        manifest = base_manifest()
        path = write_manifest(root, manifest)
        report = report_fixture({"case-1": (1.0, 1.0)})
        report_path = root / "benchmark.json"
        report_path.write_text(json.dumps(report), encoding="utf-8")
        return path, report_path

    def test_flag_to_candidate_selection_is_deterministic(self):
        with tempfile.TemporaryDirectory() as td:
            path, report_path = self.setup_repo(Path(td))
            report = json.loads(report_path.read_text(encoding="utf-8"))
            manifest = sb.validate_manifest(path)
            seeds = sb.suggest_case_candidates(report, manifest)
        self.assertEqual(len(seeds), 1)
        self.assertEqual(seeds[0]["case_id"], "case-1")
        self.assertIn("saturated/non-discriminating", seeds[0]["flags"])
        self.assertEqual(seeds[0]["prompt"], "Do the task.")

    def test_generation_is_mocked_and_manifest_untouched(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            path, report_path = self.setup_repo(root)
            manifest_bytes = path.read_bytes()
            out = root / "candidates.json"
            args = SimpleNamespace(
                benchmark=str(report_path), manifest=str(path),
                generate_cmd="python3 -c \"import sys,json; json.load(sys.stdin); print(json.dumps({'prompt': 'harder variant', 'rationale': 'raise difficulty'}))\"",
                timeout=30, out=str(out))
            rc = sb.suggest_cases(args)
            self.assertEqual(rc, 0)
            doc = json.loads(out.read_text(encoding="utf-8"))
            self.assertEqual(doc["candidates"][0]["generated"]["prompt"], "harder variant")
            self.assertIn("never edits", doc["note"])
            self.assertEqual(path.read_bytes(), manifest_bytes)   # manifest untouched

    def test_without_generate_cmd_candidates_are_seeds_only(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            path, report_path = self.setup_repo(root)
            out = root / "candidates.json"
            args = SimpleNamespace(benchmark=str(report_path), manifest=str(path), generate_cmd=None, timeout=30, out=str(out))
            sb.suggest_cases(args)
            doc = json.loads(out.read_text(encoding="utf-8"))
        self.assertNotIn("generated", doc["candidates"][0])
        self.assertIn("instruction", doc["candidates"][0])


class ErrorAnalysisTests(unittest.TestCase):
    def test_taxonomy_and_review_queue(self):
        report = {
            "results": [
                {"case_id": "c1", "variant": "with_skill", "objective_pass_rate": 1.0, "assertions": [{"name": "x", "passed": True}], "qualitative_assertions": []},
                {"case_id": "c1", "variant": "without_skill", "objective_pass_rate": 0.0, "assertions": [{"name": "detect-weak", "type": "contains", "passed": False, "evidence": "missing"}], "qualitative_assertions": []},
                {"case_id": "c2", "variant": "without_skill", "objective_pass_rate": 0.0, "assertions": [{"name": "detect-weak", "type": "contains", "passed": False, "evidence": "missing"}], "qualitative_assertions": []},
                {"case_id": "c3", "variant": "with_skill", "missing_output": True, "assertions": [], "qualitative_assertions": []},
            ],
            "case_flags": [{"case_id": "c1", "flags": ["saturated/non-discriminating", "flaky repeated pass rates: with_skill"]}],
        }
        out = sb.error_analysis_report(report)
        self.assertEqual(out["summary"]["failing_or_errored_runs"], 3)   # the passing run is not a datum
        top = out["taxonomy"][0]
        self.assertEqual(top["category"], "text:detect-weak")            # the dominant first-failure
        self.assertEqual(top["count"], 2)
        self.assertAlmostEqual(top["share"], 2 / 3, places=4)   # report rounds to 4dp
        self.assertIn("missing-output", {b["category"] for b in out["taxonomy"]})
        self.assertEqual(out["case_flag_histogram"]["saturated/non-discriminating"], 1)

    def test_execution_error_critical_and_judge_categories(self):
        report = {"results": [
            {"case_id": "c1", "variant": "with_skill", "execution_valid": False, "assertions": [], "qualitative_assertions": []},
            {"case_id": "c2", "variant": "with_skill", "vetoed": True, "critical_failures": ["wrote-outside-results"], "assertions": [], "qualitative_assertions": []},
            {"case_id": "c3", "variant": "with_skill", "objective_pass_rate": 1.0, "assertions": [{"name": "ok", "passed": True}],
             "qualitative_assertions": [{"name": "rubric", "type": "judge", "passed": False, "evidence": "weak"}]},
        ], "case_flags": []}
        out = sb.error_analysis_report(report)
        cats = {b["category"] for b in out["taxonomy"]}
        self.assertIn("execution-error", cats)
        self.assertIn("critical-failure:wrote-outside-results", cats)
        self.assertIn("judge:rubric", cats)   # a qualitative first-failure classifies as judge

    def test_review_queue_limit_truncates(self):
        report = {"results": [
            {"case_id": f"c{i}", "variant": "without_skill", "objective_pass_rate": 0.0,
             "assertions": [{"name": "x", "type": "contains", "passed": False}], "qualitative_assertions": []}
            for i in range(5)
        ], "case_flags": []}
        out = sb.error_analysis_report(report, limit=2)
        self.assertEqual(len(out["review_queue"]), 2)
        self.assertEqual(out["review_queue_truncated"], 3)
        self.assertEqual(out["summary"]["failing_or_errored_runs"], 5)   # taxonomy still counts all


class G1_TokenOverheadScorableTests(unittest.TestCase):
    """A crashed/timed-out arm must not be differenced as a skill effect; the
    paired token-overhead report excludes non-scorable pairs like every other view."""

    def test_crashed_arm_is_excluded_from_paired_overhead(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); rp = root / "repo"; _skill(rp)
            p = _manifest(rp, [CASE]); runs = root / "runs"
            write_run(runs / "c" / "with_skill", CRASH, metadata={"returncode": 1}, metrics={"total_tokens": 5000})
            write_run(runs / "c" / "without_skill", "APPROVED", metadata={"returncode": 0}, metrics={"total_tokens": 1000})
            rep = sb.paired_token_overhead_report(p, runs=runs)
            # Before the fix: the crashed with_skill arm graded 0.0 and the pair was
            # differenced -> objective_delta.mean == -1.0 ("the skill hurts accuracy").
            self.assertEqual(rep["summary"]["paired_runtime_rows"], 0)
            self.assertIsNone(rep["summary"]["objective_delta"]["mean"])


class G2_BenchmarkMetricsScorableTests(unittest.TestCase):
    """Per-variant timing/token central tendencies exclude infra-failed runs, the
    same scorable predicate the pass-rate block already uses."""

    def test_token_mean_excludes_infra_failed_runs(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); rp = root / "repo"; _skill(rp)
            p = _manifest(rp, [CASE]); runs = root / "runs"
            write_run(runs / "c" / "with_skill" / "run-1", "APPROVED", metadata={"returncode": 0}, metrics={"total_tokens": 1000})
            write_run(runs / "c" / "with_skill" / "run-2", CRASH, metadata={"returncode": 1}, metrics={"total_tokens": 5000})
            rep = sb.build_benchmark_report(p, runs, variants_arg=["with_skill"])
            s = rep["summary"]["with_skill"]
            self.assertEqual(s["total_tokens"]["mean"], 1000)        # was 3000 (timeout dragged it)
            self.assertEqual(s["median_total_tokens"], 1000)
            self.assertEqual(s["execution_errors"], 1)               # the failure is still disclosed


class TrajectoryDiffTests(unittest.TestCase):
    """The report's trajectory_diff block: per-case paired event-stream
    comparison showing HOW behavior differed between arms — commands only one
    arm ran, count deltas, skill-load rates — with missing trace evidence
    surfacing as blocked pairs, never as an empty diff."""

    def _events(self, commands, *, reads=0, skill=False):
        events = [trace_event("command", index=i + 1, name="bash", input_summary=cmd)
                  for i, cmd in enumerate(commands)]
        for n in range(reads):
            events.append(trace_event("file_read", index=len(events) + 1, name="Read",
                                      input_summary=f"notes-{n}.md"))
        if skill:
            events.append(trace_event("skill_load", index=len(events) + 1, name="Skill",
                                      input_summary="skills/demo/SKILL.md"))
        return {"schema_version": 2, "source": "test", "events": events}

    def _rows(self, td, *, with_events, without_events, exec_valid=True):
        rows = []
        for variant, events in (("with_skill", with_events), ("without_skill", without_events)):
            base = Path(td) / "c1" / variant / "run-1"
            write_run(base, "answer", events=events)
            rows.append(result_row("c1", variant, rate=1.0, exec_valid=exec_valid,
                                   run_number=1, run_base=str(base)))
        return rows

    def test_paired_diff_reports_command_and_count_deltas(self):
        with tempfile.TemporaryDirectory() as td:
            rows = self._rows(
                td,
                with_events=self._events(["npm install", "npm test"], reads=2, skill=True),
                without_events=self._events(["npm install"], reads=0))
            diff = sb.build_trajectory_diff(rows)
        self.assertEqual(diff["pairs_compared"], 1)
        case = diff["cases"][0]
        self.assertEqual(case["case_id"], "c1")
        self.assertEqual(case["commands_only_with_skill"], ["npm test"])
        self.assertEqual(case["commands_only_without_skill"], [])
        self.assertEqual(case["mean_deltas"]["commands"], 1.0)
        self.assertEqual(case["mean_deltas"]["file_reads"], 3.0)   # 2 reads + the skill load
        self.assertEqual(case["mean_deltas"]["steps"], 4.0)
        self.assertEqual(case["skill_invoked"], {"with_skill": 1.0, "without_skill": 0.0})

    def test_missing_trace_evidence_blocks_the_pair(self):
        with tempfile.TemporaryDirectory() as td:
            rows = self._rows(td, with_events=self._events(["ls"]), without_events=None)
            diff = sb.build_trajectory_diff(rows)
        self.assertEqual(diff["pairs_compared"], 0)
        self.assertEqual(diff["cases"], [])
        self.assertEqual(diff["pair_diagnostics"]["blocked_reason_counts"],
                         {"missing_trace_evidence": 1})

    def test_empty_events_are_missing_trace_evidence(self):
        empty = {"schema_version": 2, "source": "test", "events": []}
        with tempfile.TemporaryDirectory() as td:
            rows = self._rows(td, with_events=self._events(["ls"]), without_events=empty)
            diff = sb.build_trajectory_diff(rows)
        self.assertEqual(diff["pairs_compared"], 0)
        self.assertEqual(diff["pair_diagnostics"]["blocked_reason_counts"],
                         {"missing_trace_evidence": 1})

    def test_command_exclusivity_is_aggregated_across_repetitions(self):
        rows = []
        with tempfile.TemporaryDirectory() as td:
            for run_number, with_commands, without_commands in (
                    (1, ["command-a"], ["command-b"]),
                    (2, ["command-b"], ["command-a"])):
                for variant, commands in (("with_skill", with_commands),
                                          ("without_skill", without_commands)):
                    base = Path(td) / "c1" / variant / f"run-{run_number}"
                    write_run(base, "answer", events=self._events(commands))
                    rows.append(result_row(
                        "c1", variant, rate=1.0, run_number=run_number,
                        run_base=str(base)))
            diff = sb.build_trajectory_diff(rows)
        case = diff["cases"][0]
        self.assertEqual(case["commands_only_with_skill"], [])
        self.assertEqual(case["commands_only_without_skill"], [])

    def test_unscorable_run_blocks_the_pair(self):
        with tempfile.TemporaryDirectory() as td:
            rows = self._rows(td, with_events=self._events(["ls"]),
                              without_events=self._events(["ls"]), exec_valid=False)
            diff = sb.build_trajectory_diff(rows)
        self.assertEqual(diff["pairs_compared"], 0)
        self.assertEqual(diff["pair_diagnostics"]["blocked_reason_counts"],
                         {"unscorable_arm": 1})

    def test_benchmark_report_carries_the_section(self):
        with tempfile.TemporaryDirectory() as td:
            rp = Path(td) / "repo"
            p = _manifest(rp, [dict(CASE)])
            runs = Path(td) / "runs"
            for variant, cmds in (("with_skill", ["npm test"]), ("without_skill", [])):
                events = (self._events(cmds) if cmds else
                          {"schema_version": 2, "source": "test", "events": [
                              trace_event("message", role="assistant", input_summary="done")]})
                write_run(runs / "c" / variant, "APPROVED", metadata={},
                          events=events)
            report = sb.build_benchmark_report(p, runs, split="tune",
                                               variants_arg=["with_skill", "without_skill"])
        diff = report["trajectory_diff"]
        self.assertEqual(diff["pairs_compared"], 1)
        self.assertEqual(diff["cases"][0]["commands_only_with_skill"], ["npm test"])


if __name__ == "__main__":
    unittest.main()
