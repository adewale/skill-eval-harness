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


def _write_executable(path: Path, source: str) -> None:
    path.write_text(f"#!{sys.executable}\n{source}", encoding="utf-8")
    path.chmod(path.stat().st_mode | 0o111)


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


def _judge_stream(*, tool_calls: int = 0) -> str:
    records = [
        _event("init", session_id="session-judge", model="gemini-judge"),
        _event("message", role="assistant", content=json.dumps({
            "passed": True,
            "score": 1,
            "rationale": "Gemini judge ok",
        }), delta=True),
        _event("result", status="success", stats={
            "total_tokens": 8,
            "input_tokens": 6,
            "output_tokens": 2,
            "cached": 1,
            "input": 5,
            "duration_ms": 20,
            "tool_calls": tool_calls,
            "models": {
                "gemini-judge": {
                    "total_tokens": 8,
                    "input_tokens": 6,
                    "output_tokens": 2,
                    "cached": 1,
                    "input": 5,
                },
            },
        }),
    ]
    return "\n".join(json.dumps(record) for record in records) + "\n"


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
            self.assertEqual(metadata["gemini_auth_files_copied"], [
                "oauth_creds.json"])
            self.assertEqual(json.loads((target / "settings.json").read_text()), {
                "advanced": {"ignoreLocalEnv": True},
                "privacy": {"usageStatisticsEnabled": False},
                "security": {"auth": {"selectedType": "oauth-personal"}},
            })
            self.assertTrue(metadata["usage_statistics_disabled_requested"])
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
            settings = json.loads(
                (root / "isolated" / ".gemini" / "settings.json").read_text())
            self.assertEqual(settings, {
                "advanced": {"ignoreLocalEnv": True},
                "privacy": {"usageStatisticsEnabled": False},
                "security": {"auth": {"selectedType": "gemini-api-key"}},
            })
            self.assertEqual(metadata["gemini_auth_type_source"], "environment")

    def test_configured_auth_type_precedes_environment_selectors(self):
        cases = (
            ({"GEMINI_API_KEY": "key"}, "oauth-personal", ["oauth_creds.json"]),
            ({"GOOGLE_GENAI_USE_VERTEXAI": "true", "GOOGLE_API_KEY": "key"},
             "oauth-personal", ["oauth_creds.json"]),
            ({"GOOGLE_GENAI_USE_GCA": "true", "GOOGLE_CLOUD_ACCESS_TOKEN": "token"},
             "oauth-personal", []),
            ({"GEMINI_CLI_USE_COMPUTE_ADC": "true",
              "GOOGLE_APPLICATION_CREDENTIALS": "/credentials.json"},
             "oauth-personal", ["oauth_creds.json"]),
            ({"GOOGLE_GEMINI_BASE_URL": "https://gateway.invalid"},
             "oauth-personal", ["oauth_creds.json"]),
        )
        for environment, expected, copied in cases:
            with self.subTest(expected=expected), tempfile.TemporaryDirectory() as td:
                root = Path(td)
                source = root / "source" / ".gemini"
                source.mkdir(parents=True)
                (source / "settings.json").write_text(
                    '{\n  // configured auth outranks environment selectors\n'
                    '  "security": {"auth": {"selectedType": "oauth-personal"}}\n}\n',
                    encoding="utf-8")
                (source / "oauth_creds.json").write_text("{}", encoding="utf-8")
                with mock.patch.dict(os.environ, {
                        "GEMINI_CLI_HOME": str(root / "source"),
                        **environment,
                }, clear=True):
                    metadata = sb.seed_gemini_home(root / "isolated")
                settings = json.loads(
                    (root / "isolated" / ".gemini" / "settings.json").read_text())
                self.assertEqual(
                    settings["security"]["auth"]["selectedType"], expected)
                self.assertEqual(metadata["gemini_auth_files_copied"], copied)
                self.assertEqual(metadata["gemini_auth_type_source"], "settings")

    def test_environment_selector_precedence_when_settings_are_absent(self):
        cases = (
            ({"GEMINI_API_KEY": "key"}, "gemini-api-key"),
            ({"GOOGLE_GENAI_USE_VERTEXAI": "true", "GOOGLE_API_KEY": "key"},
             "vertex-ai"),
            ({"GOOGLE_GENAI_USE_GCA": "true", "GOOGLE_CLOUD_ACCESS_TOKEN": "token"},
             "oauth-personal"),
            ({"GEMINI_CLI_USE_COMPUTE_ADC": "true"},
             "compute-default-credentials"),
            ({"GOOGLE_GEMINI_BASE_URL": "https://gateway.invalid"}, "gateway"),
        )
        for environment, expected in cases:
            with self.subTest(expected=expected), tempfile.TemporaryDirectory() as td:
                root = Path(td)
                (root / "source" / ".gemini").mkdir(parents=True)
                with mock.patch.dict(os.environ, {
                        "GEMINI_CLI_HOME": str(root / "source"),
                        **environment,
                }, clear=True):
                    metadata = sb.seed_gemini_home(root / "isolated")
                settings = json.loads(
                    (root / "isolated" / ".gemini" / "settings.json").read_text())
                self.assertEqual(
                    settings["security"]["auth"]["selectedType"], expected)
                self.assertEqual(metadata["gemini_auth_type_source"], "environment")
                if expected == "gateway":
                    self.assertTrue(settings["security"]["auth"]["useExternal"])

    def test_credential_support_env_does_not_override_jsonc_oauth_selection(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source = root / "source" / ".gemini"
            source.mkdir(parents=True)
            (source / "settings.json").write_text(
                '{\n  /* Gemini accepts JSONC settings. */\n'
                '  "security": {"auth": {"selectedType": "oauth-personal"}}\n}\n',
                encoding="utf-8")
            (source / "oauth_creds.json").write_text("{}", encoding="utf-8")
            with mock.patch.dict(os.environ, {
                    "GEMINI_CLI_HOME": str(root / "source"),
                    "GOOGLE_API_KEY": "vertex-support-only",
                    "GOOGLE_APPLICATION_CREDENTIALS": "/adc.json",
            }, clear=True):
                metadata = sb.seed_gemini_home(root / "isolated")
            settings = json.loads(
                (root / "isolated" / ".gemini" / "settings.json").read_text())
            self.assertEqual(
                settings["security"]["auth"]["selectedType"], "oauth-personal")
            self.assertEqual(metadata["gemini_auth_files_copied"],
                             ["oauth_creds.json"])
            self.assertEqual(metadata["gemini_auth_type_source"], "settings")

    def test_current_encrypted_file_keychain_oauth_is_seeded_and_selectors_preserved(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source = root / "source" / ".gemini"
            source.mkdir(parents=True)
            (source / "settings.json").write_text(json.dumps({
                "security": {"auth": {"selectedType": "oauth-personal"}},
            }), encoding="utf-8")
            (source / "gemini-credentials.json").write_text(
                '{"gemini-cli-oauth":"encrypted"}', encoding="utf-8")
            with mock.patch.dict(os.environ, {
                    "GEMINI_CLI_HOME": str(root / "source"),
                    "GEMINI_FORCE_ENCRYPTED_FILE_STORAGE": "true",
                    "GEMINI_FORCE_FILE_STORAGE": "true",
            }, clear=True):
                env, metadata = sb.gemini_env_for_home(root / "isolated")

            self.assertEqual(metadata["gemini_auth_files_copied"],
                             ["gemini-credentials.json"])
            self.assertEqual(env["GEMINI_FORCE_FILE_STORAGE"], "true")
            self.assertEqual(
                env["GEMINI_FORCE_ENCRYPTED_FILE_STORAGE"], "true")
            self.assertNotIn(
                "GEMINI_FORCE_FILE_STORAGE",
                metadata["gemini_control_env_removed"])
            self.assertNotIn(
                "GEMINI_FORCE_ENCRYPTED_FILE_STORAGE",
                metadata["gemini_control_env_removed"])

    def test_encrypted_oauth_is_forced_off_the_host_keychain(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source = root / "source" / ".gemini"
            source.mkdir(parents=True)
            (source / "settings.json").write_text(json.dumps({
                "security": {"auth": {"selectedType": "oauth-personal"}},
            }), encoding="utf-8")
            (source / "gemini-credentials.json").write_text(
                '{"gemini-cli-oauth":"encrypted"}', encoding="utf-8")
            with mock.patch.dict(os.environ, {
                    "GEMINI_CLI_HOME": str(root / "source"),
                    "GEMINI_FORCE_ENCRYPTED_FILE_STORAGE": "true",
            }, clear=True):
                env, metadata = sb.gemini_env_for_home(root / "isolated")

        self.assertEqual(env["GEMINI_FORCE_FILE_STORAGE"], "true")
        self.assertTrue(metadata["gemini_file_storage_forced"])
        self.assertEqual(metadata["gemini_auth_files_copied"], [
            "gemini-credentials.json"])

    def test_native_keychain_only_oauth_fails_before_provider_spawn(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source = root / "source" / ".gemini"
            source.mkdir(parents=True)
            (source / "settings.json").write_text(json.dumps({
                "security": {"auth": {"selectedType": "oauth-personal"}},
            }), encoding="utf-8")
            with mock.patch.dict(os.environ, {
                    "GEMINI_CLI_HOME": str(root / "source"),
                    "GEMINI_FORCE_ENCRYPTED_FILE_STORAGE": "true",
            }, clear=True):
                result = sb.gemini_cli_invoke(
                    "prompt", cwd=root / "workspace",
                    gemini_cmd="this-command-must-not-run", timeout=30)

        self.assertEqual(result["returncode"], 127)
        self.assertIn("portable OAuth credential", result["protocol_error"])
        self.assertEqual(result["stdout"], "")
        self.assertFalse(
            result["environment"]["gemini_file_storage_forced"])

    def test_encrypted_oauth_portability_uses_selected_transport(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source = root / "source" / ".gemini"
            source.mkdir(parents=True)
            (source / "settings.json").write_text(json.dumps({
                "security": {"auth": {"selectedType": "oauth-personal"}},
            }), encoding="utf-8")

            with self.subTest("unselected access token"), mock.patch.dict(
                    os.environ, {
                        "GEMINI_CLI_HOME": str(root / "source"),
                        "GEMINI_FORCE_ENCRYPTED_FILE_STORAGE": "true",
                        "GOOGLE_CLOUD_ACCESS_TOKEN": "token",
                    }, clear=True):
                env, metadata = sb.gemini_env_for_home(root / "isolated-token")
                self.assertIn("gemini_auth_preflight_error", metadata)
                self.assertNotIn("GEMINI_FORCE_FILE_STORAGE", env)

            adc = root / "adc.json"
            adc.write_text('{"type":"authorized_user"}', encoding="utf-8")
            with self.subTest("explicit ADC"), mock.patch.dict(os.environ, {
                    "GEMINI_CLI_HOME": str(root / "source"),
                    "GEMINI_FORCE_ENCRYPTED_FILE_STORAGE": "true",
                    "GOOGLE_APPLICATION_CREDENTIALS": str(adc),
            }, clear=True):
                env, metadata = sb.gemini_env_for_home(root / "isolated-adc")
                self.assertNotIn("gemini_auth_preflight_error", metadata)
                self.assertTrue(metadata["google_application_credentials_copied"])
                self.assertEqual(env["GEMINI_FORCE_FILE_STORAGE"], "true")

            with self.subTest("selected GCA access token"), mock.patch.dict(
                    os.environ, {
                        "GEMINI_CLI_HOME": str(root / "source"),
                        "GEMINI_FORCE_ENCRYPTED_FILE_STORAGE": "true",
                        "GOOGLE_GENAI_USE_GCA": "true",
                        "GOOGLE_CLOUD_ACCESS_TOKEN": "token",
                    }, clear=True):
                env, metadata = sb.gemini_env_for_home(root / "isolated-gca")
                self.assertNotIn("gemini_auth_preflight_error", metadata)
                self.assertEqual(env["GEMINI_FORCE_FILE_STORAGE"], "true")

    def test_configured_vertex_is_not_overridden_by_gca_selector(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source = root / "source" / ".gemini"
            source.mkdir(parents=True)
            (source / "settings.json").write_text(json.dumps({
                "security": {"auth": {"selectedType": "vertex-ai"}},
            }), encoding="utf-8")
            (source / "gemini-credentials.json").write_text(
                '{"gemini-cli-oauth":"encrypted"}', encoding="utf-8")
            with mock.patch.dict(os.environ, {
                    "GEMINI_CLI_HOME": str(root / "source"),
                    "GOOGLE_GENAI_USE_GCA": "true",
                    "GEMINI_FORCE_ENCRYPTED_FILE_STORAGE": "true",
                    "GEMINI_FORCE_FILE_STORAGE": "true",
            }, clear=True):
                env, metadata = sb.gemini_env_for_home(root / "isolated")

            self.assertEqual(metadata["gemini_auth_type"], "vertex-ai")
            self.assertEqual(metadata["gemini_auth_files_copied"], [])
            self.assertNotIn("GOOGLE_GENAI_USE_GCA", env)
            self.assertNotIn("GOOGLE_CLOUD_ACCESS_TOKEN", env)

    def test_auth_source_and_environment_follow_one_closed_plan(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source = root / "source" / ".gemini"
            source.mkdir(parents=True)
            (source / "settings.json").write_text("{}", encoding="utf-8")
            with mock.patch.dict(os.environ, {
                    "GEMINI_CLI_HOME": str(source.parent),
                    "GEMINI_API_KEY": "key",
            }, clear=True):
                env, metadata = sb.gemini_env_for_home(root / "isolated")
            self.assertEqual(metadata["gemini_auth_type_source"], "environment")
            self.assertFalse(metadata["gemini_auth_type_copied"])
            self.assertIn("GEMINI_API_KEY", env)

            (source / "settings.json").write_text(json.dumps({
                "security": {"auth": {"selectedType": "gemini-api-key"}},
            }), encoding="utf-8")
            with mock.patch.dict(os.environ, {
                    "GEMINI_CLI_HOME": str(source.parent),
                    "GEMINI_API_KEY": "key",
                    "GOOGLE_GEMINI_BASE_URL": "https://ambient.invalid",
            }, clear=True):
                env, _ = sb.gemini_env_for_home(root / "api-key")
            self.assertNotIn("GOOGLE_GEMINI_BASE_URL", env)

            (source / "settings.json").write_text(json.dumps({
                "security": {"auth": {"selectedType": "gateway"}},
            }), encoding="utf-8")
            with mock.patch.dict(os.environ, {
                    "GEMINI_CLI_HOME": str(source.parent),
                    "GEMINI_API_KEY": "key",
                    "GOOGLE_GEMINI_BASE_URL": "https://gateway.invalid",
            }, clear=True):
                env, metadata = sb.gemini_env_for_home(root / "gateway")
            self.assertEqual(metadata["gemini_auth_type_source"], "settings")
            self.assertTrue(metadata["gemini_auth_type_copied"])
            self.assertIn("GEMINI_API_KEY", env)
            self.assertIn("GOOGLE_GEMINI_BASE_URL", env)

    def test_portable_api_key_file_is_forced_off_native_keychain(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source = root / "source" / ".gemini"
            source.mkdir(parents=True)
            (source / "settings.json").write_text(json.dumps({
                "security": {"auth": {"selectedType": "gemini-api-key"}},
            }), encoding="utf-8")
            (source / "gemini-credentials.json").write_text(
                '{"gemini-api-key":"encrypted"}', encoding="utf-8")
            with mock.patch.dict(os.environ, {
                    "GEMINI_CLI_HOME": str(source.parent),
            }, clear=True):
                env, metadata = sb.gemini_env_for_home(root / "isolated")
            self.assertEqual(env["GEMINI_FORCE_FILE_STORAGE"], "true")
            self.assertTrue(metadata["gemini_file_storage_forced"])
            self.assertNotIn("gemini_auth_preflight_error", metadata)

    def test_legacy_cloud_shell_auth_is_normalized_to_effective_compute_auth(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source = root / "source" / ".gemini"
            source.mkdir(parents=True)
            (source / "settings.json").write_text(json.dumps({
                "security": {"auth": {"selectedType": "cloud-shell"}},
            }), encoding="utf-8")
            with mock.patch.dict(os.environ, {
                    "GEMINI_CLI_HOME": str(source.parent),
                    "CLOUD_SHELL": "true",
            }, clear=True):
                metadata = sb.seed_gemini_home(root / "isolated")
            self.assertEqual(metadata["gemini_configured_auth_type"], "cloud-shell")
            self.assertEqual(
                metadata["gemini_auth_type"], "compute-default-credentials")

    def test_malformed_source_settings_fail_before_spawn(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source = root / "source" / ".gemini"
            source.mkdir(parents=True)
            (source / "settings.json").write_text("[" * 2000, encoding="utf-8")
            with mock.patch.dict(os.environ, {
                    "GEMINI_CLI_HOME": str(source.parent),
                    "GEMINI_API_KEY": "key",
            }, clear=True), mock.patch.object(
                    sb, "run_argv_capture") as spawn:
                result = sb.gemini_cli_invoke(
                    "prompt", cwd=root / "workspace", gemini_cmd="gemini")
            spawn.assert_not_called()
            self.assertEqual(
                result["invocation_state"], sb.InvocationState.SPAWN_FAILED.value)
            self.assertIn("settings could not be validated", result["stderr"])

    def test_invalid_selected_type_and_missing_oauth_fail_without_login(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source = root / "source" / ".gemini"
            source.mkdir(parents=True)
            for selected, expected in (
                    (123, "settings could not be validated"),
                    ("oauth-personal", "interactive browser/device")):
                with self.subTest(selected=selected):
                    (source / "settings.json").write_text(json.dumps({
                        "security": {"auth": {"selectedType": selected}},
                    }), encoding="utf-8")
                    with mock.patch.dict(os.environ, {
                            "GEMINI_CLI_HOME": str(source.parent),
                            "GEMINI_API_KEY": "unrelated-key",
                    }, clear=True), mock.patch.object(
                            sb, "run_argv_capture") as spawn:
                        result = sb.gemini_cli_invoke(
                            "prompt", cwd=root / f"workspace-{selected}",
                            gemini_cmd="gemini")
                    spawn.assert_not_called()
                    self.assertIn(expected, result["stderr"])
                    self.assertTrue(
                        result["environment"]["browser_auth_suppressed"])

    def test_setup_copy_failure_becomes_closed_preflight_failure(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source = root / "source" / ".gemini"
            source.mkdir(parents=True)
            (source / "settings.json").write_text(json.dumps({
                "security": {"auth": {"selectedType": "oauth-personal"}},
            }), encoding="utf-8")
            (source / "oauth_creds.json").write_text("{}", encoding="utf-8")
            with mock.patch.dict(os.environ, {
                    "GEMINI_CLI_HOME": str(source.parent),
            }, clear=True), mock.patch.object(
                    sb.shutil, "copy2", side_effect=OSError("secret path")):
                result = sb.gemini_cli_invoke(
                    "prompt", cwd=root / "workspace", gemini_cmd="gemini")
            self.assertEqual(
                result["invocation_state"], sb.InvocationState.SPAWN_FAILED.value)
            self.assertIn("OSError", result["stderr"])
            self.assertNotIn("secret path", result["stderr"])

    def test_initial_temp_creation_failure_has_the_closed_result_shape(self):
        with mock.patch.object(
                sb.tempfile, "mkdtemp", side_effect=OSError("secret temp path")), \
                mock.patch.object(sb, "run_argv_capture") as spawn:
            result = sb.gemini_cli_invoke("prompt", gemini_cmd="gemini")

        spawn.assert_not_called()
        self.assertEqual(result["returncode"], 127)
        self.assertEqual(
            result["invocation_state"], sb.InvocationState.SPAWN_FAILED.value)
        self.assertEqual(
            result["environment"]["temporary_home_cleanup"]["status"],
            "not_created")
        self.assertNotIn("secret temp path", json.dumps(result))

    def test_settings_read_failure_does_not_persist_exception_text(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source = root / "source" / ".gemini"
            source.mkdir(parents=True)
            (source / "settings.json").write_text("{}", encoding="utf-8")
            original_read_text = Path.read_text

            def fail_settings(path: Path, *args: object, **kwargs: object) -> str:
                if path == source / "settings.json":
                    raise OSError("secret settings sentinel")
                return original_read_text(path, *args, **kwargs)

            with mock.patch.dict(os.environ, {
                    "GEMINI_CLI_HOME": str(source.parent),
                    "GEMINI_API_KEY": "key",
            }, clear=True), mock.patch.object(
                    Path, "read_text", autospec=True,
                    side_effect=fail_settings), mock.patch.object(
                    sb, "run_argv_capture") as spawn:
                result = sb.gemini_cli_invoke(
                    "prompt", cwd=root / "workspace", gemini_cmd="gemini")

        spawn.assert_not_called()
        self.assertEqual(result["returncode"], 127)
        self.assertNotIn("secret settings sentinel", json.dumps(result))

    def test_unexpandable_adc_path_becomes_closed_preflight_failure(self):
        with tempfile.TemporaryDirectory() as td, mock.patch.dict(os.environ, {
                "GOOGLE_GENAI_USE_VERTEXAI": "true",
                "GOOGLE_APPLICATION_CREDENTIALS": (
                    "~definitely-no-such-user-xyz/adc.json"),
        }, clear=True), mock.patch.object(sb, "run_argv_capture") as spawn:
            result = sb.gemini_cli_invoke(
                "prompt", cwd=Path(td) / "workspace", gemini_cmd="gemini")

        spawn.assert_not_called()
        self.assertEqual(
            result["invocation_state"], sb.InvocationState.SPAWN_FAILED.value)
        self.assertIn("RuntimeError", result["stderr"])
        self.assertNotIn("definitely-no-such-user", json.dumps(result))

    def test_direct_invocation_request_contract_fails_before_any_probe(self):
        cases = (
            ("timeout-string", {"timeout": "1"}),
            ("timeout-bool", {"timeout": True}),
            ("timeout-zero", {"timeout": 0}),
            ("timeout-negative", {"timeout": -1}),
            ("read-tools-string", {"allow_read_tools": "false"}),
            ("prompt-nul", {"prompt": "bad\x00prompt"}),
            ("model-nul", {"model": "bad\x00model"}),
            ("executable-nul", {"gemini_cmd": "bad\x00gemini"}),
            ("active-at-path", {"prompt": "Summarize @secret.txt"}),
            ("email-is-provider-active", {
                "prompt": "Contact user@example.com"}),
            ("slash-command", {"prompt": "/memory show"}),
        )
        with tempfile.TemporaryDirectory() as td:
            for name, overrides in cases:
                arguments = {
                    "prompt": "prompt", "model": "gemini-test",
                    "gemini_cmd": "gemini", "timeout": 30,
                    "allow_read_tools": True, "cwd": Path(td) / name,
                    **overrides,
                }
                with self.subTest(name=name), mock.patch.object(
                        sb, "run_argv_capture") as spawn, mock.patch.object(
                        sb, "probe_gemini_cli_version") as probe:
                    result = sb.gemini_cli_invoke(**arguments)
                spawn.assert_not_called()
                probe.assert_not_called()
                self.assertEqual(result["returncode"], 127)
                self.assertEqual(
                    result["invocation_state"],
                    sb.InvocationState.SPAWN_FAILED.value)

    def test_invalid_model_text_never_reenters_durable_metadata(self):
        with tempfile.TemporaryDirectory() as td, mock.patch.object(
                sb, "run_argv_capture") as spawn:
            result = sb.gemini_cli_invoke(
                "prompt", model="gemini-\ud800", cwd=Path(td) / "workspace",
                gemini_cmd="gemini")

        spawn.assert_not_called()
        self.assertIsNone(result["metadata"]["requested_model"])
        json.dumps(result, ensure_ascii=False).encode("utf-8")

    def test_redaction_ignores_flag_like_executable_token(self):
        argv = ["--prompt=executable", "--prompt=secret", "--policy", "/private/policy"]
        self.assertEqual(sb.redact_gemini_argv(argv), [
            "--prompt=executable", "--prompt=<prompt>", "--policy",
            "<isolated policy outside workdir>",
        ])

    def test_child_environment_removes_provider_controls_but_keeps_auth(self):
        hostile = {
            "GEMINI_API_KEY": "secret",
            "GEMINI_MODEL": "ambient-model",
            "GEMINI_SYSTEM_MD": "/ambient/system.md",
            "GEMINI_CLI_SYSTEM_SETTINGS_PATH": "/ambient/settings.json",
            "GEMINI_CLI_SYSTEM_DEFAULTS_PATH": "/ambient/defaults.json",
            "GEMINI_CLI_ACTIVITY_LOG_TARGET": "/ambient/activity.jsonl",
            "GEMINI_CLI_IDE_SERVER_STDIO_COMMAND": "ambient-ide",
            "_GEMINI_USER_GCP_PROJECT": "ambient-billing-project",
            "SANDBOX": "already-sandboxed",
            "SANDBOX_FLAGS": "--privileged",
            "SANDBOX_MOUNTS": "/:/host",
            "SANDBOX_ENV": "SECRET=leak",
            "GEMINI_SANDBOX_PROXY_COMMAND": "ambient-proxy",
            "SEATBELT_PROFILE": "ambient-profile",
            "BUILD_SANDBOX": "1",
            "OTEL_EXPORTER_OTLP_ENDPOINT": "https://collector.invalid",
            "NODE_OPTIONS": "--require=/ambient/inject.js",
            "DEBUG": "true",
            "DEBUG_MODE": "1",
            "DEBUG_PORT": "9229",
            "GOOGLE_VERTEX_BASE_URL": "https://ambient.invalid",
            "GOOGLE_GENAI_API_VERSION": "ambient-version",
            "OAUTH_CALLBACK_HOST": "0.0.0.0",
            "OAUTH_CALLBACK_PORT": "8888",
            "BROWSER": "/ambient/browser",
            "NO_BROWSER": "false",
        }
        with tempfile.TemporaryDirectory() as td, mock.patch.dict(
                os.environ, hostile, clear=True):
            env, metadata = sb.gemini_env_for_home(Path(td) / "isolated")

        self.assertEqual(env["GEMINI_API_KEY"], "secret")
        self.assertEqual(env["NO_BROWSER"], "true")
        self.assertNotIn("GEMINI_CLI_TRUST_WORKSPACE", env)
        removed = set(hostile) - {"GEMINI_API_KEY", "NO_BROWSER"}
        self.assertTrue(removed.isdisjoint(env))
        self.assertEqual(
            set(metadata["gemini_control_env_removed"]), removed | {"NO_BROWSER"})
        self.assertTrue(metadata["local_env_ignored"])
        self.assertTrue(metadata["system_settings_may_be_inherited"])
        self.assertEqual(set(metadata["custom_system_settings_paths_removed"]), {
            "GEMINI_CLI_SYSTEM_SETTINGS_PATH",
            "GEMINI_CLI_SYSTEM_DEFAULTS_PATH",
        })
        self.assertTrue(
            metadata["workspace_trust_deferred_until_after_env_load"])

    def test_ambient_early_trust_cannot_enable_ancestor_gemini_env(self):
        with tempfile.TemporaryDirectory() as td, mock.patch.dict(os.environ, {
                "GEMINI_CLI_TRUST_WORKSPACE": "true",
                "GEMINI_API_KEY": "secret",
        }, clear=True):
            env, metadata = sb.gemini_env_for_home(Path(td) / "isolated")

        self.assertNotIn("GEMINI_CLI_TRUST_WORKSPACE", env)
        self.assertIn(
            "GEMINI_CLI_TRUST_WORKSPACE",
            metadata["gemini_control_env_removed"])
        self.assertTrue(metadata["local_env_ignored"])
        self.assertTrue(
            metadata["workspace_trust_deferred_until_after_env_load"])

    def test_command_value_is_one_literal_executable_token(self):
        hostile = (
            "--policy=/tmp/allow.toml", "--admin-policy", "--yolo=true",
            "--approval-mode=yolo", "--allowed-tools=run_shell_command",
            "--allowed-mcp-server-names=ambient", "--extensions=ambient",
            "--include-directories=/", "--resume=latest", "--session-file=x",
            "--worktree=ambient", "--prompt-interactive=ambient", "--acp",
            "--fake-responses=/tmp/fake.json", "--raw-output", "--",
            "--isCommand=true", "--debug", "mcp", "extensions", "extension",
            "skills", "skill", "hooks", "hook",
            "-o=json", "-ostream-json", "-s=false", "-mambient", "-pambient",
            "-r5", "-wambient", "-eambient", "-iambient", "-y", "-d",
        )
        for argument in hostile:
            with self.subTest(argument=argument):
                command = f"wrapper {argument}"
                argv = sb.build_gemini_cli_argv(
                    command, prompt="prompt",
                    output_format="stream-json", policy_path=Path("policy.toml"),
                    model="gemini-test")
                self.assertEqual(argv[0], command)
                self.assertEqual(argv.count("--policy"), 1)
                self.assertEqual(sum(
                    value.startswith("--prompt=") for value in argv), 1)

        argv = sb.build_gemini_cli_argv(
            "fake-gemini", prompt="prompt",
            output_format="stream-json", policy_path=Path("policy.toml"),
            model="gemini-test")
        self.assertEqual(argv[0], "fake-gemini")

    def test_executable_path_with_spaces_is_not_split(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            fake = root / "Program Files" / "fake gemini.py"
            fake.parent.mkdir()
            _write_executable(
                fake,
                "import sys\n"
                "if '--version' in sys.argv:\n"
                "    print('0.55.0-space-test')\n"
                "else:\n"
                f"    sys.stdout.write({_success_stream()!r})\n")

            result = sb.gemini_cli_invoke(
                "prompt", cwd=root / "workspace", gemini_cmd=str(fake),
                timeout=30)

        self.assertEqual(result["returncode"], 0)
        self.assertEqual(result["answer"], "answer from Gemini")

    def test_dash_prefixed_prompt_and_model_are_bound_as_values_and_redacted(self):
        for prompt in ("-x", "--", "--sandbox=false"):
            with self.subTest(prompt=prompt):
                argv = sb.build_gemini_cli_argv(
                    "gemini", prompt=prompt, output_format="stream-json",
                    policy_path=Path("policy.toml"), model="--model-like-value")
                self.assertIn(f"--prompt={prompt}", argv)
                self.assertIn("--model=--model-like-value", argv)
                redacted = sb.redact_gemini_argv(argv)
                self.assertNotIn(f"--prompt={prompt}", redacted)
                self.assertIn("--prompt=<prompt>", redacted)

    def test_version_probe_without_owned_prompt_is_total(self):
        with mock.patch.object(sb, "invoke_argv_with_timeout") as invoke:
            metadata = sb.probe_gemini_cli_version(
                ["gemini", "--version"], cwd=Path.cwd(), env={}, timeout=30)
        invoke.assert_not_called()
        self.assertEqual(metadata["gemini_cli_version_status"], "unavailable")

    def test_prompt_builder_rejects_only_provider_active_at_syntax(self):
        for prompt in ("Summarize @secret.txt", "Contact user@example.com"):
            with self.subTest(prompt=prompt), self.assertRaisesRegex(
                    ValueError, "active @path"):
                sb.build_gemini_cli_argv(
                    "gemini", prompt=prompt, output_format="stream-json",
                    policy_path=Path("policy.toml"), model=None)
        with self.assertRaisesRegex(ValueError, "slash-command"):
            sb.build_gemini_cli_argv(
                "gemini", prompt="/memory show", output_format="stream-json",
                policy_path=Path("policy.toml"), model=None)
        for prompt in (
                "A bare @ is text", "escaped \\@file is not expanded",
                "// code comment", "/* block comment */"):
            with self.subTest(prompt=prompt):
                argv = sb.build_gemini_cli_argv(
                    "gemini", prompt=prompt, output_format="stream-json",
                    policy_path=Path("policy.toml"), model=None)
                self.assertIn(f"--prompt={prompt}", argv)

    def test_sandbox_is_omitted_when_auth_cannot_cross_a_container_boundary(self):
        cases = (
            ({"GEMINI_API_KEY": "secret"}, {"gemini_auth_type": "gemini-api-key"}, True),
            ({}, {"gemini_auth_type": "gemini-api-key"}, False),
            ({"GOOGLE_CLOUD_ACCESS_TOKEN": "token"},
             {"gemini_auth_type": "oauth-personal"}, False),
            ({}, {"gemini_auth_type": "oauth-personal"}, False),
            ({"GOOGLE_API_KEY": "vertex-only"},
             {"gemini_auth_type": "compute-default-credentials"}, False),
        )
        for environment, auth, expected in cases:
            with self.subTest(auth=auth, environment=environment), \
                    mock.patch.object(sb.sys, "platform", "linux"), \
                    mock.patch.object(
                        sb.shutil, "which",
                        side_effect=lambda name: (
                            "/usr/bin/docker" if name == "docker" else None)):
                plan = sb.gemini_sandbox_plan(environment, auth)
                self.assertIs(plan["requested"], expected)
                self.assertTrue(plan["credential_transport"])

    def test_relative_adc_path_is_not_claimed_portable_to_container_sandbox(self):
        with tempfile.TemporaryDirectory() as td:
            previous = Path.cwd()
            os.chdir(td)
            try:
                Path("adc.json").write_text("{}", encoding="utf-8")
                with mock.patch.object(sb.sys, "platform", "linux"), \
                        mock.patch.object(
                            sb.shutil, "which",
                            side_effect=lambda name: (
                                "/usr/bin/docker" if name == "docker" else None)):
                    plan = sb.gemini_sandbox_plan(
                        {"GOOGLE_APPLICATION_CREDENTIALS": "adc.json"},
                        {"gemini_auth_type": "vertex-ai"})
            finally:
                os.chdir(previous)
        self.assertFalse(plan["requested"])

    def test_container_sandbox_transport_is_auth_specific(self):
        with tempfile.TemporaryDirectory() as td:
            adc = Path(td) / "adc.json"
            adc.write_text("{}", encoding="utf-8")
            cases = (
                ({}, {"gemini_auth_type": "oauth-personal",
                      "gemini_auth_files_copied": ["oauth_creds.json"]}, True),
                ({"GEMINI_FORCE_ENCRYPTED_FILE_STORAGE": "true"},
                 {"gemini_auth_type": "oauth-personal",
                  "gemini_auth_files_copied": ["oauth_creds.json"]}, False),
                ({"GOOGLE_APPLICATION_CREDENTIALS": str(adc)},
                 {"gemini_auth_type": "compute-default-credentials"}, True),
                ({"GOOGLE_APPLICATION_CREDENTIALS": str(adc),
                  "GOOGLE_CLOUD_PROJECT_ID": "alias-only"},
                 {"gemini_auth_type": "compute-default-credentials"}, False),
                ({"GOOGLE_APPLICATION_CREDENTIALS": str(adc),
                  "GOOGLE_CLOUD_PROJECT": "project",
                  "GOOGLE_CLOUD_QUOTA_PROJECT": "quota-project"},
                 {"gemini_auth_type": "compute-default-credentials"}, False),
                ({"GEMINI_API_KEY": "secret",
                  "GEMINI_API_KEY_AUTH_MECHANISM": "bearer"},
                 {"gemini_auth_type": "gemini-api-key"}, False),
            )
            for environment, auth, expected in cases:
                with self.subTest(auth=auth, environment=environment), \
                        mock.patch.object(sb.sys, "platform", "linux"), \
                        mock.patch.object(
                            sb.shutil, "which",
                            side_effect=lambda name: (
                                "/usr/bin/docker" if name == "docker" else None)):
                    plan = sb.gemini_sandbox_plan(environment, auth)
                self.assertIs(plan["requested"], expected)

    def test_sandbox_engine_detection_is_explicit(self):
        with self.subTest("macOS seatbelt"), \
                mock.patch.object(sb.sys, "platform", "darwin"), \
                mock.patch.object(
                    sb.shutil, "which",
                    side_effect=lambda name: (
                        "/usr/bin/sandbox-exec"
                        if name == "sandbox-exec" else None)):
            plan = sb.gemini_sandbox_plan(
                {}, {"gemini_auth_type": "oauth-personal"})
            self.assertTrue(plan["requested"])
            self.assertEqual(plan["engine"], "macos-seatbelt")

        with self.subTest("no engine"), \
                mock.patch.object(sb.sys, "platform", "linux"), \
                mock.patch.object(sb.shutil, "which", return_value=None):
            plan = sb.gemini_sandbox_plan(
                {"GEMINI_API_KEY": "key"},
                {"gemini_auth_type": "gemini-api-key"})
            self.assertFalse(plan["requested"])
            self.assertEqual(plan["engine"], "unavailable")

    def test_explicit_adc_is_copied_outside_model_workspace(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source = root / "outside-adc.json"
            source.write_text('{"type":"service_account"}', encoding="utf-8")
            with mock.patch.dict(os.environ, {
                    "GOOGLE_GENAI_USE_VERTEXAI": "true",
                    "GOOGLE_APPLICATION_CREDENTIALS": str(source),
            }, clear=True):
                env, metadata = sb.gemini_env_for_home(root / "isolated")
        self.assertNotEqual(env["GOOGLE_APPLICATION_CREDENTIALS"], str(source))
        self.assertTrue(Path(env["GOOGLE_APPLICATION_CREDENTIALS"]).is_absolute())
        self.assertTrue(metadata["google_application_credentials_copied"])

    def test_project_id_alias_is_canonicalized_for_container_transport(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            adc = root / "adc.json"
            adc.write_text("{}", encoding="utf-8")
            with mock.patch.dict(os.environ, {
                    "GOOGLE_GENAI_USE_VERTEXAI": "true",
                    "GOOGLE_APPLICATION_CREDENTIALS": str(adc),
                    "GOOGLE_CLOUD_PROJECT_ID": "alias-project",
                    "GOOGLE_CLOUD_LOCATION": "us-central1",
            }, clear=True):
                env, metadata = sb.gemini_env_for_home(root / "isolated")

        self.assertEqual(env["GOOGLE_CLOUD_PROJECT"], "alias-project")
        self.assertNotIn("GOOGLE_CLOUD_PROJECT_ID", env)
        self.assertTrue(metadata["google_cloud_project_id_canonicalized"])

    def test_conflicting_project_aliases_fail_auth_preflight(self):
        with tempfile.TemporaryDirectory() as td, mock.patch.dict(os.environ, {
                "GOOGLE_GENAI_USE_VERTEXAI": "true",
                "GOOGLE_API_KEY": "key",
                "GOOGLE_CLOUD_PROJECT": "canonical-project",
                "GOOGLE_CLOUD_PROJECT_ID": "different-project",
        }, clear=True):
            _, metadata = sb.gemini_env_for_home(Path(td) / "isolated")

        self.assertIn("conflicting", metadata["gemini_auth_preflight_error"])

    def test_relative_external_adc_is_copied_and_becomes_sandbox_portable(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            workspace = root / "workspace"
            workspace.mkdir()
            source_home = root / "source-home" / ".gemini"
            source_home.mkdir(parents=True)
            adc = root / "outside-adc.json"
            adc.write_text('{"type":"service_account"}', encoding="utf-8")
            fake = root / "fake-gemini.py"
            _write_executable(
                fake,
                "import os, pathlib, sys\n"
                "if '--version' in sys.argv:\n"
                "    print('0.55.0-relative-adc-test')\n"
                "else:\n"
                "    adc = pathlib.Path(os.environ['GOOGLE_APPLICATION_CREDENTIALS'])\n"
                "    assert adc.is_absolute() and adc.is_file()\n"
                "    assert not adc.resolve().is_relative_to(pathlib.Path.cwd().resolve())\n"
                "    assert '--sandbox' in sys.argv\n"
                f"    sys.stdout.write({_success_stream()!r})\n")
            with mock.patch.dict(os.environ, {
                    "GEMINI_CLI_HOME": str(source_home.parent),
                    "GOOGLE_GENAI_USE_VERTEXAI": "true",
                    "GOOGLE_APPLICATION_CREDENTIALS": "../outside-adc.json",
            }, clear=True):
                result = sb.gemini_cli_invoke(
                    "prompt", cwd=workspace, gemini_cmd=str(fake), timeout=30)

        self.assertEqual(result["returncode"], 0)
        self.assertTrue(
            result["environment"]["google_application_credentials_copied"])
        self.assertTrue(result["environment"]["sandbox_requested"])

    def test_ambient_temp_root_cannot_place_gemini_home_in_workspace(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            workspace = root / "workspace"
            workspace.mkdir()
            source_home = root / "source-home" / ".gemini"
            source_home.mkdir(parents=True)
            fake = root / "fake-gemini.py"
            _write_executable(
                fake,
                "import os, pathlib, sys\n"
                "if '--version' in sys.argv:\n"
                "    print('0.55.0-temp-test')\n"
                "else:\n"
                "    home = pathlib.Path(os.environ['GEMINI_CLI_HOME']).resolve()\n"
                "    assert not home.is_relative_to(pathlib.Path.cwd().resolve())\n"
                f"    sys.stdout.write({_success_stream()!r})\n")
            with mock.patch.dict(os.environ, {
                    "GEMINI_CLI_HOME": str(source_home.parent),
            }, clear=True), mock.patch.object(tempfile, "tempdir", str(workspace)):
                result = sb.gemini_cli_invoke(
                    "prompt", cwd=workspace, gemini_cmd=str(fake), timeout=30)

        self.assertEqual(result["returncode"], 0)
        self.assertTrue(result["environment"]["gemini_home_outside_workdir"])

    def test_adc_inside_model_workspace_is_rejected_before_spawn(self):
        with tempfile.TemporaryDirectory() as td:
            workspace = Path(td)
            outside = workspace.parent / f"{workspace.name}-outside-adc.json"
            outside.write_text('{"type":"service_account"}', encoding="utf-8")
            try:
                direct = workspace / "adc.json"
                direct.write_text('{"type":"service_account"}', encoding="utf-8")
                linked = workspace / "linked-adc.json"
                linked.symlink_to(outside)
                for adc in (direct, linked):
                    with self.subTest(adc=adc), mock.patch.dict(os.environ, {
                            "GOOGLE_GENAI_USE_VERTEXAI": "true",
                            "GOOGLE_APPLICATION_CREDENTIALS": str(adc),
                    }, clear=True):
                        result = sb.gemini_cli_invoke(
                            "prompt", cwd=workspace,
                            gemini_cmd="this-command-must-not-run", timeout=30)
                    self.assertEqual(result["returncode"], 127)
                    self.assertIn("must be outside", result["protocol_error"])
            finally:
                outside.unlink(missing_ok=True)


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
            _write_executable(fake,
                "import json, os, pathlib, sys\n"
                "if '--version' in sys.argv:\n"
                "    print('0.55.0-test')\n"
                "    raise SystemExit(0)\n"
                "prompt = next(a.split('=', 1)[1] for a in sys.argv if a.startswith('--prompt='))\n"
                "assert sys.argv[sys.argv.index('--output-format') + 1] == 'stream-json'\n"
                "assert '--model=gemini-test' in sys.argv\n"
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
                f"sys.stdout.write({_success_stream()!r})\n")
            runs = root / "runs"

            result = sb.run_agent(argparse.Namespace(
                agent="gemini", tasks=str(tasks), runs=str(runs),
                model="gemini-test", gemini_cmd=str(fake),
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
            self.assertEqual(environment["gemini_cli_version"], "0.55.0-test")
            self.assertEqual(environment["gemini_cli_version_status"], "reported")
            self.assertNotIn("Task prompt:", environment["command"])
            self.assertTrue((base / "trace.jsonl").exists())

    def test_protocol_failure_with_zero_exit_uses_gemini_failure_marker(self):
        with tempfile.TemporaryDirectory() as td:
            fake = Path(td) / "malformed_gemini.py"
            _write_executable(fake, "print('not-json')\n")
            command = str(fake)
            result = sb.gemini_cli_invoke(
                "prompt", gemini_cmd=command, timeout=30,
                output_format="stream-json")
            outcome = sb.GeminiBackend().invoke_answer(
                sb.InvocationRequest("prompt", Path(td), None, 30),
                gemini_cmd=command,
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

    def test_invalid_utf8_version_probe_is_not_reported_as_provenance(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            fake = root / "invalid_version_gemini.py"
            _write_executable(
                fake,
                "import os, sys\n"
                "if '--version' in sys.argv:\n"
                "    os.write(1, b'\\xff\\n')\n"
                "else:\n"
                f"    sys.stdout.write({_success_stream()!r})\n")

            result = sb.gemini_cli_invoke(
                "prompt", cwd=root / "workspace", gemini_cmd=str(fake),
                timeout=30)

        environment = result["environment"]
        self.assertEqual(
            environment["gemini_cli_version_status"], "unavailable")
        self.assertIn(
            "not valid UTF-8", environment["gemini_cli_version_error"])
        self.assertFalse(
            environment["gemini_cli_version_stdout_utf8_valid"])
        self.assertNotIn("gemini_cli_version", environment)

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

    def test_trace_tool_arguments_follow_each_official_schema(self):
        cases = (
            ("read_file", {"file_path": "skills/root/SKILL.md"},
             "skills/root/SKILL.md"),
            ("list_directory", {"dir_path": "skills/root"}, "skills/root"),
            ("grep_search", {"pattern": "SKILL.md", "dir_path": "src"}, "src"),
            ("glob", {"pattern": "**/SKILL.md", "dir_path": "src"}, "src"),
        )
        for name, parameters, expected_path_fragment in cases:
            with self.subTest(name=name):
                flat = sb._gemini_tool_flat_record(name, parameters)
                self.assertIn(expected_path_fragment, flat["path"])
                self.assertEqual(flat["parameters"], parameters)

        for name in ("glob", "grep_search"):
            flat = sb._gemini_tool_flat_record(name, {"pattern": "**/SKILL.md"})
            self.assertEqual(flat["path"], "")
        many_parameters = {"include": ["src/a.py", "skills/root/SKILL.md"]}
        many = sb._gemini_tool_flat_record("read_many_files", many_parameters)
        self.assertEqual(many["path"], "")
        self.assertEqual(many["parameters"], many_parameters)

    def test_read_many_files_include_intent_is_not_skill_read_evidence(self):
        def records(name: str, parameters: dict[str, object]) -> list[dict[str, object]]:
            return [
                _event("init", session_id="session-1", model="gemini-test"),
                _event("tool_use", tool_name=name, tool_id="call-1",
                       parameters=parameters),
                _event("tool_result", tool_id="call-1", status="success",
                       output="done"),
                _event("message", role="assistant", content="answer", delta=True),
                _event("result", status="success"),
            ]

        _, many_metrics = sb.normalize_trace_records(
            records("read_many_files", {
                "include": ["**/SKILL.md"]}),
            source="gemini")
        _, search_metrics = sb.normalize_trace_records(
            records("glob", {"pattern": "**/SKILL.md"}), source="gemini")
        # Gemini stream-json omits read_many_files' structured returnDisplay,
        # so a successful glob call proves intent, not that any file matched.
        self.assertFalse(many_metrics["skill_invoked"])
        self.assertFalse(search_metrics["skill_invoked"])

        records_with_named_output = records(
            "glob", {"pattern": "*.md", "dir_path": "skills"})
        records_with_named_output[2]["output"] = "skills/root/SKILL.md"
        _, output_metrics = sb.normalize_trace_records(
            records_with_named_output, source="gemini")
        self.assertFalse(output_metrics["skill_invoked"])

        no_match_grep = records(
            "grep_search", {
                "pattern": "needle", "dir_path": "/repo/skills/root"})
        no_match_grep[2]["output"] = "No matches found"
        _, grep_metrics = sb.normalize_trace_records(
            no_match_grep, source="gemini")
        self.assertFalse(grep_metrics["skill_invoked"])

    def test_warning_events_and_failed_tools_keep_distinct_trace_semantics(self):
        warning_records = [
            _event("init", session_id="session-1", model="gemini-test"),
            _event("error", severity="warning", message="Loop detected"),
            _event("message", role="assistant", content="answer", delta=True),
            _event("result", status="success"),
        ]
        _, warning_metrics = sb.normalize_trace_records(
            warning_records, source="gemini")
        self.assertEqual(warning_metrics["errors"], 0)

        failed_tools = (
            ("read_file", {"file_path": "missing.txt"}, "file_reads", 0),
            ("write_file", {"file_path": "denied.txt", "content": "x"},
             "file_writes", 0),
            ("run_shell_command", {"command": "false"}, "commands", 0),
            ("activate_skill", {"name": "ambient"}, "skill_invoked", False),
        )
        for name, parameters, metric, expected in failed_tools:
            with self.subTest(name=name):
                failed_records = [
                    _event("init", session_id="session-1", model="gemini-test"),
                    _event("tool_use", tool_name=name, tool_id="call-1",
                           parameters=parameters),
                    _event("tool_result", tool_id="call-1", status="error"),
                    _event("message", role="assistant", content="recovered",
                           delta=True),
                    _event("result", status="success"),
                ]
                _, metrics = sb.normalize_trace_records(
                    failed_records, source="gemini")
                self.assertEqual(metrics[metric], expected)
                self.assertEqual(metrics["errors"], 1)

    def test_multi_model_stream_keeps_resolution_ambiguous_at_invocation_boundary(self):
        records = [
            _event("init", session_id="session-1", model="auto"),
            _event("message", role="assistant", content="answer", delta=True),
            _event("result", status="success", stats={
                "total_tokens": 10, "input_tokens": 6, "output_tokens": 4,
                "cached": 1, "input": 5, "duration_ms": 10,
                "tool_calls": 0,
                "models": {
                    "model-a": {"total_tokens": 4, "input_tokens": 2,
                                "output_tokens": 2, "cached": 0, "input": 2},
                    "model-b": {"total_tokens": 6, "input_tokens": 4,
                                "output_tokens": 2, "cached": 1, "input": 3},
                },
            }),
        ]
        payload = "\n".join(json.dumps(record) for record in records) + "\n"
        with tempfile.TemporaryDirectory() as td:
            fake = Path(td) / "fake_gemini.py"
            _write_executable(
                fake, f"import sys\nsys.stdout.write({payload!r})\n")
            result = sb.gemini_cli_invoke(
                "prompt", model="requested-model",
                gemini_cmd=str(fake), timeout=30)

        self.assertIsNone(result["model"])
        self.assertIsNone(result["metadata"]["resolved_model"])
        self.assertEqual(result["metadata"]["reported_models"],
                         ["model-a", "model-b"])

        with mock.patch.object(sb, "gemini_cli_invoke", return_value=result):
            outcome = sb.GeminiBackend().invoke_answer(
                sb.InvocationRequest(
                    "prompt", Path(tempfile.gettempdir()), "requested-model", 30))
        self.assertIsInstance(outcome, rc.Completed)
        self.assertIsNone(outcome.context.model)

    def test_json_without_model_stats_preserves_request_but_not_resolution(self):
        payload = json.dumps({
            "session_id": "session-json", "response": "answer",
        })
        with tempfile.TemporaryDirectory() as td:
            fake = Path(td) / "fake_gemini_json.py"
            _write_executable(
                fake, f"import sys\nsys.stdout.write({payload!r})\n")
            result = sb.gemini_cli_invoke(
                "prompt", model="requested-model", gemini_cmd=str(fake),
                timeout=30, output_format="json")

        self.assertIsNone(result["model"])
        self.assertEqual(result["metadata"]["requested_model"],
                         "requested-model")
        self.assertIsNone(result["metadata"]["resolved_model"])

    def test_workspace_provider_controls_fail_before_invocation(self):
        for relative in (
                Path("GEMINI.md"), Path(".geminiignore"),
                Path(".GeminiIgnore"), Path(".Gemini/settings.json"),
                Path(".agents/skills/ambient/SKILL.md"),
                Path(".Agents/skills/ambient/SKILL.md")):
            with self.subTest(relative=relative), tempfile.TemporaryDirectory() as td:
                workspace = Path(td)
                target = workspace / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text("ambient", encoding="utf-8")

                result = sb.gemini_cli_invoke(
                    "prompt", cwd=workspace,
                    gemini_cmd="this-command-must-not-run", timeout=30)
                outcome = sb.GeminiBackend().invoke_answer(
                    sb.InvocationRequest("prompt", workspace, None, 30),
                    gemini_cmd="this-command-must-not-run")
                artifact = workspace / "artifact"
                sb.write_runner_outcome(artifact, outcome)
                metadata = json.loads(
                    (artifact / "metadata.json").read_text(encoding="utf-8"))

            self.assertEqual(result["returncode"], 127)
            self.assertIn("provider control files", result["protocol_error"])
            self.assertEqual(result["stdout"], "")
            self.assertIsInstance(outcome, rc.SpawnFailed)
            self.assertFalse(metadata["process_observation_complete"])

    def test_ancestor_gemini_memory_inside_git_root_fails_before_invocation(self):
        with tempfile.TemporaryDirectory() as td:
            repository = Path(td) / "repository"
            workspace = repository / "fixtures" / "task"
            workspace.mkdir(parents=True)
            (repository / ".git").mkdir()
            (repository / "GeMiNi.Md").write_text(
                "ambient parent instructions", encoding="utf-8")

            result = sb.gemini_cli_invoke(
                "prompt", cwd=workspace,
                gemini_cmd="this-command-must-not-run", timeout=30)

        self.assertEqual(result["returncode"], 127)
        self.assertIn("provider control files", result["protocol_error"])
        self.assertIn("GeMiNi.Md", result["protocol_error"])
        self.assertEqual(result["stdout"], "")

    def test_nonzero_provider_error_is_preserved_in_typed_answer_failure(self):
        provider_result = {
            "answer": "", "returncode": 9, "timed_out": False,
            "elapsed_ms": 2, "stderr": "", "trace_text": "",
            "provider_error": "APIError: denied", "protocol_error": None,
            "usage": None, "metadata": {}, "environment": {}, "model": None,
        }
        with mock.patch.object(
                sb, "gemini_cli_invoke", return_value=provider_result):
            outcome = sb.GeminiBackend().invoke_answer(
                sb.InvocationRequest("prompt", Path(tempfile.gettempdir()), None, 30))
        self.assertIsInstance(outcome, rc.ProviderFailed)
        self.assertEqual(outcome.reason, "APIError: denied")

    def test_strict_json_failures_still_commit_raw_failure_artifacts(self):
        for payload in ('{"type":"init","type":"init"}\n',
                        '{"type":"result","value":NaN}\n'):
            with self.subTest(payload=payload), tempfile.TemporaryDirectory() as td:
                root = Path(td)
                fake = root / "fake_gemini.py"
                _write_executable(
                    fake, f"import sys\nsys.stdout.write({payload!r})\n")
                outcome = sb.GeminiBackend().invoke_answer(
                    sb.InvocationRequest(
                        "prompt", root / "workspace", None, 30),
                    gemini_cmd=str(fake))
                base = root / "run"

                sb.write_runner_outcome(base, outcome)

                self.assertIsInstance(outcome, rc.ProviderFailed)
                self.assertEqual((base / "trace.jsonl").read_text(), payload)
                self.assertTrue((base / "artifact-commit.json").is_file())
                metadata = json.loads((base / "metadata.json").read_text())
                self.assertFalse(metadata["provider_response_complete"])
                self.assertTrue(metadata["parse_errors"])

    def test_invalid_utf8_provider_bytes_still_commit_failure_artifacts(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            fake = root / "invalid_utf8_gemini.py"
            _write_executable(
                fake, "import os\nos.write(1, b'\\xff\\n')\n")
            outcome = sb.GeminiBackend().invoke_answer(
                sb.InvocationRequest(
                    "prompt", root / "workspace", None, 30),
                gemini_cmd=str(fake))
            base = root / "run"

            sb.write_runner_outcome(base, outcome)

            self.assertIsInstance(outcome, rc.ProviderFailed)
            self.assertEqual((base / "trace.jsonl").read_text(), "\\xff\n")
            metadata = json.loads((base / "metadata.json").read_text())
            self.assertFalse(metadata["provider_response_complete"])
            self.assertTrue(metadata["parse_errors"])
            self.assertTrue((base / "artifact-commit.json").is_file())

    def test_invalid_utf8_inside_json_string_cannot_become_success(self):
        records = (
            b'{"type":"init","session_id":"s","model":"m"}\n'
            b'{"type":"message","role":"assistant","content":"bad \\\xff",'
            b'"delta":true}\n'
            b'{"type":"result","status":"success"}\n'
        )
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            fake = root / "invalid_utf8_in_json_gemini.py"
            _write_executable(
                fake, f"import os\nos.write(1, {records!r})\n")
            outcome = sb.GeminiBackend().invoke_answer(
                sb.InvocationRequest(
                    "prompt", root / "workspace", None, 30),
                gemini_cmd=str(fake))

            base = root / "run"
            sb.write_runner_outcome(base, outcome)
            metrics = json.loads(
                (base / "metrics.json").read_text(encoding="utf-8"))

        self.assertIsInstance(outcome, rc.ProviderFailed)
        self.assertIn("\\xff", outcome.context.trace_text)
        self.assertFalse(metrics["trace_observation_complete"])
        self.assertIn("not valid UTF-8", " ".join(
            metrics["trace_protocol_errors"]))

    def test_spawn_nonzero_and_timeout_failures_keep_process_state(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            exit_nine = root / "exit_nine.py"
            _write_executable(exit_nine, "raise SystemExit(9)\n")
            sleeper = root / "sleep.py"
            _write_executable(sleeper, "import time\ntime.sleep(5)\n")
            cases = (
                ("missing-gemini-binary-for-contract-test", 30, 127, False),
                (str(exit_nine), 30, 9, False),
                (str(sleeper), 1, 124, True),
            )
            for command, timeout, returncode, timed_out in cases:
                with self.subTest(command=command):
                    result = sb.gemini_cli_invoke(
                        "prompt", gemini_cmd=command, timeout=timeout)
                    self.assertEqual(result["returncode"], returncode)
                    self.assertIs(result["timed_out"], timed_out)
                    outcome = sb.RunnerOutcome(
                        provider="gemini", answer=result["answer"],
                        returncode=result["returncode"],
                        timed_out=result["timed_out"], timeout_s=timeout,
                        elapsed_ms=result["elapsed_ms"], stderr=result["stderr"],
                        trace_text=result["trace_text"],
                    )
                    self.assertNotIsInstance(outcome, rc.Completed)

    def test_spawned_reserved_exit_codes_are_provider_failures(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            for returncode in (124, 127):
                fake = root / f"exit_{returncode}.py"
                _write_executable(fake, f"raise SystemExit({returncode})\n")
                with self.subTest(returncode=returncode):
                    outcome = sb.GeminiBackend().invoke_answer(
                        sb.InvocationRequest(
                            "prompt", root / f"workspace-{returncode}", None, 30),
                        gemini_cmd=str(fake))
                    base = root / f"run-{returncode}"
                    sb.write_runner_outcome(base, outcome)
                    metadata = json.loads(
                        (base / "metadata.json").read_text(encoding="utf-8"))

                self.assertIsInstance(outcome, rc.ProviderFailed)
                self.assertFalse(metadata["timed_out"])
                self.assertTrue(metadata["process_observation_complete"])


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

    def test_native_judge_uses_stream_lifecycles_and_preserves_raw_artifacts(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            fake = root / "fake_gemini_judge.py"
            _write_executable(fake,
                "import os, pathlib, sys\n"
                "if '--version' in sys.argv:\n"
                "    print('0.55.0-test')\n"
                "    raise SystemExit(0)\n"
                "assert sys.argv[sys.argv.index('--output-format') + 1] == 'stream-json'\n"
                "assert '--model=gemini-judge' in sys.argv\n"
                "assert '--skip-trust' in sys.argv and '--sandbox' in sys.argv\n"
                "policy_path = pathlib.Path(sys.argv[sys.argv.index('--policy') + 1])\n"
                "assert policy_path.is_relative_to(pathlib.Path(os.environ['GEMINI_CLI_HOME']))\n"
                "policy = policy_path.read_text()\n"
                "assert 'toolName = \"*\"' in policy and 'decision = \"deny\"' in policy\n"
                f"sys.stdout.write({_judge_stream()!r})\n")
            transcripts = root / "transcripts"

            row = sb.run_one_judge_task(
                self._task(root), judge_backend="gemini",
                judge_model="gemini-judge",
                backend_options={"gemini_cmd": str(fake)},
                transcripts_dir=transcripts,
            )

            self.assertTrue(row["passed"])
            self.assertEqual(row["judge_backend"], "gemini")
            self.assertEqual(row["judge_model"], "gemini-judge")
            self.assertEqual(row["usage_normalized"]["total_tokens"], 8)
            destination = transcripts / "gemini-judge-task" / "run-1"
            self.assertEqual(
                (destination / "provider-response.json").read_text(),
                _judge_stream(),
            )
            metadata = json.loads(
                (destination / "provider-metadata.json").read_text())
            self.assertEqual(metadata["session_id"], "session-judge")
            self.assertEqual(metadata["resolved_model"], "gemini-judge")
            self.assertEqual(
                metadata["environment"]["gemini_cli_version"], "0.55.0-test")

    def test_programmatic_gemini_judge_rejects_unimplemented_explore(self):
        with tempfile.TemporaryDirectory() as td, self.assertRaisesRegex(
                ValueError, "only supported by the native Claude judge"):
            sb.run_one_judge_task(
                self._task(Path(td)), judge_backend="gemini",
                judge_model="gemini-judge", explore=True)

    def test_native_judge_returns_the_typed_invocation_contract(self):
        provider_result = {
            "answer": '{"passed":true}',
            "stderr": "",
            "returncode": 0,
            "usage": {"input_tokens": 2, "output_tokens": 1,
                      "total_tokens": 3},
            "model": "gemini-judge",
            "raw_response": _judge_stream(),
            "metadata": {"session_id": "session-judge",
                         "provider_tool_calls": 0},
        }
        with mock.patch.object(
                sb, "gemini_cli_invoke", return_value=provider_result):
            invocation = sb.gemini_judge_invoke(
                "prompt", judge_model="gemini-judge", gemini_cmd="gemini",
                explore_hint=None)
        self.assertIsInstance(invocation, jc.JudgeInvocation)
        self.assertEqual(invocation.raw_response, _judge_stream())
        self.assertEqual(invocation.metadata["session_id"], "session-judge")

    def test_invalid_requested_model_is_a_typed_judge_failure(self):
        with mock.patch.object(sb, "run_argv_capture") as spawn:
            invocation = sb.gemini_judge_invoke(
                "prompt", judge_model="gemini-\ud800", gemini_cmd="gemini",
                explore_hint=None)

        spawn.assert_not_called()
        self.assertNotEqual(invocation.returncode, 0)
        self.assertIsNone(invocation.metadata["requested_model"])
        json.dumps(dict(invocation.metadata), ensure_ascii=False).encode("utf-8")

    def test_judge_fails_closed_when_provider_reports_tool_use(self):
        provider_result = {
            "answer": '{"passed":true}', "stderr": "", "returncode": 0,
            "usage": None, "model": "gemini-judge",
            "raw_response": _judge_stream(tool_calls=1),
            "metadata": {"provider_tool_calls": 1}, "environment": {},
            "protocol_error": None, "provider_error": None,
        }
        with mock.patch.object(
                sb, "gemini_cli_invoke", return_value=provider_result):
            invocation = sb.gemini_judge_invoke(
                "prompt", judge_model="gemini-judge", gemini_cmd="gemini",
                explore_hint=None)

        self.assertEqual(invocation.returncode, 0)
        self.assertIs(
            invocation.invocation_state, sb.InvocationState.PROVIDER_FAILED)
        self.assertFalse(invocation.succeeded)
        self.assertIn("observed 1 tool lifecycle", invocation.stderr)

    def test_programmatic_judge_materializes_registry_default_command(self):
        provider_result = {
            "answer": '{"passed":true,"score":1,"rationale":"ok"}',
            "stderr": "", "returncode": 0, "usage": None,
            "model": "gemini-judge", "raw_response": _judge_stream(),
            "metadata": {"provider_tool_calls": 0}, "environment": {},
            "protocol_error": None, "provider_error": None,
        }
        with tempfile.TemporaryDirectory() as td, mock.patch.object(
                sb, "gemini_cli_invoke", return_value=provider_result) as invoke:
            row = sb.run_one_judge_task(
                self._task(Path(td)), judge_backend="gemini",
                judge_model="gemini-judge")
        self.assertTrue(row["passed"])
        self.assertEqual(invoke.call_args.kwargs["gemini_cmd"], ac.GEMINI_DEFAULT_CMD)

    def test_nonzero_provider_error_is_kept_in_judge_diagnostics(self):
        provider_result = {
            "answer": "", "stderr": "", "returncode": 9, "usage": None,
            "model": "gemini-judge", "raw_response": "{}", "metadata": {},
            "environment": {}, "protocol_error": None,
            "provider_error": "APIError: denied",
        }
        with mock.patch.object(
                sb, "gemini_cli_invoke", return_value=provider_result):
            invocation = sb.gemini_judge_invoke(
                "prompt", judge_model="gemini-judge", gemini_cmd="gemini",
                explore_hint=None)
        self.assertEqual(invocation.returncode, 9)
        self.assertIn("APIError: denied", invocation.stderr)

    def test_multi_model_judge_does_not_claim_the_requested_model(self):
        provider_result = {
            "answer": '{"passed":true}', "stderr": "", "returncode": 0,
            "usage": None, "model": None, "raw_response": _judge_stream(),
            "metadata": {"reported_models": ["model-a", "model-b"],
                         "resolved_model": None},
            "environment": {}, "protocol_error": None, "provider_error": None,
        }
        with mock.patch.object(
                sb, "gemini_cli_invoke", return_value=provider_result):
            invocation = sb.gemini_judge_invoke(
                "prompt", judge_model="requested-model", gemini_cmd="gemini",
                explore_hint=None)
        self.assertEqual(invocation.model_label, "gemini/multi-model")

    def test_unreported_judge_model_does_not_claim_the_requested_model(self):
        provider_result = {
            "answer": '{"passed":true}', "stderr": "", "returncode": 0,
            "usage": None, "model": None, "raw_response": _judge_stream(),
            "metadata": {"requested_model": "requested-model",
                         "reported_models": [], "resolved_model": None},
            "environment": {}, "protocol_error": None, "provider_error": None,
        }
        with mock.patch.object(
                sb, "gemini_cli_invoke", return_value=provider_result):
            invocation = sb.gemini_judge_invoke(
                "prompt", judge_model="requested-model", gemini_cmd="gemini",
                explore_hint=None)

        self.assertEqual(invocation.model_label, "gemini/unreported")
        self.assertEqual(
            invocation.metadata["requested_model"], "requested-model")


@unittest.skipUnless(
    os.environ.get("RUN_GEMINI_SMOKE") == "1",
    "token-backed smoke: set RUN_GEMINI_SMOKE=1 (needs Gemini CLI auth)",
)
class GeminiLiveSmokeTests(unittest.TestCase):
    def test_run_agent_writes_one_execution_valid_gemini_run(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            manifest = make_eval_repo(root)
            rows = sb.prepared_task_rows(
                manifest, sb.validate_manifest(manifest), split="tune")
            row = next(item for item in rows
                       if item["variant"] == "with_skill")
            tasks = root / "tasks.jsonl"
            tasks.write_text(json.dumps(row) + "\n", encoding="utf-8")
            runs = root / "runs"
            model = os.environ.get("SMOKE_GEMINI_MODEL", "gemini-2.5-flash")

            sb.run_agent(argparse.Namespace(
                agent="gemini", tasks=str(tasks), runs=str(runs), model=model,
                gemini_cmd=os.environ.get("GEMINI_SMOKE_CMD", "gemini"),
                timeout=int(os.environ.get("GEMINI_SMOKE_TIMEOUT", "120")),
            ))

            base = runs / row["run_dir"]
            output = (base / "output.md").read_text(encoding="utf-8")
            metadata = sb.read_metadata_base(base)
            self.assertTrue(am.execution_valid(metadata, output), metadata)
            self.assertEqual(metadata["provider"], "gemini")
            metrics = json.loads((base / "metrics.json").read_text())
            self.assertTrue(metrics["skill_invoked"], metrics)
            self.assertGreaterEqual(metrics["file_reads"], 1)
            self.assertTrue(metrics["skill_invocation_evidence"])


if __name__ == "__main__":
    unittest.main()
