"""External-source review gaps (G1-G6; see docs/academic-grounding.md). G6 lives in
test_followup_features.py beside the reliability tests; G4 onward are here. All
deterministic, no live model or network."""
import json
import tempfile
import unittest
from pathlib import Path

import skill_benchmark as sb


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
            sb.validate_manifest(write(None))                                   # absent is fine


class CapabilityRegressionIntentTests(unittest.TestCase):
    """G5 — per-case eval_intent makes saturation/no-lift/staleness intent-aware."""

    @staticmethod
    def _rows(case_id, intent, w, n):
        return [
            {"case_id": case_id, "variant": "with_skill", "objective_pass_rate": w, "combined_pass_rate": w, "eval_intent": intent},
            {"case_id": case_id, "variant": "without_skill", "objective_pass_rate": n, "combined_pass_rate": n, "eval_intent": intent},
        ]

    def _write_manifest(self, td, cases):
        root = Path(td)
        (root / "repo" / "skill").mkdir(parents=True, exist_ok=True)
        (root / "repo" / "skill" / "SKILL.md").write_text("---\nname: d\ndescription: D\n---\n", encoding="utf-8")
        (root / "repo" / "evals").mkdir(exist_ok=True)
        p = root / "repo" / "evals" / "shared-benchmark.json"
        p.write_text(json.dumps({"version": 1, "skill_name": "d", "skill_paths": ["skill/SKILL.md"],
                                 "variants": ["with_skill", "without_skill"], "cases": cases, "ablations": []}), encoding="utf-8")
        return p

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

    def test_eval_readiness_capability_saturation_blocks(self):
        with tempfile.TemporaryDirectory() as td:
            p = self._write_manifest(td, [self._case("cap")])
            m = sb.validate_manifest(p)
            readiness = sb.eval_readiness(m, p, benchmark_report={"results": self._rows("cap", "capability", 1.0, 1.0)})
        self.assertIn("cap", readiness["base_saturated_cases"])
        self.assertTrue(any("base-saturated" in b for b in readiness["blockers"]))

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
        for reserved in ("output.md", "events.json", "metrics.json"):
            self.assertNotIn(reserved, inv)      # rides its own payload key

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


if __name__ == "__main__":
    unittest.main()
