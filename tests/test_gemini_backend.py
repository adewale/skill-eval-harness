"""Offline integration tests for the official Gemini CLI backend."""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from helpers import make_eval_repo

import ablation_model as am
import agent_capabilities as ac
import judge_contracts as jc
import runner_contracts as rc
import skill_benchmark as sb


def _event(kind: str, **values: object) -> dict[str, object]:
    return {
        "type": kind,
        "timestamp": "2026-07-31T09:00:00.000Z",
        **values,
    }


def _success_stream(answer: str = "answer from Gemini") -> str:
    records = [
        _event("init", session_id="session-1", model="gemini-test"),
        _event("message", role="assistant", content=answer, delta=True),
        _event("result", status="success", stats={
            "total_tokens": 7,
            "input_tokens": 4,
            "output_tokens": 3,
            "cached": 1,
            "input": 3,
            "duration_ms": 25,
            "tool_calls": 0,
            "models": {
                "gemini-test": {
                    "total_tokens": 7,
                    "input_tokens": 4,
                    "output_tokens": 3,
                    "cached": 1,
                    "input": 3,
                },
            },
        }),
    ]
    return "\n".join(json.dumps(record) for record in records) + "\n"


def _judge_envelope() -> str:
    return json.dumps({
        "session_id": "session-judge",
        "response": json.dumps({
            "passed": True,
            "score": 1,
            "rationale": "Gemini judge ok",
        }),
        "stats": {
            "models": {
                "gemini-judge": {
                    "api": {"totalRequests": 1, "totalErrors": 0,
                            "totalLatencyMs": 20},
                    "tokens": {
                        "input": 5,
                        "prompt": 6,
                        "candidates": 2,
                        "total": 8,
                        "cached": 1,
                        "thoughts": 0,
                        "tool": 0,
                    },
                    "roles": {},
                },
            },
            "tools": {"totalCalls": 0},
            "files": {"totalLinesAdded": 0, "totalLinesRemoved": 0},
        },
    })


class GeminiRegistryTests(unittest.TestCase):
    def test_one_registry_row_projects_every_supported_surface_truthfully(self):
        registration = ac.BACKENDS["gemini"]

        self.assertTrue(registration.capabilities.answer_runner)
        self.assertTrue(registration.capabilities.judge_backend)
        self.assertTrue(registration.capabilities.trace_artifacts)
        self.assertTrue(registration.capabilities.token_usage)
        self.assertEqual(registration.capabilities.dollar_cost, "missing")
        self.assertFalse(registration.capabilities.autonomous_trigger)
        self.assertFalse(registration.capabilities.trigger_ablation)
        self.assertIsNone(registration.trigger)
        self.assertEqual(registration.answer_route, "native")
        self.assertIn("run-agent", [
            entrypoint.command for entrypoint in registration.answer_entrypoints])
        self.assertIsInstance(sb.AGENT_BACKENDS["gemini"], sb.GeminiBackend)
        self.assertIs(sb.JUDGE_BACKENDS["gemini"], sb.gemini_judge_invoke)
        self.assertIs(sb.TRACE_DIALECTS["gemini"], sb.GEMINI_TRACE_DIALECT)
        self.assertEqual(rc.Provider.GEMINI.value, "gemini")
        self.assertEqual(am.RUNNER_FAILURE_MARKER_BY_PROVIDER["gemini"],
                         "[GEMINI FAILURE")

    def test_gemini_cli_flag_is_projected_to_answer_and_judge(self):
        parser = sb.build_arg_parser()
        subs = next(action for action in parser._actions
                    if action.__class__.__name__ == "_SubParsersAction")
        for command in ("run-agent", "judge"):
            flags = {
                flag: action for action in subs.choices[command]._actions
                for flag in getattr(action, "option_strings", ())
            }
            self.assertIn("--gemini-cmd", flags)
            self.assertEqual(flags["--gemini-cmd"].default,
                             ac.GEMINI_DEFAULT_CMD)


