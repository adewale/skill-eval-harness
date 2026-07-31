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

from agent_capabilities import AGENT_CAPABILITIES, SMOKE_TARGETS, SmokeTarget
from trigger_contracts import InvocationOutcome

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

    def test_smoke_targets_are_capability_qualified_and_own_model_defaults(self):
        expected = {
            "claude": ("SMOKE_CLAUDE_MODEL", "haiku", "answer"),
            "codex": ("SMOKE_CODEX_MODEL", "gpt-5.4-mini", "answer"),
            "gemini": ("SMOKE_GEMINI_MODEL", "gemini-2.5-flash", "answer"),
            "vibe": ("SMOKE_VIBE_MODEL", "devstral-small-latest", "answer"),
            "pi": ("SMOKE_PI_MODEL", "openai-codex/gpt-5.4-mini", "trigger"),
        }
        self.assertEqual(
            {name: (target.model_env, target.fallback_model, target.population)
             for name, target in SMOKE_TARGETS.items()},
            expected,
        )
        self.assertEqual(set(smoke.DEFAULT_MODELS), set(SMOKE_TARGETS))
        for name, target in SMOKE_TARGETS.items():
            capability = AGENT_CAPABILITIES[name]
            self.assertTrue(capability.answer_runner if target.population == "answer" else capability.autonomous_trigger)
            self.assertEqual(target.resolved_model({target.model_env: "  "}), target.fallback_model)
            self.assertEqual(target.resolved_model({target.model_env: "custom/model"}), "custom/model")
        parser = smoke.argparse.ArgumentParser()
        with mock.patch.object(smoke.argparse, "ArgumentParser", return_value=parser):
            # parse_args owns one --<agent>-model option per registry target.
            with mock.patch.object(parser, "parse_args", return_value=None):
                smoke.parse_args()
        defaults = {
            action.dest: action.default for action in parser._actions
            if action.dest.endswith("_model")
        }
        self.assertEqual(defaults, {f"{name}_model": smoke.DEFAULT_MODELS[name] for name in SMOKE_TARGETS})

    def test_smoke_target_rejects_invalid_registry_states(self):
        for args in (("", "ENV", "model", "answer"), ("pi", "", "model", "trigger"),
                     ("pi", "ENV", "", "trigger"), ("pi", "ENV", "model", "other")):
            with self.subTest(args=args), self.assertRaises(ValueError):
                SmokeTarget(*args)

    def test_answer_assessment_rejects_trace_zeros_when_trace_is_incomplete(self):
        def row(availability):
            trace_keys = ("tool_calls", "commands", "file_reads", "file_writes",
                          "errors", "retries", "repeated_command_max", "skill_invoked")
            return {
                "variant": "with_skill", "execution_valid": True,
                "missing_output": False, "objective_pass_rate": 1.0,
                "metadata": {
                    "observation_complete": True,
                    "trace_observation_complete": False,
                    "telemetry": {
                        "schema_version": 3,
                        "observation_evidence": {
                            "schema_version": 1,
                            "process": {"state": "complete"},
                            "provider_response": {"state": "complete"},
                            "trace": {"state": "incomplete"},
                            "artifact_set": {"state": "complete"},
                            "operation_evidence_complete": False,
                        },
                        "measurements": {
                            key: {"availability": availability} for key in trace_keys
                        },
                    },
                },
            }

        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "benchmark.json"
            for availability, expected in (("available", False), ("unavailable", True)):
                with self.subTest(availability=availability):
                    path.write_text(json.dumps({"results": [row(availability)]}), encoding="utf-8")
                    report = {"checks": []}
                    self.assertIs(smoke.assess_answer_benchmark(path, "claude", report), expected)
                    telemetry = next(check for check in report["checks"]
                                     if check["label"] == "claude:telemetry-contract")
                    self.assertIs(telemetry["passed"], expected)

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

    def test_trigger_assessment_rejects_a_persisted_pass_that_contradicts_provider_failure(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "pi-trigger.json"
            rows = []
            for query, should_trigger in smoke.SMOKE_TRIGGER_EXPECTATIONS:
                rows.append({
                    "agent": "pi", "model": "bad", "query": query,
                    "should_trigger": should_trigger, "triggered": should_trigger,
                    "pass": True, "observation_complete": True,
                    "returncode": 0, "timed_out": False, "elapsed_ms": 1,
                    "evidence": ["/tmp/skills/demo/SKILL.md"] if should_trigger else [],
                    "usage_normalized": {"source": "missing"},
                    "cost_normalized": {"source": "missing"},
                    "stderr": "", "provider_error": "provider rejected model",
                })
            path.write_text(json.dumps({"results": rows}), encoding="utf-8")
            report = {"checks": []}
            self.assertFalse(smoke.assess_trigger_report(path, report))
            self.assertFalse(report["checks"][0]["passed"])

    def test_trigger_assessment_turns_malformed_rows_into_a_failed_check(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "bad-trigger.json"
            path.write_text(json.dumps({"results": [None, None]}), encoding="utf-8")
            report = {"checks": []}
            self.assertFalse(smoke.assess_trigger_report(path, report))
            self.assertFalse(report["checks"][0]["passed"])

    def test_failed_prepare_short_circuits_before_any_answer_call(self):
        with tempfile.TemporaryDirectory() as td:
            args = argparse.Namespace(out_dir=str(Path(td) / "out"), live=True, agents="claude",
                                      claude_model="haiku", codex_model="unused",
                                      gemini_model="unused", vibe_model="unused",
                                      pi_model="unused", timeout=1)
            with mock.patch.object(smoke, "parse_args", return_value=args), \
                 mock.patch.object(smoke.shutil, "which", return_value="/mock/claude"), \
                 mock.patch.object(smoke, "run", return_value=False) as run:
                self.assertEqual(smoke.main(), 1)
            self.assertEqual(run.call_count, 1)
            self.assertEqual(run.call_args.kwargs["label"], "claude:prepare")

    def test_smoke_subprocess_result_is_derived_from_typed_invocation_state(self):
        outcomes = [
            (InvocationOutcome.from_process(stdout="x" * 5000, stderr="y" * 5000, returncode=0, elapsed_ms=1234), True),
            (InvocationOutcome.from_process(stdout="", stderr="bad", returncode=1, elapsed_ms=2), False),
            (InvocationOutcome.from_process(stdout="", stderr="timeout", returncode=124, elapsed_ms=3), False),
            (InvocationOutcome.from_process(stdout="", stderr="missing", returncode=127, elapsed_ms=4), False),
        ]
        for outcome, expected in outcomes:
            with self.subTest(state=outcome.state), mock.patch.object(
                smoke, "invoke_argv_with_timeout", return_value=outcome,
            ):
                report = {"commands": []}
                self.assertIs(smoke.run(["cmd"], cwd=ROOT, report=report, label="x"), expected)
                entry = report["commands"][0]
                self.assertEqual(entry["state"], outcome.state.value)
                self.assertEqual(entry["returncode"], outcome.returncode)
                self.assertEqual(entry["elapsed_seconds"], round(outcome.elapsed_ms / 1000, 3))
                self.assertLessEqual(len(entry["stdout"]), 4000)
                self.assertLessEqual(len(entry["stderr"]), 4000)

    def test_registry_population_dispatches_pi_to_trigger_runner(self):
        with tempfile.TemporaryDirectory() as td:
            args = argparse.Namespace(out_dir=str(Path(td) / "out"), live=True, agents="pi",
                                      claude_model="unused", codex_model="unused",
                                      gemini_model="unused", vibe_model="unused",
                                      pi_model="model", timeout=1)
            with mock.patch.object(smoke, "parse_args", return_value=args), \
                 mock.patch.object(smoke.shutil, "which", return_value="/mock/pi"), \
                 mock.patch.object(smoke, "run", return_value=True) as run, \
                 mock.patch.object(smoke, "assess_trigger_report", return_value=True):
                self.assertEqual(smoke.main(), 0)
            self.assertEqual(run.call_count, 1)
            self.assertEqual(run.call_args.kwargs["label"], "pi:trigger")
            self.assertIn("run_trigger_matrix.py", run.call_args.args[0][1])

    def test_registry_dispatch_supports_a_non_pi_trigger_target(self):
        synthetic = SmokeTarget("other", "SMOKE_OTHER_MODEL", "cheap", "trigger")
        with tempfile.TemporaryDirectory() as td:
            args = argparse.Namespace(out_dir=str(Path(td) / "out"), live=True, agents="other",
                                      other_model="cheap", timeout=1)
            with mock.patch.object(smoke, "SMOKE_TARGETS", {"other": synthetic}), \
                 mock.patch.object(smoke, "parse_args", return_value=args), \
                 mock.patch.object(smoke.shutil, "which", return_value="/mock/other"), \
                 mock.patch.object(smoke, "run", return_value=True) as run, \
                 mock.patch.object(smoke, "assess_trigger_report", return_value=True) as assess:
                self.assertEqual(smoke.main(), 0)
            command = run.call_args.args[0]
            self.assertEqual(command[command.index("--agent") + 1], "other")
            self.assertEqual(command[command.index("--model") + 1], "cheap")
            self.assertEqual(run.call_args.kwargs["label"], "other:trigger")
            self.assertEqual(assess.call_args.args[2], "other")

    def test_live_acknowledgement_is_required_before_any_cli_call(self):
        with tempfile.TemporaryDirectory() as td:
            completed = subprocess.run(
                [sys.executable, str(SCRIPT), "--out-dir", str(Path(td) / "out")],
                text=True, capture_output=True, check=False,
            )
        self.assertEqual(completed.returncode, 2)
        self.assertIn("--live", completed.stderr)

    def test_live_smoke_rejects_an_empty_agent_selection(self):
        with tempfile.TemporaryDirectory() as td:
            completed = subprocess.run(
                [sys.executable, str(SCRIPT), "--live", "--agents", " , ", "--out-dir", str(Path(td) / "out")],
                text=True, capture_output=True, check=False,
            )
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("at least one", completed.stderr)


if __name__ == "__main__":
    unittest.main()
