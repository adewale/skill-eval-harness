"""Offline contract tests for the opt-in supported-CLI live-smoke runner."""
from __future__ import annotations

import argparse
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "smoke_supported_clis.py"
_spec = importlib.util.spec_from_file_location("smoke_supported_clis", SCRIPT)
assert _spec and _spec.loader
smoke = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(smoke)


class SupportedCliSmokeTests(unittest.TestCase):
    def test_minimal_disposable_manifest_has_one_answer_and_both_trigger_polarities(self):
        with tempfile.TemporaryDirectory() as td:
            manifest_path = smoke.make_smoke_repo(Path(td))
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            answer = [case for case in manifest["cases"] if case["kind"] != "trigger"]
            triggers = [case for case in manifest["cases"] if case["kind"] == "trigger"]
            self.assertEqual(len(answer), 1)
            self.assertEqual(len(triggers), 2)
            self.assertEqual({case["id"] for case in triggers}, {"trig-pos-review-change", "trig-neg-unrelated-question"})
            self.assertTrue((manifest_path.parent.parent / "skills" / "demo" / "SKILL.md").exists())

    def test_pi_default_uses_the_codex_provider(self):
        self.assertEqual(smoke.DEFAULT_MODELS["pi"], "openai-codex/gpt-5.4-mini")

    def test_trigger_assessment_requires_the_exact_positive_and_negative_fixture_rows(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "pi-trigger.json"
            report = {"checks": []}
            path.write_text(json.dumps({"results": [{
                "query": smoke.SMOKE_TRIGGER_EXPECTATIONS[0][0], "should_trigger": True,
                "observation_complete": True, "returncode": 0, "pass": True,
            }]}), encoding="utf-8")
            self.assertFalse(smoke.assess_trigger_report(path, report))
            self.assertFalse(report["checks"][0]["passed"])

    def test_failed_prepare_short_circuits_before_any_answer_call(self):
        with tempfile.TemporaryDirectory() as td:
            args = argparse.Namespace(out_dir=str(Path(td) / "out"), live=True, agents="claude",
                                      claude_model="haiku", codex_model="unused", vibe_model="unused",
                                      pi_model="unused", timeout=1)
            with mock.patch.object(smoke, "parse_args", return_value=args), \
                 mock.patch.object(smoke.shutil, "which", return_value="/mock/claude"), \
                 mock.patch.object(smoke, "run", return_value=False) as run:
                self.assertEqual(smoke.main(), 1)
            self.assertEqual(run.call_count, 1)
            self.assertEqual(run.call_args.kwargs["label"], "claude:prepare")

    def test_live_acknowledgement_is_required_before_any_cli_call(self):
        with tempfile.TemporaryDirectory() as td:
            completed = subprocess.run(
                [sys.executable, str(SCRIPT), "--out-dir", str(Path(td) / "out")],
                text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
            )
        self.assertEqual(completed.returncode, 2)
        self.assertIn("--live", completed.stderr)

    def test_live_smoke_rejects_an_empty_agent_selection(self):
        with tempfile.TemporaryDirectory() as td:
            completed = subprocess.run(
                [sys.executable, str(SCRIPT), "--live", "--agents", " , ", "--out-dir", str(Path(td) / "out")],
                text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
            )
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("at least one", completed.stderr)


if __name__ == "__main__":
    unittest.main()
