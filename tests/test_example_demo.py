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
        td = Path(tempfile.mkdtemp(prefix="demo-eval-"))
        rows = sb.prepared_task_rows(mp, manifest, include_ablations=True, ablation_dir=str(td / "abl"))
        (td / "tasks.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
        stub = f"{sys.executable} {DEMO / 'stub_runner.py'}"
        sb.run_codex(argparse.Namespace(tasks=str(td / "tasks.jsonl"), runs=str(td / "runs"), codex_cmd=stub, timeout=120))
        variants = sorted({r["variant"] for r in rows})   # include the ablation arms, not just the manifest variants
        return sb.build_benchmark_report(mp, td / "runs", variants_arg=variants)

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


if __name__ == "__main__":
    unittest.main()
