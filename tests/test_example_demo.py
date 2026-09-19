"""The bundled offline example is executable documentation: prepare -> run (with the
deterministic stub 'model') -> report, and the two materialized ablations each
confirm a regression on a distinct assertion. Runs in CI with no model/API."""
import argparse
import json
import sys
import tempfile
import unittest
from pathlib import Path

import skill_benchmark as sb

ROOT = Path(__file__).resolve().parents[1]
DEMO = ROOT / "examples" / "demo-skill"


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
        # Two runners, one JSONL, one runs dir. The native stub runner skips the
        # fault case; the subagent runner takes only it, driving the stub tool
        # bridge under strict replay so the declared denial is the only tool
        # result that exists. Both attest the same answer design.
        sb.run_codex(argparse.Namespace(tasks=str(td / "tasks.jsonl"), runs=str(td / "runs"),
                                        codex_cmd=stub, timeout=120, skip_fault_cases=True))
        sb.run_subagent(argparse.Namespace(
            tasks=str(td / "tasks.jsonl"), runs=str(td / "runs"),
            agent_cmd=f"{sys.executable} {DEMO / 'stub_bridge.py'}", tool_bridge=True,
            tool_replay="strict", only_fault_cases=True, timeout=120, model=None))
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
        for aid, assertion in (("no-severity", "severity-label"), ("no-checklist", "cite-checklist"),
                               ("no-fallback", "fallback-declared")):
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

    def test_fault_case_measures_the_fallback_rule_not_the_model(self):
        """Both arms get the same denied runner. Only the skill's fallback text
        differs, and it shows as one error result versus two, a declared
        fallback versus a rubber-stamp, and a paired errors delta of -1."""
        rep = self._run()
        fault = next(c for c in rep["trajectory_diff"]["cases"] if c["case_id"] == "c-fault-denied-runner")
        self.assertEqual(fault["pairs"], 6)
        self.assertEqual(fault["mean_deltas"]["errors"], -1.0)
        self.assertEqual(fault["mean_deltas"]["tool_calls"], -1.0)
        rows = [r for r in rep["results"] if r["case_id"] == "c-fault-denied-runner"]
        by_variant = {}
        for row in rows:
            by_variant.setdefault(row["variant"], set()).add(
                tuple(sorted((a["name"], a["passed"]) for a in row["assertions"])))
        self.assertEqual(by_variant["with_skill"],
                         {(("denied-once", True), ("fallback-declared", True), ("severity-label", True))})
        self.assertEqual(by_variant["without_skill"],
                         {(("denied-once", False), ("fallback-declared", False), ("severity-label", False))})
        self.assertEqual(by_variant["ablation:no-fallback"], by_variant["without_skill"])
        # The other ablations leave the fallback section intact, so they pass the fault case.
        self.assertEqual(by_variant["ablation:no-severity"], by_variant["with_skill"])


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
        # The judge calibration story is the c-review arms; fault rows belong to
        # the tool bridge (DemoExampleTests) and are left out of this design.
        rows = [r for r in rows if r["variant"] in self.VARIANTS and not r.get("tool_faults")]
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


if __name__ == "__main__":
    unittest.main()
