"""Manifest shape, datasets, migration, leakage/readiness, and eval-hygiene audits.

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


class D3_TriggerPolarityTests(unittest.TestCase):
    """One resolver for 'does this trigger case expect the skill to fire?', consumed
    by both the autonomous-trigger eval and the manifest audit, so they cannot
    disagree on a prose-authored case."""

    POS = {"id": "t1", "kind": "trigger", "split": "tune", "prompt": "q1",
           "expected_behavior": ["the skill should trigger here"],
           "assertions": [{"name": "a", "type": "contains", "value": "ok"}]}
    NEG = {"id": "t2", "kind": "trigger", "split": "tune", "prompt": "q2",
           "expected_behavior": ["the skill should not fire"],
           "assertions": [{"name": "b", "type": "contains", "value": "ok"}]}

    def test_resolver_classifies_prose(self):
        self.assertEqual(sb.expected_trigger_polarity(self.POS), "TRIGGER")
        self.assertEqual(sb.expected_trigger_polarity(self.NEG), "NO_TRIGGER")

    def test_eval_and_resolver_agree(self):
        manifest = {"skill_name": "good-pr", "cases": [self.POS, self.NEG]}
        rows = {r["query"]: r["should_trigger"] for r in tr.cases_from_manifest(manifest, None)}
        self.assertTrue(rows[tr.trigger_query_from_case(self.POS)])
        self.assertFalse(rows[tr.trigger_query_from_case(self.NEG)])

    def test_audit_classifies_every_trigger_case(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); rp = root / "repo"; _skill(rp)
            p = _manifest(rp, [self.POS, self.NEG])
            rep = sb.audit_manifest_report(p)
            c = rep["counts"]
            # No prose case dropped as 'unknown polarity': positive + negative == total.
            self.assertEqual(c["trigger"], 2)
            self.assertEqual(c["trigger_positive"] + c["trigger_negative"], 2)
            self.assertEqual(c["trigger_positive"], 1)
            self.assertEqual(c["trigger_negative"], 1)


class InstructionSimulatedAblationAuditTests(unittest.TestCase):
    """The migration lever: audit-manifest flags every label-only
    (instruction-simulated) ablation so its non-blind, raw-measurement-only status
    is visible per-manifest, and names how to materialize it. A materialized
    ablation (mechanism+target) is silent — it is already the blind,
    confirmation-gradeable form the migration targets."""

    def test_label_only_ablation_is_flagged_with_remediation(self):
        with tempfile.TemporaryDirectory() as td:
            rp = Path(td) / "repo"; _skill(rp)
            p = _manifest(rp, [CASE], ablations=[
                {"id": "no-sev", "removed_component": "severity rules",
                 "expected_regressions": ["loses Clean/Minor/Blocking calibration"]}])
            rep = sb.audit_manifest_report(p)
            f = next((f for f in rep["findings"] if f["kind"] == "ablation-instruction-simulated"), None)
            self.assertIsNotNone(f, "label-only ablation must be flagged for migration")
            self.assertIn("no-sev", f["message"])
            self.assertIn("mechanism", f["message"])   # remediation names how to materialize

    def test_materialized_ablation_is_not_flagged(self):
        with tempfile.TemporaryDirectory() as td:
            rp = Path(td) / "repo"; _skill(rp)
            p = _manifest(rp, [CASE], ablations=[
                {"id": "no-sev", "removed_component": "severity",
                 "mechanism": "section", "target": {"heading": "## Sev"},
                 "expected_regressions": [{"summary": "x", "cases": ["c"], "assertions": ["a"]}]}])
            rep = sb.audit_manifest_report(p)
            kinds = {f["kind"] for f in rep["findings"]}
            self.assertNotIn("ablation-instruction-simulated", kinds)


class EvalReadinessTests(unittest.TestCase):
    """audit-manifest emits a compact 'is this eval worth paying to run' verdict:
    are the ablations real (materialized), does any case leak its whole answer into
    the prompt, is there adversarial coverage. It turns the scattered findings into a
    gate you can drive to green before spending model budget."""

    def test_blockers_flag_instruction_simulated_leak_and_no_adversarial(self):
        with tempfile.TemporaryDirectory() as td:
            rp = Path(td) / "repo"; _skill(rp)
            cases = [{"id": "c1", "split": "tune", "kind": "positive",
                      "prompt": "Please label this Blocking and move on.",
                      "assertions": [{"name": "sev", "type": "contains", "value": "Blocking"}]}]
            p = _manifest(rp, cases, ablations=[{"id": "no-x", "removed_component": "x", "expected_regressions": ["y"]}])
            r = sb.audit_manifest_report(p)["readiness"]
            self.assertEqual(r["ablations"]["instruction_simulated"], 1)
            self.assertIn("c1", r["leak_saturated_cases"])         # the only positive assertion's value is in the prompt
            self.assertEqual(r["adversarial_cases"], 0)
            self.assertTrue(any("instruction-simulated" in b for b in r["blockers"]))
            self.assertTrue(any("leak-saturated" in b for b in r["blockers"]))
            self.assertTrue(any("adversarial" in b for b in r["blockers"]))

    def test_unverifiable_positive_assertion_blocks_leak_saturation(self):
        # A case with a leaked `contains` AND a `regex` (whose leakage the lint cannot
        # verify) must NOT be reported leak-saturated — leak-checkability is defined by
        # the leakage lint, so we never over-report a case as non-discriminating.
        with tempfile.TemporaryDirectory() as td:
            rp = Path(td) / "repo"; _skill(rp)
            cases = [{"id": "c1", "split": "tune", "kind": "positive", "prompt": "please label Blocking here",
                      "assertions": [{"name": "sev", "type": "contains", "value": "Blocking"},
                                     {"name": "shape", "type": "regex", "pattern": "^Severity:"}]}]
            r = sb.audit_manifest_report(_manifest(rp, cases, ablations=[]))["readiness"]
            self.assertEqual(r["leak_saturated_cases"], [])

    def _audit_ns(self, manifest_path, **over):
        import argparse
        base = dict(manifest=str(manifest_path), skill_path=None, runs=None, split=None,
                    format="json", out=None, min_positive=5, min_negative=3, min_adversarial=3,
                    min_trigger_pos=2, min_trigger_neg=2, leakage_min_chars=4, fail_on_blockers=False)
        base.update(over)
        return argparse.Namespace(**base)

    def test_fail_on_blockers_gates_on_readiness(self):
        with tempfile.TemporaryDirectory() as td:
            rb = Path(td) / "bad"; _skill(rb)
            bad = _manifest(rb, [CASE], ablations=[{"id": "x", "removed_component": "x", "expected_regressions": ["y"]}])
            self.assertEqual(sb.audit_manifest(self._audit_ns(bad, out=str(rb / "o.json"), fail_on_blockers=True)), 1)
            self.assertEqual(sb.audit_manifest(self._audit_ns(bad, out=str(rb / "o.json"))), 0)   # off by default
            rc = Path(td) / "clean"; _skill(rc)
            cases = [{"id": "a1", "split": "tune", "kind": "adversarial", "prompt": "a tricky near-miss to handle with care",
                      "assertions": [{"name": "k", "type": "contains", "value": "token-not-in-the-prompt"}]}]
            ab = {"id": "no-sev", "removed_component": "sev", "mechanism": "section", "class": "instructions",
                  "target": {"heading": "## Sev"}, "expected_regressions": [{"summary": "x", "cases": ["a1"], "assertions": ["k"]}]}
            clean = _manifest(rc, cases, ablations=[ab])
            self.assertEqual(sb.audit_manifest(self._audit_ns(clean, out=str(rc / "o.json"), fail_on_blockers=True)), 0)

    def test_clean_manifest_has_no_blockers(self):
        with tempfile.TemporaryDirectory() as td:
            rp = Path(td) / "repo"; _skill(rp)   # SKILL.md has a '## Sev' section
            cases = [{"id": "a1", "split": "tune", "kind": "adversarial",
                      "prompt": "A tricky near-miss that should be handled with care.",
                      "assertions": [{"name": "k", "type": "contains", "value": "token-not-in-the-prompt"}]}]
            ab = {"id": "no-sev", "removed_component": "sev", "mechanism": "section", "class": "instructions",
                  "target": {"heading": "## Sev"}, "expected_regressions": [{"summary": "x", "cases": ["a1"], "assertions": ["k"]}]}
            r = sb.audit_manifest_report(_manifest(rp, cases, ablations=[ab]))["readiness"]
            self.assertEqual(r["ablations"]["instruction_simulated"], 0)
            self.assertEqual(r["leak_saturated_cases"], [])
            self.assertGreaterEqual(r["adversarial_cases"], 1)
            self.assertEqual(r["blockers"], [])


class ReadinessRunSignalTests(unittest.TestCase):
    """eval-readiness gains two MEASURED signals (need run data, not just the
    manifest): base_saturated (with==without: measures nothing) and
    qualitative_only (objective flat but combined lifts: the judge carries the whole
    signal — the anti-slop case an objective-only eval would miss)."""

    def _res(self, cid, variant, obj, comb):
        return {"case_id": cid, "variant": variant, "run_number": 1,
                "objective_pass_rate": obj, "combined_pass_rate": comb,
                "missing_output": False, "execution_valid": True}

    def test_run_signals_classify_cases(self):
        report = {"results": [
            # base-saturated: combined identical across arms
            self._res("base", "with_skill", 1.0, 1.0), self._res("base", "without_skill", 1.0, 1.0),
            # qualitative-only: objective identical, combined lifts with_skill
            self._res("qual", "with_skill", 0.5, 0.9), self._res("qual", "without_skill", 0.5, 0.6),
            # genuine objective lift: neither flag
            self._res("real", "with_skill", 1.0, 1.0), self._res("real", "without_skill", 0.5, 0.5),
        ]}
        sig = sb.readiness_run_signals(report)
        self.assertEqual(sig["base_saturated_cases"], ["base"])
        self.assertEqual(sig["qualitative_only_cases"], ["qual"])

    def test_unmatched_models_do_not_create_readiness_comparisons(self):
        left = {**self._res("c", "with_skill", 1.0, 1.0), "model": "a"}
        right = {**self._res("c", "without_skill", 1.0, 1.0), "model": "b"}
        signals = sb.readiness_run_signals({"results": [left, right]})
        self.assertEqual(signals["base_saturated_cases"], [])
        self.assertEqual(signals["qualitative_only_cases"], [])

    def test_objective_only_is_static_and_base_saturated_blocks(self):
        with tempfile.TemporaryDirectory() as td:
            rp = Path(td) / "repo"; _skill(rp)
            cases = [{"id": "obj", "split": "tune", "kind": "pr-review", "prompt": "review this",
                      "assertions": [{"name": "k", "type": "contains", "value": "TOKEN-NOT-IN-PROMPT"}]},
                     {"id": "adv", "split": "tune", "kind": "adversarial", "prompt": "tricky near-miss to hold",
                      "assertions": [{"name": "q", "type": "judge", "prompt": "held?"}]}]
            p = _manifest(rp, cases)
            # static: the objective-only positive case is flagged, the judge case isn't
            r = sb.eval_readiness(sb.validate_manifest(p), p)
            self.assertIn("obj", r["objective_only_cases"])
            self.assertNotIn("adv", r["objective_only_cases"])
            self.assertEqual(r["base_saturated_cases"], [])            # no run data => empty
            # with run data showing obj is base-saturated, it becomes a blocker
            bench = {"results": [
                {"case_id": "obj", "variant": "with_skill", "run_number": 1,
                 "objective_pass_rate": 1.0, "combined_pass_rate": 1.0,
                 "missing_output": False, "execution_valid": True},
                {"case_id": "obj", "variant": "without_skill", "run_number": 1,
                 "objective_pass_rate": 1.0, "combined_pass_rate": 1.0,
                 "missing_output": False, "execution_valid": True}]}
            r2 = sb.eval_readiness(sb.validate_manifest(p), p, benchmark_report=bench)
            self.assertEqual(r2["base_saturated_cases"], ["obj"])
            self.assertTrue(any("base-saturated" in b for b in r2["blockers"]))


class DatasetAbstractionTests(unittest.TestCase):
    """2.5 — one case template fanned over a row set, materialized early."""

    def dataset_manifest(self) -> dict:
        manifest = base_manifest()
        manifest["datasets"] = {
            "cities": [
                {"id": "paris", "city": "Paris", "river": "Seine"},
                {"id": "cairo", "city": "Cairo", "river": "Nile"},
                {"city": "Rome", "river": "Tiber"},
            ],
        }
        manifest["cases"] = [{
            "id": "river-of",
            "split": "tune",
            "kind": "behavior",
            "template": "cities",
            "prompt": "Name the river of {city}.",
            "assertions": [{"name": "names-river", "type": "contains", "value": "{river}"}],
        }]
        return manifest

    def test_rows_materialize_into_stable_case_ids(self):
        with tempfile.TemporaryDirectory() as td:
            path = write_manifest(Path(td), self.dataset_manifest())
            manifest = sb.validate_manifest(path)
            cases = sb.iter_cases(manifest)
        self.assertEqual([c["id"] for c in cases], ["river-of-paris", "river-of-cairo", "river-of-3"])
        self.assertEqual(cases[0]["prompt"], "Name the river of Paris.")
        self.assertEqual(cases[0]["assertions"][0]["value"], "Seine")
        self.assertEqual(cases[1]["dataset"], "cities")
        self.assertTrue(all("template" not in c for c in cases))

    def test_materialized_cases_fan_out_and_grade(self):
        with tempfile.TemporaryDirectory() as td:
            path = write_manifest(Path(td), self.dataset_manifest())
            manifest = sb.validate_manifest(path)
            rows = sb.prepared_task_rows(path, manifest, split="tune")
        self.assertEqual(len(rows), 3 * 2)   # 3 materialized cases x 2 variants
        self.assertIn("river-of-cairo/with_skill", {r["run_dir"] for r in rows})

    def test_unknown_dataset_dies(self):
        manifest = self.dataset_manifest()
        manifest["cases"][0]["template"] = "ghost"
        with tempfile.TemporaryDirectory() as td:
            path = write_manifest(Path(td), manifest)
            with self.assertRaises(SystemExit):
                sb.validate_manifest(path)

    def test_leakage_lint_fires_per_materialized_case(self):
        manifest = self.dataset_manifest()
        manifest["cases"][0]["prompt"] = "Say {river} for {city}."   # leaks the asserted value
        with tempfile.TemporaryDirectory() as td:
            path = write_manifest(Path(td), manifest)
            loaded = sb.validate_manifest(path)
            findings = sb.prompt_assertion_leakage_findings(loaded, path)
        self.assertEqual({f["case_id"] for f in findings}, {"river-of-paris", "river-of-cairo", "river-of-3"})

    def test_regex_braces_survive_substitution(self):
        row = {"city": "Paris"}
        value = sb.apply_dataset_row("match {city} with \\d{2,4} and {output_dir}", row)
        self.assertEqual(value, "match Paris with \\d{2,4} and {output_dir}")


class NoCodeRegistryTests(unittest.TestCase):
    """3.3 — YAML + JSONL definitions compile to a manifest before validation."""

    def write_yaml_repo(self, root: Path, prompt: str = "Name the river of {city}.") -> Path:
        repo = root / "repo"
        (repo / "skill").mkdir(parents=True)
        (repo / "skill" / "SKILL.md").write_text("---\nname: demo\ndescription: Demo\n---\n", encoding="utf-8")
        (repo / "evals").mkdir()
        (repo / "evals" / "rows.jsonl").write_text(
            '{"id": "paris", "city": "Paris", "river": "Seine"}\n{"id": "cairo", "city": "Cairo", "river": "Nile"}\n',
            encoding="utf-8")
        (repo / "evals" / "eval.yaml").write_text(
            "version: 1\n"
            "skill_name: demo\n"
            "skill_paths: [skill/SKILL.md]\n"
            "variants: [with_skill, without_skill]\n"
            "dataset_files:\n"
            "  cities: rows.jsonl\n"
            "cases:\n"
            "  - id: river-of\n"
            "    split: tune\n"
            "    kind: behavior\n"
            "    template: cities\n"
            f"    prompt: \"{prompt}\"\n"
            "    assertions:\n"
            "      - {name: names-river, type: contains, value: \"{river}\"}\n",
            encoding="utf-8")
        return repo / "evals" / "eval.yaml"

    def test_yaml_plus_jsonl_compiles_and_validates(self):
        with tempfile.TemporaryDirectory() as td:
            path = self.write_yaml_repo(Path(td))
            manifest = sb.validate_manifest(path)
            cases = sb.iter_cases(manifest)
        self.assertEqual([c["id"] for c in cases], ["river-of-paris", "river-of-cairo"])
        self.assertEqual(cases[1]["assertions"][0]["value"], "Nile")

    def test_leakage_lint_still_runs_on_compiled_manifest(self):
        with tempfile.TemporaryDirectory() as td:
            path = self.write_yaml_repo(Path(td), prompt="Say {river} of {city}.")
            report = sb.audit_manifest_report(path)
        kinds = [f["kind"] for f in report["findings"]]
        self.assertIn("prompt-assertion-leakage", kinds)

    def test_missing_dataset_file_dies(self):
        with tempfile.TemporaryDirectory() as td:
            path = self.write_yaml_repo(Path(td))
            (path.parent / "rows.jsonl").unlink()
            with self.assertRaises(SystemExit):
                sb.validate_manifest(path)


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
             "qualitative_total": 1, "qualitative_pass_rate": 1.0, "metadata": {},
             "qualitative_assertions": [{"name": "q", "passed": True, "severity": "soft", "score": 0.9}]},
            {"case_id": "h", "variant": "with_skill", "split": "holdout", "missing_output": False, "execution_valid": True,
             "qualitative_total": 1, "qualitative_pass_rate": 0.0, "metadata": {},
             "qualitative_assertions": [{"name": "q", "passed": False, "severity": "soft", "score": 0.4}]},
        ]
        visibility = sb.qualitative_by_visibility(results)
        self.assertEqual(visibility["tune_visible"]["mean_qualitative_pass_rate"], 1.0)
        self.assertEqual(visibility["held_out"]["mean_qualitative_pass_rate"], 0.0)
        self.assertEqual(visibility["held_out"]["mean_graded_score"], 0.4)

    def test_soft_objective_checks_never_enter_the_visibility_view(self):
        # P3 regression: soft_total also counts soft OBJECTIVE checks (e.g.
        # similarity). A run with no judge verdicts at all must not appear
        # here, and a mixed run's graded mean must use judge scores only.
        similarity_only = {
            "case_id": "s", "variant": "with_skill", "split": "holdout", "missing_output": False,
            "execution_valid": True, "qualitative_total": 0, "soft_total": 1, "graded_score": 0.97,
            "metadata": {}, "qualitative_assertions": [],
            "assertions": [{"name": "sim", "type": "similarity", "passed": True, "severity": "soft", "score": 0.97}],
        }
        self.assertEqual(sb.qualitative_by_visibility([similarity_only]), {})
        mixed = {
            "case_id": "m", "variant": "with_skill", "split": "holdout", "missing_output": False,
            "execution_valid": True, "qualitative_total": 0, "soft_total": 2, "graded_score": 0.7,  # blended 0.97 + 0.43
            "metadata": {},
            "assertions": [{"name": "sim", "type": "similarity", "passed": True, "severity": "soft", "score": 0.97}],
            "qualitative_assertions": [{"name": "judge", "passed": False, "severity": "soft", "score": 0.43}],
        }
        visibility = sb.qualitative_by_visibility([mixed])
        self.assertEqual(visibility["held_out"]["mean_graded_score"], 0.43)   # judge score, not the blend


class MigrationTests(unittest.TestCase):
    """Migration section — migrate 1 -> 2: mechanical stamps, checklist, --check."""

    def v1_manifest(self) -> dict:
        manifest = base_manifest()
        manifest["cases"][0]["assertions"] = [
            {"name": "has-alpha", "type": "contains", "value": "alpha"},
            {"name": "quality", "type": "judge", "rubric": ["complete", "clear"]},
            {"name": "oracle", "type": "script", "command": ["python3", "-c", "raise SystemExit(0)"]},
        ]
        return manifest

    def test_golden_round_trip_stamps_defaults(self):
        migrated, checklist = sb.migrate_manifest_data(self.v1_manifest())
        self.assertEqual(migrated["version"], 2)
        by_name = {a["name"]: a for a in migrated["cases"][0]["assertions"]}
        self.assertEqual(by_name["has-alpha"]["severity"], "gate")
        self.assertEqual(by_name["has-alpha"]["oracle"], "strong")
        self.assertEqual(by_name["quality"]["severity"], "soft")
        self.assertEqual(by_name["quality"]["oracle"], "live")
        self.assertIn("graded?", by_name["quality"]["_migrate_todo"])
        self.assertEqual(by_name["oracle"]["oracle"], "demo")
        decisions = {item["decision"] for item in checklist}
        self.assertEqual(decisions, {"graded dimensions", "oracle tier", "reference floor"})

    def test_checklist_lists_every_binary_judge_and_script(self):
        manifest = self.v1_manifest()
        manifest["cases"].append({
            "id": "case-2", "split": "tune", "kind": "behavior", "prompt": "p2",
            "assertions": [{"name": "q2", "type": "judge", "rubric": ["good"]},
                           {"name": "s2", "type": "script", "command": ["python3", "-c", "raise SystemExit(0)"]}],
        })
        _, checklist = sb.migrate_manifest_data(manifest)
        graded = [c for c in checklist if c["decision"] == "graded dimensions"]
        scripts = [c for c in checklist if c["decision"] == "oracle tier"]
        self.assertEqual({c["assertion"] for c in graded}, {"quality", "q2"})
        self.assertEqual({c["assertion"] for c in scripts}, {"oracle", "s2"})

    def test_graded_judge_gets_no_todo_marker(self):
        manifest = base_manifest()
        manifest["cases"][0]["assertions"] = [{"name": "g", "type": "judge",
                                               "graded_dimensions": [{"name": "d", "rubric": "5 = anchored; 1 = flat"}]}]
        migrated, checklist = sb.migrate_manifest_data(manifest)
        self.assertNotIn("_migrate_todo", migrated["cases"][0]["assertions"][0])
        self.assertFalse([c for c in checklist if c["decision"] == "graded dimensions"])

    def test_check_is_dry_and_write_migrates_in_place(self):
        with tempfile.TemporaryDirectory() as td:
            path = write_manifest(Path(td), self.v1_manifest())
            before = path.read_bytes()
            args = SimpleNamespace(manifest=str(path), check=True, out_checklist=str(Path(td) / "checklist.json"))
            self.assertEqual(sb.migrate_command(args), 0)
            self.assertEqual(path.read_bytes(), before)   # --check writes nothing to the manifest
            checklist = json.loads((Path(td) / "checklist.json").read_text(encoding="utf-8"))
            self.assertTrue(checklist["checklist"])
            args = SimpleNamespace(manifest=str(path), check=False, out_checklist=None)
            self.assertEqual(sb.migrate_command(args), 0)
            migrated = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(migrated["version"], 2)

    def test_version_2_manifest_validates_and_grades(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            manifest = base_manifest()
            manifest["version"] = 2
            manifest["cases"][0]["assertions"] = [{"name": "a", "type": "contains", "value": "alpha", "severity": "gate", "oracle": "strong"}]
            path = write_manifest(root, manifest)
            loaded = sb.validate_manifest(path)
            runs = root / "runs"
            for variant, text in [("with_skill", "alpha"), ("without_skill", "nope")]:
                base = runs / "case-1" / variant
                base.mkdir(parents=True)
                (base / "output.md").write_text(text, encoding="utf-8")
            report = sb.build_benchmark_report(path, runs)
        self.assertEqual(loaded["version"], 2)
        self.assertEqual(report["paired_summary"]["absolute_delta"], 1.0)

    def test_unsupported_version_dies(self):
        with tempfile.TemporaryDirectory() as td:
            manifest = base_manifest()
            manifest["version"] = 3
            path = write_manifest(Path(td), manifest)
            with self.assertRaises(SystemExit):
                sb.validate_manifest(path)

    def test_migration_preserves_pass_rates(self):
        # The back-compatibility contract: identical grading before and after.
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            path = write_manifest(root, self.v1_manifest())
            runs = root / "runs"
            for variant, text in [("with_skill", "alpha"), ("without_skill", "beta")]:
                base = runs / "case-1" / variant
                base.mkdir(parents=True)
                (base / "output.md").write_text(text, encoding="utf-8")
            before = sb.build_benchmark_report(path, runs)
            sb.migrate_command(SimpleNamespace(manifest=str(path), check=False, out_checklist=None))
            after = sb.build_benchmark_report(path, runs)
        for report in (before, after):
            report.pop("generated_at")
            report.pop("manifest")
        # The graded channel may appear (judge severity is now explicit soft —
        # same value it defaulted to), but every pass rate must be identical.
        self.assertEqual(
            [r["objective_pass_rate"] for r in before["results"]],
            [r["objective_pass_rate"] for r in after["results"]],
        )
        self.assertEqual(before["paired_summary"]["absolute_delta"], after["paired_summary"]["absolute_delta"])


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


class CapabilityRegressionIntentTests(unittest.TestCase):
    """G5 — per-case eval_intent makes saturation/no-lift/staleness intent-aware."""

    @staticmethod
    def _rows(case_id, intent, w, n):
        return [
            {"case_id": case_id, "variant": "with_skill", "run_number": 1,
             "objective_pass_rate": w, "combined_pass_rate": w, "eval_intent": intent},
            {"case_id": case_id, "variant": "without_skill", "run_number": 1,
             "objective_pass_rate": n, "combined_pass_rate": n, "eval_intent": intent},
        ]

    def _write_manifest(self, td, cases):
        return make_eval_repo(Path(td), skill_name="d", skill_paths=["skill/SKILL.md"],
                              skill_text="---\nname: d\ndescription: D\n---\n", cases=cases)

    def _case(self, cid, intent=None, value="y"):
        c = {"id": cid, "split": "tune", "kind": "behavior", "prompt": "x",
             "assertions": [{"name": "a", "type": "contains", "value": value}]}
        if intent is not None:
            c["eval_intent"] = intent
        return c

    def test_validate_enum(self):
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(SystemExit):
                sb.validate_manifest(self._write_manifest(td, [self._case("c", "bogus")]))
            for good in ("capability", "regression", None):
                sb.validate_manifest(self._write_manifest(td, [self._case("c", good)]))

    def test_result_rows_carry_intent_end_to_end(self):
        with tempfile.TemporaryDirectory() as td:
            p = self._write_manifest(td, [self._case("reg", "regression", "alpha"), self._case("cap", None, "alpha")])
            runs = Path(td) / "runs"
            for cid in ("reg", "cap"):
                for variant in ("with_skill", "without_skill"):
                    base = runs / cid / variant
                    base.mkdir(parents=True)
                    (base / "output.md").write_text("alpha", encoding="utf-8")
            report = sb.build_benchmark_report(p, runs)
        intent = {r["case_id"]: r.get("eval_intent") for r in report["results"]}
        self.assertEqual(intent["reg"], "regression")
        self.assertEqual(intent["cap"], "capability")   # default when untagged

    def test_readiness_splits_saturation_by_intent(self):
        report = {"results": self._rows("cap", "capability", 1.0, 1.0) + self._rows("reg", "regression", 1.0, 1.0)}
        sig = sb.readiness_run_signals(report)
        self.assertIn("cap", sig["base_saturated_cases"])
        self.assertIn("reg", sig["base_saturated_expected_cases"])
        self.assertNotIn("reg", sig["base_saturated_cases"])

    def test_eval_readiness_regression_guard_not_a_blocker(self):
        with tempfile.TemporaryDirectory() as td:
            p = self._write_manifest(td, [self._case("reg", "regression")])
            m = sb.validate_manifest(p)
            readiness = sb.eval_readiness(m, p, benchmark_report={"results": self._rows("reg", "regression", 1.0, 1.0)})
        self.assertIn("reg", readiness["regression_guards_holding"])
        self.assertNotIn("reg", readiness["base_saturated_cases"])
        self.assertFalse(any("base-saturated" in b for b in readiness["blockers"]))

    def test_stale_exempts_regression_guard(self):
        rep = {"results": self._rows("cap", "capability", 1.0, 1.0) + self._rows("reg", "regression", 1.0, 1.0)}
        ids = {c["case_id"] for c in sb.stale_case_candidates([rep, rep])}
        self.assertIn("cap", ids)          # all-green capability probe -> prune candidate
        self.assertNotIn("reg", ids)       # all-green regression guard -> exempt

    def test_suggest_exempts_regression_guard(self):
        report = {"case_flags": [{"case_id": "cap", "flags": ["saturated/non-discriminating"]},
                                 {"case_id": "reg", "flags": ["saturated/non-discriminating"]}]}
        manifest = {"cases": [{"id": "cap", "prompt": "p", "assertions": []},
                              {"id": "reg", "prompt": "p", "assertions": [], "eval_intent": "regression"}]}
        ids = {s["case_id"] for s in sb.suggest_case_candidates(report, manifest)}
        self.assertIn("cap", ids)
        self.assertNotIn("reg", ids)


class ContaminationPerimeterTests(unittest.TestCase):
    """Output-side contamination perimeter: canary tripwire, output<->answer n-gram
    overlap, released_at/cutoff gate. All pure and model-free."""

    def test_ngram_containment_exact_partial_and_safe(self):
        ans = "the quick brown fox jumps over the lazy dog again today here now"
        self.assertEqual(sb.ngram_containment(ans, ans, 4), 1.0)                          # output == answer
        self.assertEqual(sb.ngram_containment("wholly unrelated different content written elsewhere entirely", ans, 4), 0.0)
        partial = sb.ngram_containment("the quick brown fox jumps over", ans, 4)
        self.assertGreater(partial, 0.0)
        self.assertLess(partial, 1.0)
        self.assertEqual(sb.ngram_containment("a b c", "a b", 4), 0.0)                    # reference too short -> 0.0, no ZeroDivision

    def test_canary_tripwire_both_directions(self):
        case = {"id": "c", "canary": "CANARY-GUID-abc123"}
        self.assertTrue(any(f["kind"] == "canary-hit" for f in sb.contamination_check(case, "leak CANARY-GUID-abc123 here")["findings"]))
        self.assertFalse(sb.contamination_check(case, "no canary present")["findings"])

    def test_output_answer_overlap_flags(self):
        answer = " ".join(f"word{i}" for i in range(30))
        case = {"id": "c", "expected_behavior": [answer]}
        hit = sb.contamination_check(case, answer, n=6, overlap_threshold=0.6)
        self.assertAlmostEqual(hit["overlap"], 1.0)
        self.assertTrue(any(f["kind"] == "output-answer-overlap" for f in hit["findings"]))
        clean = sb.contamination_check(case, "entirely unrelated prose about other matters", n=6, overlap_threshold=0.6)
        self.assertEqual(clean["overlap"], 0.0)
        self.assertFalse(clean["findings"])

    def test_released_at_cutoff_gate_both_directions(self):
        case = {"id": "c", "released_at": "2024-06"}
        self.assertTrue(any(f["kind"] == "released-before-cutoff" for f in sb.contamination_check(case, "x", model_cutoff="2025-01")["findings"]))
        self.assertFalse(any(f["kind"] == "released-before-cutoff" for f in sb.contamination_check(case, "x", model_cutoff="2024-01")["findings"]))

    def test_released_at_numeric_not_lexicographic(self):
        # "2024-6" <= "2024-12" is FALSE as strings ('6' > '1') though June precedes December;
        # the gate must order by date. Mixed precision: a mid-month release vs a month cutoff.
        hit = lambda rel, cut: any(f["kind"] == "released-before-cutoff"
                                   for f in sb.contamination_check({"id": "c", "released_at": rel}, "x", model_cutoff=cut)["findings"])
        self.assertTrue(hit("2024-6", "2024-12"))       # was a false-negative under string compare
        self.assertTrue(hit("2024-06-15", "2024-06"))   # released inside the cutoff month -> flagged
        self.assertFalse(hit("2025-06", "2024-01"))     # clearly after the cutoff -> not flagged

    def test_released_at_equal_to_cutoff_flags(self):
        # docstring says "at/before" -> the == boundary must fire.
        case = {"id": "c", "released_at": "2024-06"}
        self.assertTrue(any(f["kind"] == "released-before-cutoff"
                            for f in sb.contamination_check(case, "x", model_cutoff="2024-06")["findings"]))

    def test_cutoff_key_precision_and_unparseable(self):
        self.assertEqual(sb.cutoff_key("2024", end=False), (2024, 1, 1))
        self.assertEqual(sb.cutoff_key("2024", end=True), (2024, 12, 31))
        self.assertEqual(sb.cutoff_key("2024-06", end=False), (2024, 6, 1))
        self.assertEqual(sb.cutoff_key("2024-06-15", end=True), (2024, 6, 15))
        self.assertIsNone(sb.cutoff_key("not-a-date", end=False))     # unparseable -> gate no-ops

    def test_overlap_at_exact_threshold_flags(self):
        # overlap exactly == threshold must fire (>=, not >): answer has two 2-grams, output has one.
        case = {"id": "c", "expected_behavior": ["a b c"]}
        r = sb.contamination_check(case, "a b", n=2, overlap_threshold=0.5)
        self.assertEqual(r["overlap"], 0.5)
        self.assertTrue(any(f["kind"] == "output-answer-overlap" for f in r["findings"]))

    def _manifest(self, td, case_extra):
        root = Path(td)
        (root / "repo" / "skill").mkdir(parents=True)
        (root / "repo" / "skill" / "SKILL.md").write_text("---\nname: d\ndescription: D\n---\n", encoding="utf-8")
        (root / "repo" / "evals").mkdir()
        p = root / "repo" / "evals" / "shared-benchmark.json"
        p.write_text(json.dumps({"version": 1, "skill_name": "d", "skill_paths": ["skill/SKILL.md"],
            "variants": ["with_skill", "without_skill"], "ablations": [],
            "cases": [{"id": "c", "split": "tune", "kind": "behavior", "prompt": "x",
                       "assertions": [{"name": "a", "type": "contains", "value": "y"}], **case_extra}]}), encoding="utf-8")
        return root, p

    def test_validate_rejects_non_string_canary(self):
        with tempfile.TemporaryDirectory() as td:
            _, p = self._manifest(td, {"canary": 123})
            with self.assertRaises(SystemExit):
                sb.validate_manifest(p)

    def test_validate_rejects_non_string_released_at(self):
        with tempfile.TemporaryDirectory() as td:
            _, p = self._manifest(td, {"released_at": 123})
            with self.assertRaises(SystemExit):
                sb.validate_manifest(p)

    def test_report_flags_canary_in_output_end_to_end(self):
        with tempfile.TemporaryDirectory() as td:
            root, p = self._manifest(td, {"canary": "ZZ-CANARY-99"})
            runs = root / "runs"
            (runs / "c" / "with_skill").mkdir(parents=True)
            (runs / "c" / "with_skill" / "output.md").write_text("here is the ZZ-CANARY-99 leaking", encoding="utf-8")
            (runs / "c" / "without_skill").mkdir(parents=True)
            (runs / "c" / "without_skill" / "output.md").write_text("clean output", encoding="utf-8")
            report = sb.contamination_report(p, runs, split="tune")
        self.assertEqual(report["total_findings"], 1)
        self.assertEqual(report["cases"][0]["case_id"], "c")
        self.assertEqual(report["cases"][0]["findings"][0]["kind"], "canary-hit")


if __name__ == "__main__":
    unittest.main()
