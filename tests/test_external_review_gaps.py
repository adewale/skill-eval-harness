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


if __name__ == "__main__":
    unittest.main()
