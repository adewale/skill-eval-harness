"""The bundled offline example is executable documentation: prepare -> run (with the
deterministic stub 'model') -> report, and the two materialized ablations each
confirm a regression on a distinct assertion. Runs in CI with no model/API."""
import argparse
import json
import sys
import tempfile
import unittest
from pathlib import Path

import run_pi_trigger_eval as tr
import run_trigger_matrix as tm
import skill_benchmark as sb

ROOT = Path(__file__).resolve().parents[1]
DEMO = ROOT / "examples" / "demo-skill"
DEMO_MANIFEST = DEMO / "evals" / "shared-benchmark.json"
TRAJECTORY_MANIFEST = DEMO / "trajectory-benchmark.json"
TRIGGER_EVAL_SET = DEMO / "evals" / "trigger-eval-set.json"


def run_demo_suite(test: unittest.TestCase, manifest_path: Path, *,
                   runner_flags: str = "", judge_flags: str = "") -> tuple[dict, list[dict]]:
    """prepare -> run-codex (stub runner) -> judge (stub judge) -> benchmark,
    exactly the command sequence the demo journeys print."""
    manifest = sb.validate_manifest(manifest_path)
    tmp = tempfile.TemporaryDirectory(prefix="demo-journey-")
    test.addCleanup(tmp.cleanup)
    td = Path(tmp.name)
    rows = sb.prepared_task_rows(manifest_path, manifest)
    (td / "tasks.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    sb.run_codex(argparse.Namespace(
        tasks=str(td / "tasks.jsonl"), runs=str(td / "runs"),
        codex_cmd=f"{sys.executable} {DEMO / 'stub_runner.py'}{runner_flags}", timeout=120))
    judge_cmd = f"{sys.executable} {DEMO / 'stub_judge.py'}{judge_flags}"
    verdicts = [sb.run_one_judge_task(task, judge_cmd, None, 1)
                for task in sb.collect_judge_tasks(manifest_path, td / "runs")]
    judge_results = td / "judge-results.jsonl"
    judge_results.write_text("\n".join(json.dumps(v) for v in verdicts) + "\n", encoding="utf-8")
    return sb.build_benchmark_report(manifest_path, td / "runs",
                                     judge_results_path=str(judge_results)), verdicts


def assertion_row(report: dict, case_id: str, variant: str, name: str) -> dict:
    result = next(r for r in report["results"]
                  if r["case_id"] == case_id and r["variant"] == variant)
    return next(a for a in result["assertions"] + result["qualitative_assertions"]
                if a["name"] == name)


