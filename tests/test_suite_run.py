import argparse
import json
import tempfile
import unittest
from pathlib import Path

import skill_benchmark as sb


class SuiteRunTests(unittest.TestCase):
    def _repo(self, root: Path, name: str, *, skill_name: str | None = None, ablations: list[dict] | None = None) -> Path:
        repo = root / name
        (repo / "skills" / name).mkdir(parents=True)
        (repo / "evals").mkdir()
        (repo / "skills" / name / "SKILL.md").write_text(
            "---\n"
            f"name: {skill_name or name}\n"
            f"description: Use {skill_name or name} when asked.\n"
            "---\n\n"
            "# Skill\n\n"
            "Follow the checklist.\n",
            encoding="utf-8",
        )
        manifest = {
            "version": 1,
            "skill_name": skill_name or name,
            "skill_paths": [f"skills/{name}/SKILL.md"],
            "variants": ["with_skill", "without_skill"],
            "cases": [
                {
                    "id": "pos-one",
                    "split": "tune",
                    "kind": "behavior",
                    "prompt": "Do the thing.",
                    "expected_behavior": ["Does the thing"],
                    "assertions": [
                        {"name": "mentions-done", "type": "contains_any", "values": ["done"]},
                        {"name": "judge-quality", "type": "judge", "rubric": ["clear"]},
                    ],
                },
                {
                    "id": "trig-one",
                    "split": "tune",
                    "kind": "trigger",
                    "prompt": "Trigger decision eval. User prompt: do the thing",
                    "expected_behavior": ["Should trigger"],
                    "assertions": [{"name": "label", "type": "regex", "pattern": "TRIGGER"}],
                },
            ],
            "ablations": ablations or [],
        }
        (repo / "evals" / "shared-benchmark.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        return repo

    def _pins(self, root: Path, repo: str) -> Path:
        manifest_path = root / repo / "evals" / "shared-benchmark.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        tree_hash = sb.canonical_skill_tree_hash(root / repo, manifest)
        pins = root / "pins.json"
        pins.write_text(json.dumps({"skills": {manifest["skill_name"]: {"tree_hash": tree_hash}}}, indent=2), encoding="utf-8")
        return pins

    def test_suite_scope_refuses_extra_top_level_manifest(self):
        tmp = tempfile.TemporaryDirectory(prefix="suite-scope-")
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self._repo(root, "allowed")
        self._repo(root, "beautiful-mermaid", skill_name="agentic-mermaid-project-skills")
        suite = root / "suite.txt"
        suite.write_text("allowed/evals/shared-benchmark.json\n", encoding="utf-8")
        pins = self._pins(root, "allowed")

        scope = sb.build_suite_scope(suite, root, pins_file=pins)

        self.assertIn("beautiful-mermaid/evals/shared-benchmark.json", scope["extra_manifests"])
        self.assertEqual(scope["status"], "blocked")
        self.assertTrue(any("extra top-level manifests" in b for b in scope["blockers"]))

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
        self._repo(root, "extra")
        suite = root / "suite.txt"
        suite.write_text("allowed/evals/shared-benchmark.json\n", encoding="utf-8")
        pins = self._pins(root, "allowed")
        out = root / "out"

        code = sb.suite_run(argparse.Namespace(
            suite_file=str(suite),
            workspace_root=str(root),
            pins=str(pins),
            out_dir=str(out),
            tier="preflight",
            split="tune",
            runs_per_variant=1,
            include_ablations=False,
            allow_extra_manifests=False,
            skip_pin_check=False,
        ))

        self.assertEqual(code, 2)
        written = json.loads((out / "RUN_SCOPE.json").read_text(encoding="utf-8"))
        self.assertEqual(written["status"], "blocked")
        self.assertIn("extra/evals/shared-benchmark.json", written["extra_manifests"])

    def test_prepare_tier_runs_only_allowlisted_manifest(self):
        tmp = tempfile.TemporaryDirectory(prefix="suite-prepare-")
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self._repo(root, "allowed")
        suite = root / "suite.txt"
        suite.write_text("allowed/evals/shared-benchmark.json\n", encoding="utf-8")
        pins = self._pins(root, "allowed")
        out = root / "out"

        code = sb.suite_run(argparse.Namespace(
            suite_file=str(suite),
            workspace_root=str(root),
            pins=str(pins),
            out_dir=str(out),
            tier="prepare",
            split="tune",
            runs_per_variant=1,
            include_ablations=False,
            allow_extra_manifests=False,
            skip_pin_check=False,
        ))

        self.assertEqual(code, 0)
        scope = json.loads((out / "RUN_SCOPE.json").read_text(encoding="utf-8"))
        self.assertEqual(scope["status"], "completed")
        self.assertEqual(len(scope["commands_run"]), 1)
        self.assertTrue((out / "tasks" / "allowed.tasks.jsonl").exists())


if __name__ == "__main__":
    unittest.main()