class GeminiIsolationTests(unittest.TestCase):
    def test_home_seeding_copies_only_minimal_auth_and_selected_auth_type(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source_root = root / "source"
            source = source_root / ".gemini"
            (source / "skills" / "ambient").mkdir(parents=True)
            (source / "extensions" / "ambient").mkdir(parents=True)
            (source / "policies").mkdir(parents=True)
            (source / "oauth_creds.json").write_text(
                '{"refresh_token":"secret"}', encoding="utf-8")
            (source / "gemini-credentials.json").write_text(
                '{"access_token":"secret"}', encoding="utf-8")
            (source / "settings.json").write_text(json.dumps({
                "security": {"auth": {"selectedType": "oauth-personal"}},
                "mcpServers": {"ambient": {"command": "unsafe"}},
                "hooks": {"BeforeTool": [{"command": "unsafe"}]},
                "context": {"fileName": "AMBIENT.md"},
            }), encoding="utf-8")
            (source / "skills" / "ambient" / "SKILL.md").write_text(
                "ambient", encoding="utf-8")
            (source / "extensions" / "ambient" / "gemini-extension.json").write_text(
                "{}", encoding="utf-8")
            (source / "policies" / "ambient.toml").write_text(
                '[[rule]]\ntoolName="*"\ndecision="allow"\npriority=999\n',
                encoding="utf-8")
            target_root = root / "isolated"

            with mock.patch.dict(os.environ, {
                    "GEMINI_CLI_HOME": str(source_root)}, clear=True):
                metadata = sb.seed_gemini_home(target_root)

            target = target_root / ".gemini"
            self.assertEqual(set(metadata["gemini_auth_files_copied"]), {
                "oauth_creds.json", "gemini-credentials.json"})
            self.assertEqual(json.loads((target / "settings.json").read_text()), {
                "security": {"auth": {"selectedType": "oauth-personal"}},
            })
            self.assertFalse((target / "skills").exists())
            self.assertFalse((target / "extensions").exists())
            self.assertFalse((target / "policies" / "ambient.toml").exists())

    def test_environment_auth_prevents_credential_file_copy(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source = root / "source" / ".gemini"
            source.mkdir(parents=True)
            (source / "oauth_creds.json").write_text("{}", encoding="utf-8")
            with mock.patch.dict(os.environ, {
                    "GEMINI_CLI_HOME": str(root / "source"),
                    "GEMINI_API_KEY": "secret",
            }, clear=True):
                metadata = sb.seed_gemini_home(root / "isolated")
            self.assertEqual(metadata["gemini_auth_files_copied"], [])


class GeminiAnswerBackendTests(unittest.TestCase):
    def _one_with_skill_task(self, root: Path) -> tuple[Path, str]:
        manifest = make_eval_repo(root)
        rows = sb.prepared_task_rows(
            manifest, sb.validate_manifest(manifest), split="tune")
        row = next(item for item in rows if item["variant"] == "with_skill")
        tasks = root / "tasks.jsonl"
        tasks.write_text(json.dumps(row) + "\n", encoding="utf-8")
        return tasks, row["run_dir"]

    def test_run_agent_uses_headless_stream_json_and_writes_complete_artifacts(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            tasks, run_dir = self._one_with_skill_task(root)
            fake = root / "fake_gemini.py"
            fake.write_text(
                "import json, os, pathlib, sys\n"
                "prompt = sys.argv[sys.argv.index('--prompt') + 1]\n"
                "assert sys.argv[sys.argv.index('--output-format') + 1] == 'stream-json'\n"
                "assert sys.argv[sys.argv.index('--model') + 1] == 'gemini-test'\n"
                "assert '--skip-trust' in sys.argv and '--sandbox' in sys.argv\n"
                "policy = pathlib.Path(sys.argv[sys.argv.index('--policy') + 1])\n"
                "policy_text = policy.read_text()\n"
                "assert 'toolName = \"*\"' in policy_text\n"
                "assert 'toolName = [\"glob\", \"grep_search\", \"list_directory\", \"read_file\", \"read_many_files\"]' in policy_text\n"
                "home = pathlib.Path(os.environ['GEMINI_CLI_HOME'])\n"
                "assert home.is_dir() and not home.is_relative_to(pathlib.Path.cwd())\n"
                "assert not (home / '.gemini' / 'skills').exists()\n"
                "assert not (home / '.gemini' / 'extensions').exists()\n"
                "assert 'Task prompt:' in prompt\n"
                f"sys.stdout.write({_success_stream()!r})\n",
                encoding="utf-8")
            runs = root / "runs"

            result = sb.run_agent(argparse.Namespace(
                agent="gemini", tasks=str(tasks), runs=str(runs),
                model="gemini-test", gemini_cmd=f"{sys.executable} {fake}",
                timeout=30,
            ))

            self.assertEqual(result, 0)
            base = runs / run_dir
            self.assertEqual((base / "output.md").read_text(encoding="utf-8"),
                             "answer from Gemini")
            metadata = json.loads((base / "metadata.json").read_text())
            self.assertEqual(metadata["provider"], "gemini")
            self.assertEqual(metadata["model"], "gemini-test")
            self.assertEqual(metadata["resolved_model"], "gemini-test")
            self.assertEqual(metadata["usage_normalized"]["total_tokens"], 7)
            self.assertEqual(metadata["cost_normalized"], {"source": "missing"})
            self.assertTrue(metadata["provider_response_complete"])
            environment = json.loads((base / "environment.json").read_text())
            self.assertTrue(environment["config_isolated"])
            self.assertTrue(environment["gemini_home_outside_workdir"])
            self.assertEqual(environment["tool_policy"], "read-only allowlist")
            self.assertNotIn("Task prompt:", environment["command"])
            self.assertTrue((base / "trace.jsonl").exists())

    def test_protocol_failure_with_zero_exit_uses_gemini_failure_marker(self):
        result = sb.gemini_cli_invoke(
            "prompt", gemini_cmd=f"{sys.executable} -c 'print(\"not-json\")'",
            timeout=30, output_format="stream-json")
        outcome = sb.GeminiBackend().invoke_answer(
            sb.InvocationRequest("prompt", Path(tempfile.gettempdir()), None, 30),
            gemini_cmd=f"{sys.executable} -c 'print(\"not-json\")'",
        )
        self.assertEqual(result["returncode"], 0)
        self.assertIsNotNone(result["protocol_error"])
        self.assertIsInstance(outcome, rc.ProviderFailed)
        with tempfile.TemporaryDirectory() as td:
            base = Path(td) / "run"
            sb.write_runner_outcome(base, outcome)
            self.assertTrue((base / "output.md").read_text().startswith(
                "[GEMINI FAILURE"))
            self.assertFalse(json.loads(
                (base / "metadata.json").read_text())["provider_response_complete"])

    def test_trace_dialect_preserves_completed_read_lifecycle(self):
        fixture = (Path(__file__).parent / "fixtures" / "gemini"
                   / "tool-answer.stream.jsonl").read_text(encoding="utf-8")
        records, errors = sb.parse_trace_jsonl_text(fixture)
        self.assertEqual(errors, [])

        events, metrics = sb.normalize_trace_records(records, source="gemini")

        reads = [event for event in events["events"]
                 if event["type"] == "file_read"]
        self.assertEqual(len(reads), 2)
        self.assertEqual([event["status"] for event in reads],
                         ["in_progress", "completed"])
        self.assertEqual(reads[1]["raw_ref"]["line"], 4)
        self.assertEqual(reads[1]["raw_result_ref"]["line"], 5)
        self.assertEqual(metrics["file_reads"], 1)
        self.assertNotIn("trace_protocol_errors", metrics)

    def test_workspace_provider_controls_fail_before_invocation(self):
        with tempfile.TemporaryDirectory() as td:
            workspace = Path(td)
            (workspace / "GEMINI.md").write_text("ambient", encoding="utf-8")

            result = sb.gemini_cli_invoke(
                "prompt", cwd=workspace,
                gemini_cmd="this-command-must-not-run", timeout=30)

        self.assertEqual(result["returncode"], 1)
        self.assertIn("provider control files", result["protocol_error"])
        self.assertEqual(result["stdout"], "")


class GeminiJudgeBackendTests(unittest.TestCase):
    def _task(self, root: Path) -> dict[str, object]:
        output = root / "output.md"
        output.write_text("answer under review", encoding="utf-8")
        return {
            "judge_task_id": "gemini-judge-task",
            "case_id": "case-1",
            "variant": "with_skill",
            "run_number": 1,
            "output_path": str(output),
            "assertion": {
                "type": "llm_judge",
                "rubric": "Pass if the answer is grounded.",
            },
        }

    def test_native_judge_uses_json_no_tools_and_preserves_raw_provider_artifacts(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            fake = root / "fake_gemini_judge.py"
            fake.write_text(
                "import pathlib, sys\n"
                "assert sys.argv[sys.argv.index('--output-format') + 1] == 'json'\n"
                "assert sys.argv[sys.argv.index('--model') + 1] == 'gemini-judge'\n"
                "assert '--skip-trust' in sys.argv and '--sandbox' in sys.argv\n"
                "policy = pathlib.Path(sys.argv[sys.argv.index('--policy') + 1]).read_text()\n"
                "assert 'toolName = \"*\"' in policy and 'decision = \"deny\"' in policy\n"
                f"sys.stdout.write({_judge_envelope()!r})\n",
                encoding="utf-8")
            transcripts = root / "transcripts"

            row = sb.run_one_judge_task(
                self._task(root), judge_backend="gemini",
                judge_model="gemini-judge",
                backend_options={"gemini_cmd": f"{sys.executable} {fake}"},
                transcripts_dir=transcripts,
            )

            self.assertTrue(row["passed"])
            self.assertEqual(row["judge_backend"], "gemini")
            self.assertEqual(row["judge_model"], "gemini-judge")
            self.assertEqual(row["usage_normalized"]["total_tokens"], 8)
            destination = transcripts / "gemini-judge-task" / "run-1"
            self.assertEqual(
                (destination / "provider-response.json").read_text(),
                _judge_envelope(),
            )
            metadata = json.loads(
                (destination / "provider-metadata.json").read_text())
            self.assertEqual(metadata["session_id"], "session-judge")
            self.assertEqual(metadata["resolved_model"], "gemini-judge")

    def test_native_judge_returns_the_typed_invocation_contract(self):
        provider_result = {
            "answer": '{"passed":true}',
            "stderr": "",
            "returncode": 0,
            "usage": {"input_tokens": 2, "output_tokens": 1,
                      "total_tokens": 3},
            "model": "gemini-judge",
            "raw_response": _judge_envelope(),
            "metadata": {"session_id": "session-judge"},
        }
        with mock.patch.object(
                sb, "gemini_cli_invoke", return_value=provider_result):
            invocation = sb.gemini_judge_invoke(
                "prompt", judge_model="gemini-judge", gemini_cmd="gemini",
                explore_hint=None)
        self.assertIsInstance(invocation, jc.JudgeInvocation)
        self.assertEqual(invocation.raw_response, _judge_envelope())
        self.assertEqual(invocation.metadata["session_id"], "session-judge")


if __name__ == "__main__":
    unittest.main()
