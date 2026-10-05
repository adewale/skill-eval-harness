import json
import tempfile
import unittest
from pathlib import Path

from helpers import make_eval_repo, run_cli

import skill_benchmark as sb

# One answer case with a judge assertion and one trigger case: the suite scope
# counts both, and the judge assertion once per tune pair.
SUITE_CASES = [
    {"id": "pos-one", "split": "tune", "kind": "behavior", "prompt": "Do the thing.",
     "expected_behavior": ["Does the thing"],
     "assertions": [{"name": "mentions-done", "type": "contains_any", "values": ["done"]},
                    {"name": "judge-quality", "type": "judge", "rubric": ["clear"]}]},
    {"id": "trig-one", "split": "tune", "kind": "trigger", "should_trigger": True,
     "prompt": "Trigger decision eval. User prompt: do the thing",
     "expected_behavior": ["Should trigger"],
     "assertions": [{"name": "label", "type": "regex", "pattern": "TRIGGER"}]},
]


class SuiteRunTests(unittest.TestCase):
    def _repo(self, root: Path, name: str, *, skill_name: str | None = None, ablations: list[dict] | None = None) -> Path:
        """A repo at root/<name>, where suite scope discovers top-level manifests."""
        make_eval_repo(root, skill_name=skill_name or name, cases=SUITE_CASES, ablations=ablations)
        return (root / "repo").rename(root / name)

    def _pins(self, root: Path, repo: str) -> Path:
        manifest_path = root / repo / "evals" / "shared-benchmark.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        tree_hash = sb.canonical_skill_tree_hash(root / repo, manifest)
        pins = root / "pins.json"
        pins.write_text(json.dumps({"skills": {manifest["skill_name"]: {"tree_hash": tree_hash}}}, indent=2), encoding="utf-8")
        return pins

    def test_suite_scope_verifies_pins_and_estimates_rows(self):
        tmp = tempfile.TemporaryDirectory(prefix="suite-scope-")
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self._repo(root, "allowed", ablations=[{"id": "no-checklist", "removed_component": "checklist", "expected_regressions": ["less useful"]}])
        suite = root / "suite.txt"
        suite.write_text("allowed/evals/shared-benchmark.json\n", encoding="utf-8")
        pins = self._pins(root, "allowed")

        scope = sb.build_suite_scope(suite, root, pins_file=pins, include_ablations=True)

        self.assertEqual(scope["status"], "preflight_ok")
        self.assertEqual(scope["manifests"][0]["pin"]["status"], "verified")
        self.assertEqual(scope["totals"]["skills"], 1)
        self.assertEqual(scope["totals"]["tune_cases"], 2)
        self.assertEqual(scope["totals"]["baseline_rows"], 4)  # 2 cases x with/without
        self.assertEqual(scope["totals"]["ablation_rows"], 5)  # baseline + 1 answer case x 1 ablation
        self.assertEqual(scope["totals"]["judge_assertions_tune_pair"], 2)

    def test_suite_run_writes_run_scope_before_returning_blocked(self):
        tmp = tempfile.TemporaryDirectory(prefix="suite-run-")
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self._repo(root, "allowed")
        # A sibling repo whose directory and skill names differ is still found.
        self._repo(root, "beautiful-mermaid", skill_name="agentic-mermaid-project-skills")
        suite = root / "suite.txt"
        suite.write_text("allowed/evals/shared-benchmark.json\n", encoding="utf-8")
        pins = self._pins(root, "allowed")
        out = root / "out"

        code, _, stderr = run_cli("suite-run", suite, "--workspace-root", root, "--pins", pins, "--out-dir", out)

        self.assertEqual(code, 2)
        self.assertIn("FAIL: extra top-level manifests not in suite allowlist: "
                      "beautiful-mermaid/evals/shared-benchmark.json", stderr)
        written = json.loads((out / "RUN_SCOPE.json").read_text(encoding="utf-8"))
        self.assertEqual(written["status"], "blocked")
        self.assertEqual(written["extra_manifests"], ["beautiful-mermaid/evals/shared-benchmark.json"])
        self.assertTrue(any("extra top-level manifests" in b for b in written["blockers"]))

    def test_prepare_tier_runs_only_allowlisted_manifest(self):
        tmp = tempfile.TemporaryDirectory(prefix="suite-prepare-")
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self._repo(root, "allowed")
        suite = root / "suite.txt"
        suite.write_text("allowed/evals/shared-benchmark.json\n", encoding="utf-8")
        pins = self._pins(root, "allowed")
        out = root / "out"

        code, _, _ = run_cli("suite-run", suite, "--workspace-root", root, "--pins", pins,
                             "--out-dir", out, "--tier", "prepare")

        self.assertEqual(code, 0)
        scope = json.loads((out / "RUN_SCOPE.json").read_text(encoding="utf-8"))
        self.assertEqual(scope["status"], "completed")
        self.assertEqual(len(scope["commands_run"]), 1)
        self.assertTrue((out / "tasks" / "allowed.tasks.jsonl").exists())


if __name__ == "__main__":
    unittest.main()
