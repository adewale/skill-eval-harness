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
