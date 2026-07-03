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


if __name__ == "__main__":
    unittest.main()
