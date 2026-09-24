"""Eval-quality review through the real report builder: verifier suspicions,
the declared model order, and error-analysis ordering, all offline.

The motivating evidence is the 2026-09-23 dogfood of the claude plugin eval
importer: on real Claude runs, `never_passes` caught an imported file check
that native runners can never satisfy, and `never_passes` plus
`oracle_disagreement` together caught an imported Skill-order check."""
import argparse
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

from helpers import attest_answer_design, make_eval_repo, write_run

import skill_benchmark as sb

CASES = [{
    "id": "label", "split": "tune", "prompt": "Label the finding.",
    "assertions": [
        {"name": "severity", "type": "regex", "pattern": "^Blocking", "ci": False},
        {"name": "impossible", "type": "contains", "value": "UNREACHABLE-TOKEN"},
        {"name": "quality", "type": "judge", "rubric": ["names the finding"], "severity": "gate"},
    ],
}]


def build(root: Path, outputs: dict[tuple[str, str], str], *, models: list[str] | None = None,
          judge: str | None = None, model_order: str | None = None) -> dict:
    manifest = make_eval_repo(root, cases=CASES)
    runs = root / "runs"
    for (model, variant), text in outputs.items():
        base = runs / "label" / model / variant if models else runs / "label" / variant
        write_run(base, text, metadata={"model": model} if models else None)
    attest_answer_design(manifest, runs)
    judge_path = None
    if judge is not None:
        # Real verdict rows through the judge command: a deterministic stub
        # passes a run exactly when its candidate output contains `judge`.
        judge_path = root / "judge.jsonl"
        stub = root / "judge_stub.py"
        stub.write_text("import json, sys\nprompt = sys.stdin.read()\n"
                        f"print(json.dumps({{'passed': {judge!r} in prompt, 'rationale': 'stub'}}))\n",
                        encoding="utf-8")
        args = argparse.Namespace(
            manifest=str(manifest), runs=str(runs), split=None, variant=None,
            judge_cmd=f"{sys.executable} {stub}", judge_backend="cmd", judge_model=None, judge_panel=None,
            judge_runs=1, strict_judge_schema=False, judge_trajectory=False, judge_explore=False,
            quorum=None, transcripts=None, out=str(judge_path), max_cost_usd=None,
            assumed_cost_per_run_usd=None)
        assert sb.judge_command(args) == 0
    order = sb.ModelOrder.parse(model_order) if model_order else None
    return sb.build_benchmark_report(manifest, runs, judge_results_path=str(judge_path) if judge_path else None,
                                     model_order=order)


class VerifierReviewReportTests(unittest.TestCase):
    def test_near_miss_and_never_passes_come_from_observed_verdicts(self):
        with tempfile.TemporaryDirectory() as td:
            report = build(Path(td), {
                ("m", "with_skill"): "**blocking**: the admin route has no test",
                ("m", "without_skill"): "Minor: looks fine",
            })
        review = report["verifier_review"]
        found = {(s["assertion"], s["signal"]) for s in review["suspicions"]}
        # severity fails strictly on "**blocking**" but passes with case and markdown ignored;
        # "Minor" is a real miss, so only the with-skill run is a near miss.
        self.assertIn(("severity", "format_near_miss"), found)
        near = next(s for s in review["suspicions"] if s["signal"] == "format_near_miss")
        self.assertEqual([r["variant"] for r in near["runs"]], ["with_skill"])
        self.assertIn(("impossible", "never_passes"), found)
        self.assertEqual(review["evidence_class"], "diagnostic")
        # diagnostics never move a pass rate
        rows = {r["variant"]: r for r in report["results"]}
        self.assertEqual(rows["with_skill"]["objective_pass_rate"], 0.0)

    def test_negative_checks_are_never_relaxed(self):
        definition = {"name": "no-todo", "type": "not_regex", "pattern": "TODO"}
        self.assertFalse(sb.format_near_miss(definition, "**todo** later"))
        self.assertTrue(sb.format_near_miss({"name": "x", "type": "contains", "value": "Blocking", "ci": False}, "`blocking`"))
        self.assertFalse(sb.format_near_miss({"name": "x", "type": "contains", "value": "Blocking", "ci": False}, "Minor"))

    def test_oracle_disagreement_needs_merged_judge_verdicts(self):
        with tempfile.TemporaryDirectory() as td:
            report = build(Path(td), {("m", "with_skill"): "Blocking: x", ("m", "without_skill"): "Blocking: y"},
                           judge="Blocking: x")
        disagreements = [s for s in report["verifier_review"]["suspicions"] if s["signal"] == "oracle_disagreement"]
        self.assertEqual([(s["assertion"], [r["variant"] for r in s["runs"]]) for s in disagreements],
                         [("impossible", ["with_skill"])])

    def test_error_analysis_puts_suspect_runs_first(self):
        with tempfile.TemporaryDirectory() as td:
            report = build(Path(td), {("m", "with_skill"): "**blocking**: x", ("m", "without_skill"): "Minor"},
                           judge="never-matches")
        self.assertEqual(report["availability"], "complete")
        analysis = sb.error_analysis_report(report)
        self.assertEqual(analysis["summary"]["verifier_suspect_runs"], 2)
        self.assertTrue(all(entry["verifier_suspects"] for entry in analysis["review_queue"]))
        self.assertEqual(analysis["verifier_signals"]["format_near_miss"], 1)
        first = analysis["review_queue"][0]
        self.assertIn("severity:format_near_miss", first["verifier_suspects"])


