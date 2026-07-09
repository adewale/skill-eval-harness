"""Judge plumbing: verdict schemas, presets, consensus, robustness probes, trajectory/tool-using judges.

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
        # factuality is soft by default: the verdict fills the soft/graded channel.
        self.assertEqual(merged["soft_passed"], 1)
        self.assertTrue(merged["qualitative_assertions"][0]["passed"])
        self.assertEqual(merged["qualitative_assertions"][0]["oracle"], "live")

    def test_unknown_preset_dies_in_validation(self):
        manifest = base_manifest()
        manifest["cases"][0]["assertions"] = [{"type": "judge", "preset": "vibes"}]
        with tempfile.TemporaryDirectory() as td:
            path = write_manifest(Path(td), manifest)
            with self.assertRaises(SystemExit):
                sb.validate_manifest(path)


class VerdictSchemaTests(unittest.TestCase):
    """G4 — canonical JSON schema per judge verdict shape + post-hoc gate."""

    GRADED = {"type": "judge", "graded_dimensions": [{"name": "clarity"}]}
    DYN = {"type": "judge", "dynamic_rubric": {"minimum_criteria": 3}}
    PLAIN = {"type": "judge", "name": "j"}

    def test_schema_per_shape(self):
        self.assertEqual(sb.verdict_schema_for(self.PLAIN)["required"], ["passed"])
        self.assertEqual(sb.verdict_schema_for(self.PLAIN)["properties"]["passed"]["type"], "boolean")
        self.assertEqual(sb.verdict_schema_for(self.GRADED)["required"], ["dimension_scores"])
        d = sb.verdict_schema_for({"type": "judge", "dynamic_rubric": {"minimum_criteria": 4}})
        self.assertEqual(d["required"], ["criteria"])
        self.assertEqual(d["properties"]["criteria"]["minItems"], 4)   # tracks minimum_criteria

    def test_json_schema_errors_flags_each_violation(self):
        plain = sb.verdict_schema_for(self.PLAIN)
        self.assertTrue(sb.json_schema_errors({"score": 5}, plain))          # missing passed
        self.assertFalse(sb.json_schema_errors({"passed": True}, plain))     # well-formed
        graded = sb.verdict_schema_for(self.GRADED)
        self.assertTrue(sb.json_schema_errors({"rationale": "x"}, graded))   # missing dimension_scores
        self.assertFalse(sb.json_schema_errors({"dimension_scores": {"clarity": 4}}, graded))
        dyn = sb.verdict_schema_for(self.DYN)
        self.assertTrue(sb.json_schema_errors({"criteria": [{"name": "a", "met": True}]}, dyn))            # < minItems
        self.assertTrue(sb.json_schema_errors({"criteria": [{"name": "a"}, {"name": "b"}, {"name": "c"}]}, dyn))  # missing met
        self.assertFalse(sb.json_schema_errors({"criteria": [{"name": f"c{i}", "met": True} for i in range(3)]}, dyn))

    def _task(self, td):
        run = Path(td) / "run"
        run.mkdir()
        (run / "output.md").write_text("answer", encoding="utf-8")
        return {"judge_task_id": "c::with_skill::run-1::j", "case_id": "c", "variant": "with_skill",
                "run_number": 1, "prompt": "p", "output_path": str(run / "output.md"),
                "assertion": {"type": "judge", "name": "j"}}

    def _cmd(self, td, obj):
        f = Path(td) / "verdict.json"
        f.write_text(json.dumps(obj), encoding="utf-8")
        return f"cat {f}"

    def test_report_surfaces_and_strict_fails_on_wrong_keys(self):
        with tempfile.TemporaryDirectory() as td:
            task = self._task(td)
            cmd = self._cmd(td, {"verdict": "good", "score": 5})   # valid JSON, but no `passed`
            report_row = sb.run_one_judge_task(task, judge_cmd=cmd, schema_enforcement="report")
            strict_row = sb.run_one_judge_task(task, judge_cmd=cmd, schema_enforcement="strict")
        self.assertIn("schema_errors", report_row)   # violation surfaced in both modes
        self.assertIn("schema_errors", strict_row)
        self.assertTrue(report_row["passed"])         # report: verdict resolves exactly as today (score>=threshold)
        self.assertFalse(strict_row["passed"])        # strict: forced closed via the parse-error channel

    def test_wellformed_verdict_stays_clean_and_default_is_report(self):
        with tempfile.TemporaryDirectory() as td:
            task = self._task(td)
            cmd = self._cmd(td, {"passed": True, "score": 5, "rationale": "ok"})
            rows = [sb.run_one_judge_task(task, judge_cmd=cmd),                              # default
                    sb.run_one_judge_task(task, judge_cmd=cmd, schema_enforcement="report"),
                    sb.run_one_judge_task(task, judge_cmd=cmd, schema_enforcement="strict")]
        for r in rows:
            self.assertNotIn("schema_errors", r)   # clean verdict -> no new key -> byte-identical row
            self.assertTrue(r["passed"])

    def test_native_codex_judge_uses_last_message_and_schema(self):
        with tempfile.TemporaryDirectory() as td:
            task = self._task(td)
            fake = Path(td) / "codex_stub.py"
            fake.write_text(
                "import json, os, pathlib, sys\n"
                "_ = sys.stdin.read()\n"
                "codex_home = pathlib.Path(os.environ['CODEX_HOME'])\n"
                "assert codex_home.is_dir()\n"
                "assert not codex_home.is_relative_to(pathlib.Path.cwd())\n"
                "assert not (pathlib.Path.cwd() / '.codex' / 'auth.json').exists()\n"
                "out = pathlib.Path(sys.argv[sys.argv.index('--output-last-message') + 1])\n"
                "schema = json.loads(pathlib.Path(sys.argv[sys.argv.index('--output-schema') + 1]).read_text())\n"
                "assert schema['additionalProperties'] is False\n"
                "assert set(schema['required']) == {'passed', 'score', 'rationale'}\n"
                "assert 'null' in schema['properties']['score']['type']\n"
                "out.write_text(json.dumps({'passed': True, 'score': 1, 'rationale': 'codex ok'}))\n"
                "print(json.dumps({'type':'usage','usage':{'input_tokens':2,'output_tokens':3}}))\n",
                encoding="utf-8")
            row = sb.run_one_judge_task(task, judge_backend="codex", judge_model="gpt-mini", codex_cmd=f"{sys.executable} {fake}")
        self.assertTrue(row["passed"])
        self.assertEqual(row["judge_model"], "codex/gpt-mini")
        self.assertEqual(row["judge_backend"], "codex")
        self.assertEqual(row["usage_normalized"]["total_tokens"], 5)

    def test_native_vibe_judge_parses_final_assistant_message(self):
        with tempfile.TemporaryDirectory() as td:
            task = self._task(td)
            fake = Path(td) / "vibe_stub.py"
            fake.write_text(
                "import json, os, pathlib, sys\n"
                "prompt = sys.argv[sys.argv.index('--prompt') + 1]\n"
                "assert '--prompt' in sys.argv\n"
                "assert '--output' in sys.argv and 'json' in sys.argv\n"
                "assert '--enabled-tools' in sys.argv and 're:^$' in sys.argv\n"
                "assert os.environ.get('VIBE_ACTIVE_MODEL') == 'mistral-large'\n"
                "cwd = pathlib.Path.cwd()\n"
                "vibe_home = pathlib.Path(os.environ['VIBE_HOME'])\n"
                "assert vibe_home.is_dir()\n"
                "assert not vibe_home.is_relative_to(cwd)\n"
                "assert not (cwd / '.vibe-home' / '.env').exists()\n"
                "assert 'Return only JSON' in prompt\n"
                "json.dump([{'role': 'assistant', 'content': json.dumps({'passed': True, 'score': 1, 'rationale': 'vibe ok'}),"
                " 'usage': {'input_tokens': 3, 'output_tokens': 4}}], sys.stdout)\n",
                encoding="utf-8")
            row = sb.run_one_judge_task(task, judge_backend="vibe", judge_model="mistral-large", vibe_cmd=f"{sys.executable} {fake}")
        self.assertTrue(row["passed"])
        self.assertEqual(row["judge_model"], "vibe/mistral-large")
        self.assertEqual(row["judge_backend"], "vibe")
        self.assertEqual(row["usage_normalized"]["total_tokens"], 7)

    def test_native_vibe_judge_default_cwd_is_isolated_from_repo(self):
        with tempfile.TemporaryDirectory() as td:
            task = self._task(td)
            cwd_file = Path(td) / "vibe-cwd.txt"
            fake = Path(td) / "vibe_stub.py"
            fake.write_text(
                "import json, os, pathlib, sys\n"
                "pathlib.Path(sys.argv[1]).write_text(os.getcwd())\n"
                "json.dump([{'role': 'assistant', 'content': json.dumps({'passed': True, 'score': 1, 'rationale': 'ok'})}], sys.stdout)\n",
                encoding="utf-8")
            row = sb.run_one_judge_task(task, judge_backend="vibe", judge_model="mistral", vibe_cmd=f"{sys.executable} {fake} {cwd_file}")
            self.assertTrue(row["passed"])
            invoked_cwd = Path(cwd_file.read_text(encoding="utf-8"))
            self.assertFalse((invoked_cwd / "skill_benchmark.py").exists(), invoked_cwd)
            self.assertNotEqual(invoked_cwd.resolve(), ROOT.resolve())

    def test_native_codex_judge_default_cwd_is_isolated_from_repo(self):
        with tempfile.TemporaryDirectory() as td:
            task = self._task(td)
            cwd_file = Path(td) / "cwd.txt"
            fake = Path(td) / "codex_stub.py"
            fake.write_text(
                "import json, os, pathlib, sys\n"
                "pathlib.Path(sys.argv[1]).write_text(os.getcwd())\n"
                "out = pathlib.Path(sys.argv[sys.argv.index('--output-last-message') + 1])\n"
                "out.write_text(json.dumps({'passed': True, 'score': 1, 'rationale': 'ok'}))\n",
                encoding="utf-8")
            row = sb.run_one_judge_task(task, judge_backend="codex", judge_model="gpt-mini", codex_cmd=f"{sys.executable} {fake} {cwd_file}")
            self.assertTrue(row["passed"])
            invoked_cwd = Path(cwd_file.read_text(encoding="utf-8"))
            self.assertFalse((invoked_cwd / "skill_benchmark.py").exists(), invoked_cwd)
            self.assertNotEqual(invoked_cwd.resolve(), ROOT.resolve())

    def test_codex_structured_schema_supports_graded_dimensions(self):
        assertion = {"type": "judge", "graded_dimensions": [{"name": "clarity", "rubric": "anchored"}]}
        schema = sb.codex_structured_output_schema(sb.verdict_schema_for(assertion))
        self.assertFalse(schema["additionalProperties"])
        dim_scores = schema["properties"]["dimension_scores"]
        self.assertFalse(dim_scores["additionalProperties"])
        self.assertEqual(dim_scores["required"], ["clarity"])
        self.assertEqual(dim_scores["properties"]["clarity"]["type"], "number")
        self.assertTrue(sb.json_schema_errors({"dimension_scores": {"other": 3}, "rationale": None}, schema))
        self.assertFalse(sb.json_schema_errors({"dimension_scores": {"clarity": 4}, "rationale": None}, schema))

    def test_prompt_embeds_the_schema(self):
        task = {"judge_task_id": "c::with_skill::run-1::j", "case_id": "c", "variant": "with_skill",
                "run_number": 1, "prompt": "p", "assertion": self.PLAIN}
        self.assertIn(json.dumps(sb.verdict_schema_for(self.PLAIN)), sb.judge_prompt(task, "output"))

    def test_manifest_schema_enforcement_validated(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "repo" / "skill").mkdir(parents=True)
            (root / "repo" / "skill" / "SKILL.md").write_text("---\nname: d\ndescription: D\n---\n", encoding="utf-8")
            (root / "repo" / "evals").mkdir()
            p = root / "repo" / "evals" / "shared-benchmark.json"
            base = {"version": 1, "skill_name": "d", "skill_paths": ["skill/SKILL.md"],
                    "variants": ["with_skill", "without_skill"],
                    "cases": [{"id": "c", "split": "tune", "kind": "behavior", "prompt": "x",
                               "assertions": [{"name": "a", "type": "contains", "value": "y"}]}],
                    "ablations": []}

            def write(judge_cfg):
                m = dict(base)
                if judge_cfg is not None:
                    m["judge"] = judge_cfg
                p.write_text(json.dumps(m), encoding="utf-8")
                return p

            with self.assertRaises(SystemExit):
                sb.validate_manifest(write({"schema_enforcement": "loose"}))   # invalid enum rejected
            sb.validate_manifest(write({"schema_enforcement": "strict"}))       # valid accepted
            sb.validate_manifest(write({"schema_enforcement": "report"}))       # the documented default, also accepted
            sb.validate_manifest(write(None))                                   # absent is fine
            # G3 panel activation surface (judge.panel / judge.models) is validated too.
            sb.validate_manifest(write({"panel": ["m1", "m2"]}))                # good panel accepted
            for bad in ({"panel": []}, {"panel": ["m1", 2]}, {"models": "solo"}, {"models": [""]}):
                with self.assertRaises(SystemExit):
                    sb.validate_manifest(write(bad))


class TrajectoryJudgeTests(unittest.TestCase):
    """G1 — opt-in run-dir/trajectory judge, gated by the leakage denylist."""

    def _run_dir(self, td, *, events="valid", extra=None):
        run = Path(td) / "run"
        run.mkdir()
        (run / "output.md").write_text("the answer", encoding="utf-8")
        if events == "valid":
            (run / "events.json").write_text(json.dumps({"events": [{"type": "tool_call", "name": "Read"}]}), encoding="utf-8")
        elif events == "malformed":
            (run / "events.json").write_text("{not json", encoding="utf-8")
        (run / "metrics.json").write_text(json.dumps({"total_tokens": 42}), encoding="utf-8")
        (run / "poster.html").write_text("<html>", encoding="utf-8")     # legit artifact
        (run / "notes.txt").write_text("scratch", encoding="utf-8")      # legit artifact
        (run / "grading.json").write_text(json.dumps({"answer": "BLOCK"}), encoding="utf-8")  # PLANTED oracle
        (run / "result.json").write_text(json.dumps({"passed": True}), encoding="utf-8")      # a PRIOR judge verdict
        for name, body in (extra or {}).items():
            (run / name).write_text(body, encoding="utf-8")
        return run

    def _task(self, run):
        return {"judge_task_id": "c::with_skill::run-1::j", "case_id": "c", "variant": "with_skill",
                "run_number": 1, "prompt": "p", "run_base": str(run),
                "output_path": str(run / "output.md"), "assertion": {"type": "judge", "name": "j"}}

    def test_inventory_denylists_oracle_and_reserved(self):
        with tempfile.TemporaryDirectory() as td:
            inv = sb.judge_artifact_inventory(self._run_dir(td))
        self.assertIn("poster.html", inv)
        self.assertIn("notes.txt", inv)
        self.assertNotIn("grading.json", inv)   # KEYSTONE: never leak the grader's answer key
        for reserved in ("output.md", "events.json", "metrics.json", "result.json"):
            self.assertNotIn(reserved, inv)      # rides its own payload key (result.json is a prior verdict)

    def test_inventory_denylists_answer_and_rubric_names(self):
        with tempfile.TemporaryDirectory() as td:
            inv = sb.judge_artifact_inventory(self._run_dir(td, extra={"answer_key.txt": "x", "rubric.md": "y", "expected.json": "z"}))
        for leaky in ("answer_key.txt", "rubric.md", "expected.json"):
            self.assertNotIn(leaky, inv)

    def test_flag_off_prompt_is_byte_identical(self):
        with tempfile.TemporaryDirectory() as td:
            task = self._task(self._run_dir(td))
            off = sb.judge_prompt(task, "the answer")
            explicit_none = sb.judge_prompt(task, "the answer", trajectory=None, metrics=None, artifacts=None)
        self.assertEqual(off, explicit_none)
        self.assertNotIn("trajectory", off)

    def test_flag_on_payload_carries_trajectory_not_oracle(self):
        with tempfile.TemporaryDirectory() as td:
            run = self._run_dir(td)
            task = self._task(run)
            events, _ = sb.read_events_base(run)
            prompt = sb.judge_prompt(task, "the answer", trajectory=events,
                                     metrics=sb.read_metrics_base(run), artifacts=sb.judge_artifact_inventory(run))
        self.assertIn("trajectory", prompt)
        # The EVENTS must reach the payload, not merely the hint word "trajectory": the
        # hint says "tool-call" (hyphen); the event value "tool_call" (underscore) plus
        # the tool name "Read" only appear when the events themselves are embedded.
        self.assertIn("tool_call", prompt)
        self.assertIn('"name": "Read"', prompt)
        self.assertIn("poster.html", prompt)       # inventory embedded
        self.assertIn("total_tokens", prompt)       # metrics embedded
        self.assertNotIn("grading.json", prompt)    # oracle filename never embedded

    def test_run_one_judge_task_shape_unchanged_with_trajectory(self):
        with tempfile.TemporaryDirectory() as td:
            run = self._run_dir(td)
            task = self._task(run)
            vf = Path(td) / "verdict.json"      # outside run_base so it is not an artifact
            vf.write_text(json.dumps({"passed": True, "score": 4, "rationale": "ok"}), encoding="utf-8")
            cmd = f"cat {vf}"
            text_row = sb.run_one_judge_task(task, judge_cmd=cmd)
            traj_row = sb.run_one_judge_task(task, judge_cmd=cmd, include_trajectory=True)
        self.assertEqual(set(text_row), set(traj_row))   # trajectory changes the INPUT, not the row contract
        self.assertTrue(traj_row["passed"])
        self.assertEqual(traj_row["judge_task_id"], "c::with_skill::run-1::j")

    def test_malformed_events_degrades_without_crashing(self):
        with tempfile.TemporaryDirectory() as td:
            run = self._run_dir(td, events="malformed")
            task = self._task(run)
            vf = Path(td) / "verdict.json"
            vf.write_text(json.dumps({"passed": True}), encoding="utf-8")
            row = sb.run_one_judge_task(task, judge_cmd=f"cat {vf}", include_trajectory=True)
        self.assertTrue(row["passed"])   # bad events.json -> trajectory omitted, judge still runs


class CrossJudgeConsensusTests(unittest.TestCase):
    """G3 — merge_cross_judge_rows consensus + effective_judge_models panel."""

    @staticmethod
    def _row(model, passed, score=None, cost=None):
        r = {"judge_task_id": "c::with_skill::run-1::j", "case_id": "c", "variant": "with_skill",
             "run_number": 1, "judge_model": model, "passed": passed, "threshold": 3, "evidence": f"{model}-ev"}
        if score is not None:
            r["score"] = score
        if cost is not None:
            r["cost_usd"] = cost
        return r

    def test_len1_shortcircuits_unchanged(self):
        row = self._row("m1", True, 4)
        self.assertIs(sb.merge_cross_judge_rows([row]), row)   # single-judge path stays byte-identical

    def test_unanimous_pass_median_and_agreement(self):
        out = sb.merge_cross_judge_rows([self._row("m1", True, 4), self._row("m2", True, 5), self._row("m3", True, 3)])
        self.assertTrue(out["passed"])
        self.assertEqual(out["score"], 4)                      # median(4,5,3)
        self.assertEqual(out["judge_model"], "consensus")
        self.assertEqual(out["judge_models"], ["m1", "m2", "m3"])
        self.assertEqual(out["agreement"], {"concur": 3, "n": 3, "concur_fraction": 1.0, "unanimous": True, "unresolved": False})
        self.assertEqual(len(out["judge_panel"]), 3)           # members nested
        self.assertIn("m1-ev", out["evidence"])

    def test_majority_and_minority(self):
        maj = sb.merge_cross_judge_rows([self._row("m1", True, 5), self._row("m2", True, 5), self._row("m3", False, 1)])
        self.assertTrue(maj["passed"])
        self.assertEqual(maj["agreement"]["concur_fraction"], round(2 / 3, 4))
        self.assertFalse(maj["agreement"]["unanimous"])
        minr = sb.merge_cross_judge_rows([self._row("m1", True, 5), self._row("m2", False, 1), self._row("m3", False, 1)])
        self.assertFalse(minr["passed"])

    def test_even_tie_resolved_by_score_median(self):
        # 2-2 split; median score 4 >= threshold 3 -> passed, not unresolved
        out = sb.merge_cross_judge_rows([self._row("m1", True, 5), self._row("m2", True, 5),
                                         self._row("m3", False, 3), self._row("m4", False, 3)])
        self.assertTrue(out["passed"])
        self.assertFalse(out["agreement"]["unresolved"])

    def test_even_tie_without_scores_is_unresolved_not_coinflip(self):
        out = sb.merge_cross_judge_rows([self._row("m1", True), self._row("m2", False)])
        self.assertFalse(out["passed"])
        self.assertTrue(out["agreement"]["unresolved"])        # explicit, never a silent coin-flip

    def test_quorum_overrides_majority(self):
        out = sb.merge_cross_judge_rows([self._row("m1", True, 5), self._row("m2", True, 5), self._row("m3", False, 1)], quorum=3)
        self.assertFalse(out["passed"])                        # 2-of-3 pass, but quorum demands 3

    def test_consensus_is_order_independent(self):
        rows = [self._row("m1", True, 5), self._row("m2", False, 1), self._row("m3", True, 4)]
        a = sb.merge_cross_judge_rows(list(rows))
        b = sb.merge_cross_judge_rows(list(reversed(rows)))
        self.assertEqual((a["passed"], a["score"], a["agreement"]["concur"], a["agreement"]["unanimous"]),
                         (b["passed"], b["score"], b["agreement"]["concur"], b["agreement"]["unanimous"]))

    def test_cost_is_panel_summed_once(self):
        out = sb.merge_cross_judge_rows([self._row("m1", True, 5, cost=0.10), self._row("m2", True, 5, cost=0.20)])
        self.assertAlmostEqual(out["cost_usd"], 0.30)          # summed on the top row, not double-counted

    def test_effective_judge_models_precedence(self):
        self.assertEqual(sb.effective_judge_models({}, ["a", "b"], "x"), ["a", "b"])                       # cli panel wins
        self.assertEqual(sb.effective_judge_models({"judge": {"panel": ["p1", "p2"]}}, None, None), ["p1", "p2"])
        self.assertEqual(sb.effective_judge_models({"judge": {"models": ["q1"]}}, None, None), ["q1"])
        self.assertEqual(sb.effective_judge_models({"judge": {"model": "solo"}}, None, None), ["solo"])    # scalar -> 1-member
        self.assertEqual(sb.effective_judge_models({}, None, "cli"), ["cli"])
        self.assertEqual(sb.effective_judge_models({}, None, None), [])

    def test_consensus_row_joins_like_a_single_verdict(self):
        jassert = {"name": "j", "type": "judge", "severity": "gate"}
        jid = sb.judge_task_id("c", "with_skill", 1, sb.expand_judge_preset(jassert))
        consensus = sb.merge_cross_judge_rows([{**self._row("m1", True, 5), "judge_task_id": jid},
                                               {**self._row("m2", True, 5), "judge_task_id": jid}])
        case = {"id": "c", "split": "tune", "kind": "behavior", "assertions": [jassert]}
        result, _ = sb.grade_case_variant(case, "with_skill", "x", Path("o.md"), {}, judge_results={jid: consensus})
        self.assertEqual(len(result["qualitative_assertions"]), 1)                 # exactly one merged verdict per jid
        self.assertEqual((result["qualitative_total"], result["qualitative_passed"]), (1, 1))

    def test_consensus_score_is_median_not_mean(self):
        # [5,5,1]: median 5 vs mean ~3.67 -> pins median; a mean mutation would show 3.67.
        out = sb.merge_cross_judge_rows([self._row("m1", True, 5), self._row("m2", True, 5), self._row("m3", False, 1)])
        self.assertEqual(out["score"], 5)

    def test_even_tie_with_scores_but_no_threshold_is_unresolved(self):
        # a bare raw-score panel (no calibrated threshold) must NOT pass on the default-1
        # fallback (median >= 1 is ~always true) — it resolves to unresolved.
        row = lambda m, p, s: {"judge_task_id": "j", "judge_model": m, "passed": p, "score": s, "evidence": "e"}
        out = sb.merge_cross_judge_rows([row("m1", True, 3), row("m2", False, 2)])   # no threshold key
        self.assertFalse(out["passed"])
        self.assertTrue(out["agreement"]["unresolved"])

    def test_quorum_exactly_met_passes(self):
        out = sb.merge_cross_judge_rows([self._row("m1", True, 5), self._row("m2", True, 5), self._row("m3", False, 1)], quorum=2)
        self.assertTrue(out["passed"])                         # concur == quorum passes (>=, not >)

    def test_even_tie_median_equals_threshold_passes(self):
        # 2-2 tie, all scores 3, threshold 3 -> median == threshold -> passed via >=.
        out = sb.merge_cross_judge_rows([self._row("m1", True, 3), self._row("m2", True, 3),
                                         self._row("m3", False, 3), self._row("m4", False, 3)])
        self.assertTrue(out["passed"])
        self.assertFalse(out["agreement"]["unresolved"])

    def test_even_tie_median_below_threshold_fails_but_resolved(self):
        # median 2 < threshold 3 -> passed False, but RESOLVED (the median decided), not unresolved.
        out = sb.merge_cross_judge_rows([self._row("m1", True, 2), self._row("m2", True, 2),
                                         self._row("m3", False, 2), self._row("m4", False, 2)])
        self.assertFalse(out["passed"])
        self.assertFalse(out["agreement"]["unresolved"])

    def test_all_fail_panel_is_unanimous(self):
        out = sb.merge_cross_judge_rows([self._row("m1", False, 1), self._row("m2", False, 1)])
        self.assertTrue(out["agreement"]["unanimous"])         # concur == 0 is unanimous too, not just concur == n
        self.assertFalse(out["passed"])


class JudgeRobustnessTests(unittest.TestCase):
    """Judge-robustness probes: order-flip self-consistency + empty/master-key
    negative controls a robust judge must reject. Stub judges are real subprocesses
    (file-based, deterministic) so run_one_judge_task runs for real; each broken
    judge isolates exactly ONE dimension so the finding-set assertions are
    mutation-killing (a rate that stops distinguishing the four judges is a bug)."""

    # A candidate output carrying this marker is the only "genuinely good" answer.
    ROBUST = 'import sys\ns = sys.stdin.read()\nprint(\'{"passed": true}\' if "GOODANSWER" in s else \'{"passed": false}\')\n'
    ALWAYS_PASS = 'import sys\nsys.stdin.read()\nprint(\'{"passed": true}\')\n'
    # Obeys an instruction injected into the candidate output -> defeated by the master key only.
    MASTER_KEY_VULN = ('import sys\ns = sys.stdin.read()\n'
                       'ok = "GOODANSWER" in s or "IGNORE ALL PRIOR INSTRUCTIONS" in s\n'
                       'print(\'{"passed": true}\' if ok else \'{"passed": false}\')\n')
    # Passes only when ZEBRA-FIRST precedes ALPHA-SECOND -> the order flip inverts it.
    ORDER_SENSITIVE = ('import sys\ns = sys.stdin.read()\n'
                       'try:\n    ordered = s.index("ZEBRA-FIRST") < s.index("ALPHA-SECOND")\n'
                       'except ValueError:\n    ordered = False\n'
                       'ok = ordered and "GOODANSWER" in s\n'
                       'print(\'{"passed": true}\' if ok else \'{"passed": false}\')\n')

    def _judge(self, td, name, body):
        f = Path(td) / f"judge_{name}.py"
        f.write_text(body, encoding="utf-8")
        return f"python3 {f}"

    def _task(self, td, *, output="GOODANSWER is present"):
        run = Path(td) / "run"
        run.mkdir(exist_ok=True)
        (run / "output.md").write_text(output, encoding="utf-8")
        return {"judge_task_id": "c::with_skill::run-1::j", "case_id": "c", "variant": "with_skill",
                "run_number": 1, "prompt": "p", "expected_behavior": ["ZEBRA-FIRST", "ALPHA-SECOND"],
                "output_path": str(run / "output.md"), "assertion": {"type": "judge", "name": "j"}}

    def _report(self, td, name, body, task=None):
        ctl = Path(td) / "ctl"
        ctl.mkdir(exist_ok=True)
        return sb.judge_robustness_report([task or self._task(td)], tmp_dir=ctl,
                                          judge_cmd=self._judge(td, name, body))

    def test_robust_judge_has_no_findings(self):
        with tempfile.TemporaryDirectory() as td:
            rep = self._report(td, "robust", self.ROBUST)
        self.assertEqual(rep["findings"], [])                                             # clean, exactly
        self.assertEqual(rep["summary"], {"n": 1, "order_flip_consistency": 1.0, "control_leak_rate": 0.0})
        self.assertEqual(rep["tasks"][0]["controls_passed"], {"empty": False, "master-key": False})
        self.assertIs(rep["tasks"][0]["order_flip_consistent"], True)

    def test_always_pass_leaks_both_controls(self):
        with tempfile.TemporaryDirectory() as td:
            rep = self._report(td, "yes", self.ALWAYS_PASS)
        self.assertEqual(sorted(f["kind"] for f in rep["findings"]),
                         ["passes-empty-control", "passes-master-key-control"])
        self.assertEqual(rep["summary"]["control_leak_rate"], 1.0)                        # 2 of 2
        self.assertEqual(rep["summary"]["order_flip_consistency"], 1.0)                   # passes base and flip alike
        self.assertEqual(rep["tasks"][0]["controls_passed"], {"empty": True, "master-key": True})

    def test_master_key_vuln_isolates_that_control(self):
        with tempfile.TemporaryDirectory() as td:
            rep = self._report(td, "mk", self.MASTER_KEY_VULN)
        self.assertEqual(len(rep["findings"]), 1)                                         # ONLY the master key leaks
        self.assertEqual(rep["findings"][0]["kind"], "passes-master-key-control")
        self.assertEqual(rep["findings"][0]["judge_task_id"], "c::with_skill::run-1::j")
        self.assertEqual(rep["tasks"][0]["controls_passed"], {"empty": False, "master-key": True})
        self.assertEqual(rep["summary"]["control_leak_rate"], 0.5)                        # 1 of 2
        self.assertEqual(rep["summary"]["order_flip_consistency"], 1.0)

    def test_order_sensitive_judge_flagged(self):
        with tempfile.TemporaryDirectory() as td:
            rep = self._report(td, "ord", self.ORDER_SENSITIVE)
        self.assertEqual(len(rep["findings"]), 1)
        self.assertEqual(rep["findings"][0]["kind"], "order-flip-inconsistent")
        self.assertEqual(rep["summary"]["order_flip_consistency"], 0.0)                   # the one task flipped
        self.assertEqual(rep["summary"]["control_leak_rate"], 0.0)                        # still rejects both controls
        self.assertIs(rep["tasks"][0]["order_flip_consistent"], False)

    def test_flip_reverses_and_does_not_mutate(self):
        import copy
        task = {"expected_behavior": ["a", "b", "c"], "review_rubric": ["x", "y"],
                "assertion": {"graded_dimensions": ["d1", "d2"]}}
        original = copy.deepcopy(task)
        flipped = sb.flipped_judge_task(task)
        self.assertEqual(flipped["expected_behavior"], ["c", "b", "a"])
        self.assertEqual(flipped["review_rubric"], ["y", "x"])
        self.assertEqual(flipped["assertion"]["graded_dimensions"], ["d2", "d1"])
        self.assertEqual(task, original)                                                  # input untouched (no aliasing)
        double = sb.flipped_judge_task(flipped)                                           # flip is an involution
        self.assertEqual(double["expected_behavior"], original["expected_behavior"])
        self.assertEqual(double["review_rubric"], original["review_rubric"])
        self.assertEqual(double["assertion"]["graded_dimensions"], original["assertion"]["graded_dimensions"])

    def test_flip_tolerates_missing_and_nonlist_keys(self):
        out = sb.flipped_judge_task({"prompt": "p"})
        self.assertEqual(out["assertion"], {})                                            # normalized to a dict
        self.assertNotIn("expected_behavior", out)                                        # absent stays absent
        out2 = sb.flipped_judge_task({"expected_behavior": "notalist"})
        self.assertEqual(out2["expected_behavior"], "notalist")                           # non-list left alone

    def _cli_manifest(self, td):
        root = Path(td)
        (root / "repo" / "skill").mkdir(parents=True)
        (root / "repo" / "skill" / "SKILL.md").write_text("---\nname: d\ndescription: D\n---\n", encoding="utf-8")
        (root / "repo" / "evals").mkdir()
        p = root / "repo" / "evals" / "shared-benchmark.json"
        p.write_text(json.dumps({"version": 1, "skill_name": "d", "skill_paths": ["skill/SKILL.md"],
            "variants": ["with_skill", "without_skill"], "ablations": [],
            "cases": [{"id": "c", "split": "tune", "kind": "behavior", "prompt": "x",
                       "assertions": [{"name": "j", "type": "judge", "review_rubric": ["is it good"]}]}]}), encoding="utf-8")
        runs = root / "runs"
        (runs / "c" / "with_skill").mkdir(parents=True)
        (runs / "c" / "with_skill" / "output.md").write_text("GOODANSWER is present", encoding="utf-8")
        (runs / "c" / "without_skill").mkdir(parents=True)
        (runs / "c" / "without_skill" / "output.md").write_text("GOODANSWER is present", encoding="utf-8")
        return p, runs

    def _args(self, td, p, runs, name, body, *, fail_on_findings=False):
        from types import SimpleNamespace
        return SimpleNamespace(cmd="judge-robustness", manifest=str(p), runs=str(runs), split="tune",
                               variant=None, judge_cmd=self._judge(td, name, body), judge_model=None,
                               claude_bin="claude", fail_on_findings=fail_on_findings, out=str(Path(td) / "rep.json"))

    def test_command_exit_code_contract_end_to_end(self):
        with tempfile.TemporaryDirectory() as td:
            p, runs = self._cli_manifest(td)
            # Robust judge -> no findings -> exit 0 even with the CI gate armed.
            self.assertEqual(sb.judge_robustness_command(self._args(td, p, runs, "robust", self.ROBUST, fail_on_findings=True)), 0)
            report = json.loads((Path(td) / "rep.json").read_text(encoding="utf-8"))
            self.assertEqual(report["findings"], [])
            self.assertEqual(report["summary"]["n"], 2)   # one judge task per variant (with_skill + without_skill)
            # Always-pass judge leaks controls: findings present, and the gate flips exit code.
            self.assertEqual(sb.judge_robustness_command(self._args(td, p, runs, "yes", self.ALWAYS_PASS, fail_on_findings=False)), 0)
            self.assertEqual(sb.judge_robustness_command(self._args(td, p, runs, "yes", self.ALWAYS_PASS, fail_on_findings=True)), 1)


class ToolUsingJudgeTests(unittest.TestCase):
    """G1 follow-on — the opt-in tool-using judge explores a SANITIZED copy of the
    run dir. The security invariant is safety-by-CONSTRUCTION: the oracle is never
    copied, so a filesystem-reading judge cannot read the answer key. The keystone
    test proves that through the real run_one_judge_task path with a stub judge that
    lists the directory it was actually given."""

    ORACLE = {"grading.json": '{"answer": "BLOCK"}', "answer_key.txt": "BLOCK",
              "rubric.md": "grade on X", "expected.json": "{}", "GOLD.txt": "g"}
    LEGIT = {"output.md": "the candidate answer", "events.json": '{"events": []}',
             "metrics.json": '{"total_tokens": 9}', "poster.html": "<html>", "notes.txt": "scratch"}

    def _run_dir(self, td, *, nested=False):
        run = Path(td) / "run"
        run.mkdir()
        for name, body in {**self.LEGIT, **self.ORACLE}.items():
            (run / name).write_text(body, encoding="utf-8")
        if nested:
            (run / "sub").mkdir()
            (run / "sub" / "answer_note.txt").write_text("BLOCK", encoding="utf-8")  # oracle, nested
            (run / "sub" / "artifact.txt").write_text("keep me", encoding="utf-8")   # legit, nested
            (run / "grading").mkdir()                                                # whole oracle dir
            (run / "grading" / "key.txt").write_text("BLOCK", encoding="utf-8")
        return run

    # --- sanitized_run_copy: the safety-by-construction core ---
    def test_copy_drops_every_oracle_keeps_legit_and_reserved(self):
        with tempfile.TemporaryDirectory() as td:
            dest = sb.sanitized_run_copy(self._run_dir(td), Path(td) / "san")
            present = {p.name for p in dest.iterdir()}
        for oracle in self.ORACLE:
            self.assertNotIn(oracle, present)        # KEYSTONE: no answer key / rubric on disk
        for legit in self.LEGIT:
            self.assertIn(legit, present)            # reserved files STAY (judge reads output.md etc.)

    def test_copy_drops_oracle_in_nested_dirs(self):
        with tempfile.TemporaryDirectory() as td:
            dest = sb.sanitized_run_copy(self._run_dir(td, nested=True), Path(td) / "san")
            self.assertTrue((dest / "sub" / "artifact.txt").exists())        # legit nested file kept
            self.assertFalse((dest / "sub" / "answer_note.txt").exists())    # oracle nested file dropped
            self.assertFalse((dest / "grading").exists())                    # whole oracle dir dropped

    def test_copy_returns_none_when_run_base_absent(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertIsNone(sb.sanitized_run_copy(Path(td) / "missing", Path(td) / "san"))

    # --- prompt hint: additive, byte-identical when off, never names the oracle ---
    def test_prompt_byte_identical_when_off_and_carries_dir_when_on(self):
        task = {"judge_task_id": "c::with_skill::run-1::j", "case_id": "c", "variant": "with_skill",
                "run_number": 1, "prompt": "p", "assertion": {"type": "judge", "name": "j"}}
        off = sb.judge_prompt(task, "out")
        self.assertEqual(off, sb.judge_prompt(task, "out", explore_dir=None))   # None -> unchanged
        self.assertNotIn("explore", off.lower())
        on = sb.judge_prompt(task, "out", explore_dir="/tmp/san/run")
        self.assertIn("/tmp/san/run", on)
        self.assertIn("read-only tools", on)
        self.assertIn("NOT present", on)                                       # tells the judge the oracle is absent
        # Composes with the trajectory hint without clobbering it.
        both = sb.judge_prompt(task, "out", trajectory=[{"type": "tool_call"}], explore_dir="/tmp/san/run")
        self.assertIn("trajectory", both)
        self.assertIn("/tmp/san/run", both)

    def _stub_claude(self, td):
        """A fake `claude`: records the argv + a listing of the --add-dir it was handed
        to a probe.json beside itself, then emits a passing verdict envelope."""
        stub = Path(td) / "claude_stub.py"
        body = ('#!/usr/bin/env python3\n'
                'import sys, json, os\n'
                'argv = sys.argv[1:]\n'
                '_ = sys.stdin.read()\n'
                'add_dir = None\n'
                'for i, a in enumerate(argv):\n'
                '    if a == "--add-dir" and i + 1 < len(argv): add_dir = argv[i+1]\n'
                'seen = sorted(os.listdir(add_dir)) if add_dir and os.path.isdir(add_dir) else []\n'
                'probe = os.path.join(os.path.dirname(os.path.abspath(sys.argv[0])), "probe.json")\n'
                'json.dump({"argv": argv, "add_dir": add_dir, "seen": seen, "cwd": os.getcwd()}, open(probe, "w"))\n'
                'verdict = json.dumps({"passed": True, "score": 5, "rationale": "explored"})\n'
                'env = {"type": "result", "result": verdict, "total_cost_usd": 0.01,\n'
                '       "usage": {"input_tokens": 1, "output_tokens": 1}}\n'
                'sys.stdout.write(json.dumps(env))\n')
        stub.write_text(body, encoding="utf-8")
        stub.chmod(stub.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
        return stub

    def _task(self, run):
        return {"judge_task_id": "c::with_skill::run-1::j", "case_id": "c", "variant": "with_skill",
                "run_number": 1, "prompt": "p", "run_base": str(run),
                "output_path": str(run / "output.md"), "assertion": {"type": "judge", "name": "j"}}

    def _tmp_explore_dirs(self):
        return {n for n in os.listdir(tempfile.gettempdir()) if n.startswith("judge-explore-")}

    def test_explore_end_to_end_sanitizes_and_arms_readonly_tools(self):
        with tempfile.TemporaryDirectory() as td:
            run = self._run_dir(td)
            stub = self._stub_claude(td)
            before = self._tmp_explore_dirs()
            row = sb.run_one_judge_task(self._task(run), judge_model="m", claude_bin=str(stub), explore=True)
            probe = json.loads((Path(td) / "probe.json").read_text(encoding="utf-8"))
            after = self._tmp_explore_dirs()
        self.assertTrue(row["passed"])                                     # verdict flows back unchanged
        self.assertEqual(row["score"], 5)
        self.assertIn("--add-dir", probe["argv"])                          # tools were armed
        self.assertIn("--allowedTools", probe["argv"])
        self.assertEqual(probe["argv"][probe["argv"].index("--allowedTools") + 1], "Read,Grep,Glob,LS")
        self.assertIn("--json-schema", probe["argv"])
        self.assertIn("output.md", probe["seen"])                          # judge saw the real output...
        for oracle in self.ORACLE:
            self.assertNotIn(oracle, probe["seen"])                        # ...but NEVER the answer key
        # The judge runs WITH the sanitized copy as cwd — not the repo root, which holds
        # the live oracle. Read/Grep with no path would otherwise range over the repo.
        self.assertIn("judge-explore-", probe["cwd"])
        self.assertNotEqual(probe["cwd"], os.getcwd())
        self.assertEqual(after, before)                                    # the scratch copy was cleaned up

    def test_explore_off_arms_no_tools_and_no_add_dir(self):
        with tempfile.TemporaryDirectory() as td:
            run = self._run_dir(td)
            stub = self._stub_claude(td)
            row = sb.run_one_judge_task(self._task(run), judge_model="m", claude_bin=str(stub), explore=False)
            probe = json.loads((Path(td) / "probe.json").read_text(encoding="utf-8"))
        self.assertTrue(row["passed"])
        self.assertIsNone(probe["add_dir"])                                # off -> no directory handed over
        self.assertNotIn("--add-dir", probe["argv"])
        self.assertIn("--tools", probe["argv"])
        self.assertEqual(probe["argv"][probe["argv"].index("--tools") + 1], "")
        self.assertIn("--json-schema", probe["argv"])
        self.assertNotEqual(probe["cwd"], os.getcwd())                     # native judges never inherit repo cwd
        self.assertIn("claude-invoke-cwd-", probe["cwd"])                 # isolated empty cwd by construction

    def test_copy_drops_innocent_named_symlink_to_oracle(self):
        # KEYSTONE: copytree(symlinks=False) dereferences a link, copying the target's
        # CONTENT under the link's name — an oracle smuggled past the name denylist.
        with tempfile.TemporaryDirectory() as td:
            run = self._run_dir(td)
            os.symlink(run / "grading.json", run / "notes_link.txt")   # innocent name -> oracle
            os.symlink(Path(td) / "outside_secret.txt", run / "data.txt")  # escapes run_base
            (Path(td) / "outside_secret.txt").write_text("EXFIL", encoding="utf-8")
            dest = sb.sanitized_run_copy(run, Path(td) / "san")
            present = {p.name for p in dest.iterdir()}
        self.assertNotIn("notes_link.txt", present)                    # link to grading.json not followed
        self.assertNotIn("data.txt", present)                          # link escaping run_base not followed
        self.assertIn("output.md", present)                            # real files still copied

    def test_explore_skipped_when_run_base_missing(self):
        # A task with no run_base must NOT resolve to '.' (repo root) and copy it.
        with tempfile.TemporaryDirectory() as td:
            stub = self._stub_claude(td)
            task = {"judge_task_id": "c::with_skill::run-1::j", "case_id": "c", "variant": "with_skill",
                    "run_number": 1, "prompt": "p", "output_path": str(Path(td) / "missing.md"),
                    "assertion": {"type": "judge", "name": "j"}}   # NO run_base key
            before = self._tmp_explore_dirs()
            row = sb.run_one_judge_task(task, judge_model="m", claude_bin=str(stub), explore=True)
            probe = json.loads((Path(td) / "probe.json").read_text(encoding="utf-8"))
            after = self._tmp_explore_dirs()
        self.assertTrue(row["passed"])
        self.assertIsNone(probe["add_dir"])          # no run dir -> no copy, no --add-dir over the repo
        self.assertNotEqual(probe["cwd"], os.getcwd())
        self.assertIn("claude-invoke-cwd-", probe["cwd"])
        self.assertEqual(after, before)

    def test_explore_is_inert_on_shell_judge_cmd(self):
        with tempfile.TemporaryDirectory() as td:
            run = self._run_dir(td)
            vf = Path(td) / "verdict.json"
            vf.write_text(json.dumps({"passed": True}), encoding="utf-8")
            # explore=True but a shell judge_cmd (no judge_model): no copy built, no crash.
            row = sb.run_one_judge_task(self._task(run), judge_cmd=f"cat {vf}", explore=True)
        self.assertTrue(row["passed"])

    def test_command_rejects_explore_with_shell_judge_cmd(self):
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "skill").mkdir(parents=True)
            (root / "skill" / "SKILL.md").write_text("---\nname: d\ndescription: D\n---\n", encoding="utf-8")
            p = root / "shared-benchmark.json"
            p.write_text(json.dumps({"version": 1, "skill_name": "d", "skill_paths": ["skill/SKILL.md"],
                "variants": ["with_skill", "without_skill"], "ablations": [],
                "cases": [{"id": "c", "split": "tune", "kind": "behavior", "prompt": "x",
                           "assertions": [{"name": "a", "type": "contains", "value": "y"}]}]}), encoding="utf-8")
            args = SimpleNamespace(manifest=str(p), runs=str(root / "runs"), split=None, variant=None,
                                   judge_cmd="cat x", judge_model=None, judge_panel=None, claude_bin="claude",
                                   judge_runs=1, strict_judge_schema=False, judge_trajectory=False,
                                   judge_explore=True, quorum=None, transcripts=None, out=None)
            with self.assertRaises(SystemExit):
                sb.judge_command(args)


class JudgeVerdictPassedTests(unittest.TestCase):
    """A stored judge verdict may carry `passed` with `score: null` (the judge
    stated a boolean, no numeric score). The merge must read `passed` and never
    evaluate a `score >= threshold` fallback against None — a `dict.get(k, expr)`
    default is evaluated eagerly, so the buggy one-liner crashed on real judge
    output. run_one_judge_task and grade_case_variant now share one owner."""

    def test_passed_true_with_null_score_does_not_crash(self):
        self.assertTrue(sb.judge_verdict_passed({"passed": True, "score": None}))
        self.assertFalse(sb.judge_verdict_passed({"passed": False, "score": None}))

    def test_score_only_paths(self):
        self.assertTrue(sb.judge_verdict_passed({"score": 1, "threshold": 1}))
        self.assertFalse(sb.judge_verdict_passed({"score": 0.4, "threshold": 1}))
        # no passed and non-numeric score => not passed (never a TypeError)
        self.assertFalse(sb.judge_verdict_passed({"score": None}))
        self.assertFalse(sb.judge_verdict_passed({}))

    def test_grade_case_variant_merges_null_score_verdict(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td) / "run"
            write_run(base, "some candidate answer", metadata={}, metrics={})
            case = {"id": "c", "split": "tune", "prompt": "x",
                    "assertions": [{"name": "quality", "type": "judge",
                                    "prompt": "is it good?"}]}
            jid = sb.judge_task_id("c", "with_skill", 1, case["assertions"][0])
            judged = {jid: {"passed": True, "score": None, "threshold": 1,
                            "evidence": "looks good"}}
            result, tasks = sb.grade_case_variant(
                case, "with_skill", "some candidate answer",
                base / "output.md", {}, run_number=1, run_base=base,
                judge_results=judged)
            self.assertEqual(tasks, [])                      # verdict supplied, no new judge task
            # A default (soft) judge feeds the graded/soft channel, not the
            # qualitative pass rate; the null-score verdict still merges as a pass.
            self.assertEqual(result["qualitative_total"], 0)
            self.assertEqual(result["soft_total"], 1)
            self.assertEqual(result["soft_passed"], 1)
            self.assertTrue(result["qualitative_assertions"][0]["passed"])


class JudgeTaskScorabilityTests(unittest.TestCase):
    """Judge-task emission must honor THE scorable_run predicate, like every other
    report view. A run with no output (or an infra failure) is excluded from
    scoring downstream anyway, so emitting a judge task for it only spends a model
    call to grade an empty/failed candidate whose verdict is then discarded. The
    live multi-model run wasted ~$2 grading missing outputs this way."""

    Q_CASE = {"id": "c", "split": "tune", "prompt": "x",
              "assertions": [{"name": "quality", "type": "judge", "prompt": "is it good?"}]}

    def _tasks(self, text, metadata):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td) / "run"
            base.mkdir(parents=True)
            _, tasks = sb.grade_case_variant(
                self.Q_CASE, "with_skill", text, base / "output.md", metadata,
                run_number=1, run_base=base, judge_results={})
            return tasks

    def test_scorable_run_emits_judge_task(self):
        self.assertEqual(len(self._tasks("a real candidate answer", {})), 1)

    def test_missing_output_emits_no_judge_task(self):
        self.assertEqual(self._tasks(None, {}), [])

    def test_infra_failure_emits_no_judge_task(self):
        self.assertEqual(self._tasks("[CODEX FAILURE: returncode=1]\ndied", {"returncode": 1}), [])


class JudgeAlignmentTests(unittest.TestCase):
    def test_cohen_kappa_chance_corrects(self):
        self.assertEqual(sb.cohen_kappa([True, False, True, False], [True, False, True, False]), 1.0)
        self.assertEqual(sb.cohen_kappa([True, True], [True, True]), 1.0)  # degenerate-agree
        self.assertAlmostEqual(sb.cohen_kappa([True, False, True, False], [False, True, False, True]), -1.0)

    def test_alignment_confusion_and_warnings(self):
        human = {"a": {"passed": True}, "b": {"passed": False}, "c": {"passed": True}}
        judge = {"a": {"passed": True}, "b": {"passed": True}, "c": {"passed": True}}   # b is a false positive
        rep = sb.judge_alignment_report(human, judge)
        self.assertEqual(rep["confusion"], {"tp": 2, "fp": 1, "fn": 0, "tn": 0})
        self.assertAlmostEqual(rep["agreement"], 2 / 3, places=4)   # report rounds to 4dp
        self.assertAlmostEqual(rep["recall"], 1.0)              # caught every real pass
        self.assertAlmostEqual(rep["precision"], 2 / 3, places=4)  # but passed a human-fail
        self.assertTrue(any("unstable" in w for w in rep["warnings"]))  # n=3 < 50

    def test_no_overlap_is_flagged_not_crashed(self):
        rep = sb.judge_alignment_report({"x": {"passed": True}}, {"y": {"passed": True}})
        self.assertEqual(rep["n"], 0)
        self.assertIsNone(rep["agreement"])
        self.assertTrue(rep["warnings"])

    def test_f1_is_zero_not_none_for_label_inverting_judge(self):
        # the worst judge (inverts every label -> tp=0) must report F1=0.0, not None,
        # or a reviewer scanning for low F1 skips the null. Regression for the audit bug.
        human = {"a": {"passed": True}, "b": {"passed": False}}
        judge = {"a": {"passed": False}, "b": {"passed": True}}
        rep = sb.judge_alignment_report(human, judge)
        self.assertEqual(rep["confusion"], {"tp": 0, "fp": 1, "fn": 1, "tn": 0})
        self.assertEqual(rep["f1"], 0.0)
        self.assertEqual(rep["precision"], 0.0)
        self.assertEqual(rep["recall"], 0.0)

    def test_f1_none_only_when_no_positives_exist(self):
        # tp=fp=fn=0 (everything a true negative) -> F1 genuinely undefined
        rep = sb.judge_alignment_report({"a": {"passed": False}}, {"a": {"passed": False}})
        self.assertIsNone(rep["f1"])
        self.assertEqual(rep["confusion"], {"tp": 0, "fp": 0, "fn": 0, "tn": 1})

    def test_kappa_band_thresholds(self):
        self.assertEqual(sb.kappa_band(0.9), "almost-perfect")
        self.assertEqual(sb.kappa_band(0.8), "substantial")   # strict > boundary
        self.assertEqual(sb.kappa_band(0.5), "moderate")
        self.assertEqual(sb.kappa_band(0.3), "fair")
        self.assertEqual(sb.kappa_band(0.1), "slight")
        self.assertEqual(sb.kappa_band(0.0), "poor (<= chance)")
        self.assertEqual(sb.kappa_band(-0.2), "poor (<= chance)")
        self.assertIsNone(sb.kappa_band(None))


if __name__ == "__main__":
    unittest.main()