class DemoExampleTests(unittest.TestCase):
    def _run(self):
        mp = DEMO / "evals" / "shared-benchmark.json"
        manifest = sb.validate_manifest(mp)
        tmp = tempfile.TemporaryDirectory(prefix="demo-eval-")
        self.addCleanup(tmp.cleanup)
        td = Path(tmp.name)
        # 6 matched runs per arm clear the two-sided paired sign-flip floor
        # (2/2^6 = 0.03125); fewer unanimous pairs stay INDETERMINATE.
        rows = sb.prepared_task_rows(mp, manifest, include_ablations=True, ablation_dir=str(td / "abl"), runs_per_variant=6)
        (td / "tasks.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
        stub = f"{sys.executable} {DEMO / 'stub_runner.py'}"
        sb.run_codex(argparse.Namespace(tasks=str(td / "tasks.jsonl"), runs=str(td / "runs"), codex_cmd=stub, timeout=120))
        variants = sorted({r["variant"] for r in rows})   # include the ablation arms, not just the manifest variants
        # The example declares one judge assertion, so an executable end-to-end
        # report must also materialize its verdicts. Leaving them deferred would
        # correctly make the report partial and would make objective ablation
        # evidence look complete only by projecting away a declared grader.
        judge_tasks = sb.collect_judge_tasks(mp, td / "runs", variants=variants)
        judge_cmd = f"{sys.executable} {DEMO / 'stub_judge.py'}"
        verdicts = [sb.run_one_judge_task(task, judge_cmd, None, 1)
                    for task in judge_tasks]
        judge_results = td / "judge-results.jsonl"
        judge_results.write_text(
            "\n".join(json.dumps(verdict) for verdict in verdicts) + "\n",
            encoding="utf-8",
        )
        return sb.build_benchmark_report(
            mp, td / "runs", variants_arg=variants,
            judge_results_path=str(judge_results),
        )

    def test_materialized_ablations_confirm_offline(self):
        rep = self._run()
        regs = {e["id"]: e for e in rep["ablation_regressions"]}
        for aid, assertion in (("no-severity", "severity-label"), ("no-checklist", "cite-checklist")):
            entry = regs[aid]
            self.assertEqual(entry["status"], "measured", f"{aid} should be measured")
            self.assertTrue(entry["provenance_verified"], f"{aid} provenance must verify (materialized, same revision)")
            confirmed = [r for r in entry["regressions"] if r.get("expected_regression_confirmed")]
            self.assertTrue(confirmed, f"{aid} should confirm a regression")

    def test_with_skill_beats_without_on_the_demo(self):
        rep = self._run()
        s = rep["summary"]
        self.assertEqual(s["with_skill"]["objective_pass_rate"]["mean"], 1.0)      # skill present -> both assertions pass
        self.assertEqual(s["without_skill"]["objective_pass_rate"]["mean"], 0.0)   # no skill -> both fail


class DemoJudgeTests(unittest.TestCase):
    """Pins the stub-judge pair's calibration signature that
    docs/can-i-trust-my-judge.md pastes: the careful judge aligns with the human
    labels and rejects the negative controls; the --lenient rubber-stamp leaks
    every control and scores kappa 0.0 despite 0.5 raw agreement."""

    VARIANTS = ["with_skill", "without_skill", "ablation:no-severity", "ablation:no-checklist"]
    # The human gold labels the journey doc records for the four c-review arms.
    HUMAN = {
        "with_skill": True,
        "without_skill": False,
        "ablation:no-severity": False,
        "ablation:no-checklist": True,
    }

    def _judge_rows(self, lenient: bool):
        mp = DEMO / "evals" / "shared-benchmark.json"
        manifest = sb.validate_manifest(mp)
        tmp = tempfile.TemporaryDirectory(prefix="demo-judge-")
        self.addCleanup(tmp.cleanup)
        td = Path(tmp.name)
        rows = sb.prepared_task_rows(mp, manifest, include_ablations=True, ablation_dir=str(td / "abl"))
        rows = [r for r in rows if r["variant"] in self.VARIANTS]
        (td / "tasks.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
        sb.run_codex(argparse.Namespace(tasks=str(td / "tasks.jsonl"), runs=str(td / "runs"),
                                        codex_cmd=f"{sys.executable} {DEMO / 'stub_runner.py'}", timeout=120))
        tasks = sb.collect_judge_tasks(mp, td / "runs", variants=self.VARIANTS)
        self.assertEqual(len(tasks), 4)   # one actionable-review task per c-review arm
        cmd = f"{sys.executable} {DEMO / 'stub_judge.py'}" + (" --lenient" if lenient else "")
        verdicts = [sb.run_one_judge_task(t, cmd, None, 1) for t in tasks]
        return tasks, verdicts, td

    @staticmethod
    def _keyed(rows):
        return {r["judge_task_id"]: r for r in rows}

    def test_careful_judge_aligns_and_rejects_controls(self):
        tasks, verdicts, td = self._judge_rows(lenient=False)
        human = {t["judge_task_id"]: {"passed": self.HUMAN[t["variant"]]} for t in tasks}
        align = sb.judge_alignment_report(human, self._keyed(verdicts), min_labels=4)
        self.assertEqual(align["cohen_kappa"], 1.0)
        self.assertEqual(align["confusion"], {"tp": 2, "fp": 0, "fn": 0, "tn": 2})
        robust = sb.judge_robustness_report(tasks, tmp_dir=td,
                                            judge_cmd=f"{sys.executable} {DEMO / 'stub_judge.py'}")
        self.assertEqual(robust["summary"]["order_flip_consistency"], 1.0)
        self.assertEqual(robust["summary"]["control_leak_rate"], 0.0)
        self.assertEqual(robust["findings"], [])

    def test_lenient_judge_is_caught_by_both_probes(self):
        tasks, verdicts, td = self._judge_rows(lenient=True)
        human = {t["judge_task_id"]: {"passed": self.HUMAN[t["variant"]]} for t in tasks}
        align = sb.judge_alignment_report(human, self._keyed(verdicts), min_labels=4)
        self.assertEqual(align["agreement"], 0.5)      # right whenever the answer deserves to pass...
        self.assertEqual(align["cohen_kappa"], 0.0)    # ...but no better than chance once corrected
        self.assertEqual(align["recall"], 1.0)
        self.assertEqual(align["precision"], 0.5)
        robust = sb.judge_robustness_report(tasks, tmp_dir=td,
                                            judge_cmd=f"{sys.executable} {DEMO / 'stub_judge.py'} --lenient")
        self.assertEqual(robust["summary"]["control_leak_rate"], 1.0)
        kinds = {f["kind"] for f in robust["findings"]}
        self.assertEqual(kinds, {"passes-empty-control", "passes-master-key-control"})


class DemoTrajectoryJourneyTests(unittest.TestCase):
    """Pins docs/did-my-skill-change-how-the-model-works.md: the stub runner's
    trace records the reads it actually performs, a saturated (no-lift) case
    still shows the skill changing the path, and --loop reaches the same
    passing answer through a redundant path that only the path checks catch."""

    def test_no_lift_case_still_shows_the_skill_changing_the_path(self):
        report, _ = run_demo_suite(self, TRAJECTORY_MANIFEST)
        for variant in ("with_skill", "without_skill"):
            self.assertTrue(assertion_row(report, "c-weak-outcome", variant, "gives-a-review")["passed"])
        flags = next(f["flags"] for f in report["case_flags"] if f["case_id"] == "c-weak-outcome")
        self.assertIn("no objective lift", flags)
        case = next(c for c in report["trajectory_diff"]["cases"] if c["case_id"] == "c-weak-outcome")
        self.assertEqual(case["commands_only_with_skill"],
                         ["cat skills/skills_demo_SKILL.md/SKILL.md",
                          "cat skills/skills_demo_SKILL.md/references/checklist.md"])
        self.assertEqual(case["commands_only_without_skill"], [])
        self.assertEqual(case["skill_invoked"], {"with_skill": 1.0, "without_skill": 0.0})
        self.assertEqual(case["mean_deltas"]["commands"], 2.0)

    def test_careful_path_passes_the_process_and_per_step_checks(self):
        report, verdicts = run_demo_suite(self, TRAJECTORY_MANIFEST)
        for name in ("severity-label", "skill-read", "no-reread-loop", "sound-steps"):
            with self.subTest(name=name):
                self.assertTrue(assertion_row(report, "c-review-path", "with_skill", name)["passed"])
        per_step = next(v for v in verdicts if v["judge_task_id"].endswith("::sound-steps"))
        self.assertEqual(per_step["criteria"], [{"name": "step-1", "met": True},
                                                {"name": "step-2", "met": True}])

    def test_looping_path_keeps_the_answer_and_fails_only_the_path_checks(self):
        report, verdicts = run_demo_suite(self, TRAJECTORY_MANIFEST, runner_flags=" --loop")
        self.assertTrue(assertion_row(report, "c-review-path", "with_skill", "severity-label")["passed"])
        self.assertTrue(assertion_row(report, "c-review-path", "with_skill", "skill-read")["passed"])
        self.assertFalse(assertion_row(report, "c-review-path", "with_skill", "no-reread-loop")["passed"])
        self.assertFalse(assertion_row(report, "c-review-path", "with_skill", "sound-steps")["passed"])
        per_step = next(v for v in verdicts if v["judge_task_id"].endswith("::sound-steps"))
        self.assertEqual([c["met"] for c in per_step["criteria"]], [True, True, False, False])
        case = next(c for c in report["trajectory_diff"]["cases"] if c["case_id"] == "c-review-path")
        self.assertEqual(case["mean_deltas"]["commands"], 4.0)

    def test_lenient_per_step_judge_misses_the_loop(self):
        # The per-step verdict is a model judgment (oracle tier live); a
        # rubber-stamp judge passes the looping path the deterministic
        # no-reread-loop assertion still fails.
        report, _ = run_demo_suite(self, TRAJECTORY_MANIFEST, runner_flags=" --loop",
                                   judge_flags=" --lenient")
        self.assertTrue(assertion_row(report, "c-review-path", "with_skill", "sound-steps")["passed"])
        self.assertFalse(assertion_row(report, "c-review-path", "with_skill", "no-reread-loop")["passed"])


class DemoDiscoveryJourneyTests(unittest.TestCase):
    """Pins docs/did-removing-this-break-discovery.md: the weaker-description
    ablation (removing when_to_use) is refuted on the manifest's queries,
    indeterminate on three queries in the users' words, and confirmed on the
    full eval set — each through trigger-compare's causal gate."""

    @staticmethod
    def _compare(rows):
        kwargs = {"agents": ["stub"], "models": None, "runs_per_query": 3, "timeout": 30, "workers": 1}
        baseline = tm.run_matrix(DEMO_MANIFEST, rows, **kwargs)
        ablated = tm.run_matrix(DEMO_MANIFEST, rows, ablation="weaker-description", **kwargs)
        return sb.build_trigger_comparison(baseline, ablated)

    @staticmethod
    def _eval_set_rows():
        return tr.validate_trigger_rows(
            json.loads(TRIGGER_EVAL_SET.read_text(encoding="utf-8"))["queries"],
            str(TRIGGER_EVAL_SET))

    def test_manifest_queries_refute_the_ablation(self):
        rows = tm.cases_from_manifest(tm.load_manifest(DEMO_MANIFEST), None)
        out = self._compare(rows)
        self.assertTrue(out["provenance"]["verified"])
        self.assertEqual(out["evidence_class"], "refuted")
        self.assertEqual(out["regressed_queries"], [])

    def test_three_queries_in_the_users_words_are_indeterminate(self):
        rows = [r for r in self._eval_set_rows() if r["query_id"].startswith("wtu-")][:3]
        out = self._compare(rows)
        self.assertEqual(out["evidence_class"], "indeterminate")
        self.assertEqual(len(out["regressed_queries"]), 3)
        self.assertIn("not significant", out["note"])

    def test_full_eval_set_confirms_the_ablation(self):
        rows = self._eval_set_rows()
        out = self._compare(rows)
        self.assertEqual(out["evidence_class"], "confirmed_causal")
        regressed = {r["query_id"] for r in out["regressed_queries"]}
        self.assertEqual(regressed, {r["query_id"] for r in rows if r["query_id"].startswith("wtu-")})
        self.assertEqual(len(regressed), 6)


if __name__ == "__main__":
    unittest.main()