class ModelOrderReportTests(unittest.TestCase):
    OUTPUTS = {
        ("haiku", "with_skill"): "Blocking: UNREACHABLE-TOKEN", ("haiku", "without_skill"): "Minor",
        ("opus", "with_skill"): "Minor", ("opus", "without_skill"): "Minor",
    }

    def test_order_is_never_inferred(self):
        with tempfile.TemporaryDirectory() as td:
            report = build(Path(td), self.OUTPUTS, models=["haiku", "opus"])
        self.assertEqual(report["model_order_check"]["availability"], "not_applicable")

    def test_declared_order_flags_the_weaker_model_passing_more(self):
        with tempfile.TemporaryDirectory() as td:
            report = build(Path(td), self.OUTPUTS, models=["haiku", "opus"], model_order="haiku,opus")
        check = report["model_order_check"]
        self.assertEqual(check["order_weakest_first"], ["haiku", "opus"])
        scopes = {(i["scope"], i["variant"], i["weaker"], i["stronger"]) for i in check["inversions"]}
        self.assertEqual(scopes, {("label", "with_skill", "haiku", "opus"), ("suite", "with_skill", "haiku", "opus")})
        inversion = next(i for i in check["inversions"] if i["scope"] == "label")
        self.assertEqual((inversion["weaker_pass"]["passed"], inversion["stronger_pass"]["passed"]), (1, 0))
        self.assertFalse(inversion["significant"])        # one run each cannot clear p <= 0.05
        self.assertEqual(check["compared_pairs"], 2)

    def test_cli_validates_and_threads_the_flag(self):
        parser = sb.build_arg_parser()
        with self.assertRaises(ValueError):
            sb.CLIInvocation.from_namespace(parser.parse_args(["benchmark", "m", "--runs", "r", "--model-order", "haiku"]))
        with self.assertRaises(ValueError):
            sb.CLIInvocation.from_namespace(parser.parse_args(["benchmark", "m", "--runs", "r", "--model-order", "a,a"]))
        args = parser.parse_args(["benchmark", "m", "--runs", "r", "--model-order", "haiku,opus"])
        self.assertEqual(sb.model_order_from_args(sb.CLIInvocation.from_namespace(args).to_legacy_namespace()).to_list(),
                         ["haiku", "opus"])
        self.assertIsNone(sb.model_order_from_args(parser.parse_args(["benchmark", "m", "--runs", "r"])))


class RecordedRealOutputTests(unittest.TestCase):
    """Twelve real Claude outputs (2026-09-23) of a deliberately format-strict
    case; see tests/fixtures/eval-quality/README.md."""

    FIXTURE = Path(__file__).resolve().parent / "fixtures" / "eval-quality"

    def report(self, root: Path, recording: str = "recorded",
               order: str = "claude-haiku-4-5,claude-sonnet-5") -> dict:
        repo = root / "repo"
        shutil.copytree(self.FIXTURE / "repo", repo)
        runs = root / "runs"
        shutil.copytree(self.FIXTURE / recording, runs)
        manifest = repo / "evals" / "shared-benchmark.json"
        attest_answer_design(manifest, runs)
        return sb.build_benchmark_report(manifest, runs, model_order=sb.ModelOrder.parse(order))

    def test_all_three_findings_hold_on_real_model_output(self):
        with tempfile.TemporaryDirectory() as td:
            report = self.report(Path(td))
        suspicions = {(s["assertion"], s["signal"]): s for s in report["verifier_review"]["suspicions"]}
        near = suspicions[("severity-exact", "format_near_miss")]
        self.assertIn({"case_id": "c-review-verdict", "model": "claude-sonnet-5", "variant": "with_skill",
                       "run_number": 2}, near["runs"])
        never = suspicions[("verdict-line", "never_passes")]
        self.assertEqual(len(never["runs"]), 12)
        check = report["model_order_check"]
        significant = [i for i in check["inversions"] if i["significant"]]
        self.assertEqual([(i["assertion"], i["variant"], i["weaker_pass"]["passed"], i["stronger_pass"]["passed"])
                          for i in significant], [("severity-exact", "with_skill", 3, 0)])
        self.assertEqual(significant[0]["p_value"], 0.05)
        # the full-run view alone could not see it: neither model fully passed this case
        self.assertFalse([i for i in check["inversions"] if i["assertion"] is None and i["significant"]])


    def test_replication_keeps_the_verifier_flaw_and_drops_the_inversion(self):
        # 36 fresh runs (2026-09-24): n=6 per cell, Opus added as a third tier.
        with tempfile.TemporaryDirectory() as td:
            report = self.report(Path(td), "replication",
                                 "claude-haiku-4-5,claude-sonnet-5,claude-opus-5-5")
        suspicions = {(s["assertion"], s["signal"]): s for s in report["verifier_review"]["suspicions"]}
        # The verifier flaw replicates on every model and arm.
        self.assertEqual(len(suspicions[("verdict-line", "never_passes")]["runs"]), 36)
        # The p = 0.05 inversion does not: Haiku 3/6, Sonnet 2/6, Opus 2/6.
        check = report["model_order_check"]
        self.assertEqual(check["significant_inversions"], 0)
        self.assertEqual(sorted((i["stronger"], i["weaker_pass"]["passed"], i["stronger_pass"]["passed"], i["p_value"])
                                for i in check["inversions"]),
                         [("claude-opus-5-5", 3, 2, 0.5), ("claude-sonnet-5", 3, 2, 0.5)])

if __name__ == "__main__":
    unittest.main()
