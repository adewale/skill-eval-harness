"""First-class Claude adapter: parse the `claude -p --output-format json`
envelope in one place, capture real cost/usage into metrics.json, and total it
in the benchmark report."""
import argparse
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path

import skill_benchmark as sb


def _skill(rp: Path):
    sd = rp / "skills" / "demo"
    sd.mkdir(parents=True)
    (sd / "SKILL.md").write_text(
        "---\nname: demo\ndescription: Demo skill. Use for demos.\n---\n\n# Demo\n\nDo the thing.\n",
        encoding="utf-8")


def _manifest(rp: Path, cases):
    (rp / "evals").mkdir(parents=True, exist_ok=True)
    m = {"version": 1, "skill_name": "demo", "skill_paths": ["skills/demo/SKILL.md"],
         "variants": ["with_skill", "without_skill"], "cases": cases, "ablations": []}
    p = rp / "evals" / "shared-benchmark.json"
    p.write_text(json.dumps(m), encoding="utf-8")
    return p


def _stub_claude(path: Path, *, answer="STUB ANSWER token-XYZ", cost=0.0123,
                 in_tok=11, out_tok=22, returncode=0):
    """A fake `claude` executable: ignores argv, reads the prompt on stdin, emits
    the -p --output-format json envelope on stdout."""
    body = f'''#!/usr/bin/env python3
import sys, json
_ = sys.stdin.read()
env = {{"type":"result","result":{json.dumps(answer)},
       "total_cost_usd":{cost},
       "usage":{{"input_tokens":{in_tok},"output_tokens":{out_tok},
                "cache_read_input_tokens":100,"cache_creation_input_tokens":5}}}}
sys.stdout.write(json.dumps(env))
sys.exit({returncode})
'''
    path.write_text(body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return path


class ParseClaudeEnvelopeTests(unittest.TestCase):
    def test_parses_result_cost_and_usage(self):
        out = json.dumps({"result": "hello", "total_cost_usd": 0.04,
                          "usage": {"input_tokens": 3, "output_tokens": 7,
                                    "cache_read_input_tokens": 9}})
        p = sb.parse_claude_cli_json(out)
        self.assertEqual(p["answer"], "hello")
        self.assertEqual(p["cost_usd"], 0.04)
        self.assertEqual(p["usage"]["input_tokens"], 3)
        self.assertEqual(p["usage"]["output_tokens"], 7)
        self.assertEqual(p["usage"]["total_tokens"], 10)          # derived
        self.assertEqual(p["usage"]["cache_read_tokens"], 9)

    def test_tolerates_non_envelope(self):
        p = sb.parse_claude_cli_json("just raw text, not json")
        self.assertEqual(p["answer"], "just raw text, not json")
        self.assertIsNone(p["cost_usd"])
        self.assertIsNotNone(p["parse_error"])

    def test_tolerates_fenced_json_envelope(self):
        out = "```json\n" + json.dumps({"result": "x", "total_cost_usd": 0.01, "usage": {}}) + "\n```"
        p = sb.parse_claude_cli_json(out)
        self.assertEqual(p["answer"], "x")
        self.assertEqual(p["cost_usd"], 0.01)


class RunClaudeAdapterTests(unittest.TestCase):
    def _run(self, td: Path, *, cost=0.0123, returncode=0, answer="STUB ANSWER token-XYZ"):
        rp = td / "repo"; _skill(rp)
        case = {"id": "c", "split": "tune", "prompt": "do it",
                "assertions": [{"name": "a", "type": "contains", "value": "token-XYZ"}]}
        p = _manifest(rp, [case])
        rows = [r for r in sb.prepared_task_rows(p, sb.validate_manifest(p)) if r["variant"] == "with_skill"]
        tasks = td / "tasks.jsonl"
        tasks.write_text("".join(json.dumps(r) + "\n" for r in rows))
        stub = _stub_claude(td / "claude_stub.py", cost=cost, returncode=returncode, answer=answer)
        runs = td / "runs"
        ns = argparse.Namespace(tasks=str(tasks), runs=str(runs),
                                model="claude-haiku-4-5-20251001", claude_bin=str(stub), timeout=60)
        sb.run_claude(ns)
        return p, runs, rows[0]["run_dir"]

    def test_writes_output_and_cost_metrics(self):
        with tempfile.TemporaryDirectory() as t:
            td = Path(t)
            _, runs, run_dir = self._run(td)
            base = runs / run_dir
            self.assertIn("token-XYZ", (base / "output.md").read_text())
            metrics = json.loads((base / "metrics.json").read_text())
            self.assertEqual(metrics["cost_usd"], 0.0123)
            self.assertEqual(metrics["input_tokens"], 11)
            self.assertEqual(metrics["output_tokens"], 22)
            self.assertEqual(metrics["total_tokens"], 33)
            meta = json.loads((base / "metadata.json").read_text())
            self.assertEqual(meta["provider"], "claude")
            self.assertEqual(meta["model"], "claude-haiku-4-5-20251001")

    def test_nonzero_returncode_marks_infra_failure(self):
        with tempfile.TemporaryDirectory() as t:
            td = Path(t)
            _, runs, run_dir = self._run(td, returncode=3)
            text = (runs / run_dir / "output.md").read_text()
            self.assertTrue(text.lstrip().startswith(sb.CLAUDE_FAILURE))
            # and it is recognized as a non-scorable infra failure
            self.assertFalse(sb.execution_valid({"returncode": 3}, text))

    def test_benchmark_totals_cost(self):
        with tempfile.TemporaryDirectory() as t:
            td = Path(t)
            p, runs, _ = self._run(td, cost=0.02)
            report = sb.build_benchmark_report(p, runs, split="tune", variants_arg=["with_skill"])
            self.assertAlmostEqual(report["summary"]["with_skill"]["cost_usd_total"], 0.02, places=6)


class ClaudeJudgeAndPanelTests(unittest.TestCase):
    def test_native_claude_judge_stamps_model_and_cost(self):
        with tempfile.TemporaryDirectory() as t:
            td = Path(t)
            out = td / "output.md"; out.write_text("candidate answer")
            # stub emits a VERDICT json inside result (what a judge returns)
            stub = _stub_claude(td / "judge_stub.py",
                                answer=json.dumps({"passed": True, "score": 1}), cost=0.0051)
            task = {"judge_task_id": "c::with_skill::run-1::quality", "case_id": "c",
                    "variant": "with_skill", "run_number": 1, "output_path": str(out),
                    "assertion": {"name": "quality", "type": "judge", "threshold": 1},
                    "prompt": "grade it"}
            row = sb.run_one_judge_task(task, None, judge_model="claude-haiku-4-5-20251001",
                                        claude_bin=str(stub))
            self.assertTrue(row["passed"])
            self.assertEqual(row["judge_model"], "claude-haiku-4-5-20251001")
            self.assertEqual(row["cost_usd"], 0.0051)

    def test_panel_flags_magnitude_sensitivity(self):
        # good-pr: both judges positive, but Sonnet sees a much bigger lift
        haiku = {"summary": {"with_skill": {"mean_combined_pass_rate": 0.92},
                             "without_skill": {"mean_combined_pass_rate": 0.89}}}   # +0.03
        sonnet = {"summary": {"with_skill": {"mean_combined_pass_rate": 0.79},
                              "without_skill": {"mean_combined_pass_rate": 0.61}}}  # +0.18
        s = sb.judge_panel_sensitivity({"haiku": haiku, "sonnet": sonnet})
        self.assertFalse(s["sign_sensitive"])                 # both positive
        self.assertTrue(s["magnitude_sensitive"])             # spread 0.15 > 0.1
        self.assertTrue(s["judge_sensitive"])
        self.assertAlmostEqual(s["lift_by_judge"]["haiku"], 0.03, places=6)

    def test_panel_flags_sign_disagreement(self):
        a = {"summary": {"with_skill": {"mean_combined_pass_rate": 0.6},
                         "without_skill": {"mean_combined_pass_rate": 0.55}}}       # +0.05
        b = {"summary": {"with_skill": {"mean_combined_pass_rate": 0.5},
                         "without_skill": {"mean_combined_pass_rate": 0.55}}}       # -0.05
        s = sb.judge_panel_sensitivity({"a": a, "b": b})
        self.assertTrue(s["sign_sensitive"])
        self.assertTrue(s["judge_sensitive"])

    def test_panel_agreement_is_not_sensitive(self):
        a = {"summary": {"with_skill": {"mean_combined_pass_rate": 0.9},
                         "without_skill": {"mean_combined_pass_rate": 0.78}}}       # +0.12
        b = {"summary": {"with_skill": {"mean_combined_pass_rate": 0.8},
                         "without_skill": {"mean_combined_pass_rate": 0.68}}}       # +0.12
        s = sb.judge_panel_sensitivity({"a": a, "b": b})
        self.assertFalse(s["judge_sensitive"])


if __name__ == "__main__":
    unittest.main()
