"""Provider CLI plumbing for Claude, Codex, Gemini, and Vibe: isolated homes,
argv construction, invocation, and output parsing.
"""
from __future__ import annotations

import copy
import errno
import json
import os
import re
import shlex
import shutil
import sys
import tempfile
import time
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from agent_capabilities import GEMINI_DEFAULT_CMD, VIBE_DEFAULT_CMD
from gemini_contracts import GeminiJsonResponse, GeminiStream
from harness_io import (
    DEFAULT_RUNNER_TIMEOUT_S,
    _stderr_with_warning,
    invoke_argv_with_timeout,
    write_json,
)
from invocation_contracts import (
    InvocationResult,
    InvocationState,
    ProcessInvocationPlan,
)
from json_contracts import strict_json_loads, validate_json_text
from telemetry_blocks import USAGE_ALIASES, _num, normalize_cost, normalize_usage
from trace_contracts import event_is_completed
from trace_normalization import (
    GEMINI_READ_ONLY_TOOLS,
    _codex_trace_protocol_error,
    normalize_trace_records,
    parse_trace_jsonl_text,
)

CODEX_TEMP_CLEANUP_RETRY_DELAYS_S = (0.05, 0.1, 0.2, 0.4, 0.8)


CODEX_HOME_FILES = ("auth.json", "config.toml")

VIBE_READ_ONLY_TOOLS = ("skill", "read_file", "grep")
VIBE_NO_TOOLS = ("re:^$",)

GEMINI_AUTH_FILES = ("oauth_creds.json", "gemini-credentials.json")
GEMINI_AUTH_ENV = (
    "GEMINI_API_KEY", "GOOGLE_API_KEY", "GOOGLE_APPLICATION_CREDENTIALS",
    "GEMINI_API_KEY_AUTH_MECHANISM",
    "GOOGLE_GENAI_USE_GCA", "GOOGLE_GENAI_USE_VERTEXAI",
    "GOOGLE_GEMINI_BASE_URL", "GOOGLE_CLOUD_ACCESS_TOKEN",
    "GOOGLE_CLOUD_PROJECT", "GOOGLE_CLOUD_PROJECT_ID",
    "GOOGLE_CLOUD_LOCATION", "GOOGLE_CLOUD_QUOTA_PROJECT", "CLOUD_SHELL",
    "GEMINI_CLI_USE_COMPUTE_ADC", "GEMINI_FORCE_ENCRYPTED_FILE_STORAGE",
    "GEMINI_FORCE_FILE_STORAGE",
)
GEMINI_AUTH_TYPES = frozenset({
    "oauth-personal", "gemini-api-key", "vertex-ai", "cloud-shell",
    "compute-default-credentials", "gateway",
})
GEMINI_AUTH_FILES_BY_TYPE = {
    # Current OAuth uses the shared FileKeychain; oauth_creds.json remains the
    # official migration fallback for older installations.
    "oauth-personal": ("oauth_creds.json", "gemini-credentials.json"),
    "gemini-api-key": ("gemini-credentials.json",),
}
GEMINI_ALLOWED_CONTROL_ENV = frozenset({
    "GEMINI_API_KEY", "GEMINI_CLI_USE_COMPUTE_ADC",
    "GEMINI_API_KEY_AUTH_MECHANISM",
    "GEMINI_FORCE_ENCRYPTED_FILE_STORAGE", "GEMINI_FORCE_FILE_STORAGE",
})
GEMINI_NONPREFIX_CONTROL_ENV = frozenset({
    "BUILD_SANDBOX", "NODE_OPTIONS", "SEATBELT_PROFILE", "SURFACE",
    "CODE_ASSIST_ENDPOINT", "CODE_ASSIST_API_VERSION",
    "DEBUG", "DEBUG_MODE", "DEBUG_PORT", "GOOGLE_VERTEX_BASE_URL",
    "GOOGLE_GENAI_API_VERSION", "OAUTH_CALLBACK_HOST",
    "OAUTH_CALLBACK_PORT", "BROWSER", "NO_BROWSER",
})


GEMINI_WIRE_CONTRACT_COMMIT = "d55e366f6ab393e024c613d940fead3696d56eac"
GEMINI_WIRE_CONTRACT_PACKAGE_VERSION = "0.55.0-nightly.20260729.g3499c84f7"
# Mirrors the pinned CLI's atCommandProcessor path grammar. Any unescaped
# match is preprocessed before the tool-policy/event loop, so it cannot be
# permitted in an evidence-complete benchmark prompt.
GEMINI_ACTIVE_AT_PATH = re.compile(
    r'(?<!\\)@(?:(?:"[^"]*")|(?:\\.|[^ \t\n\r,;!?()\[\]{}.]|\.(?!$|[ \t\n\r])))+'
)


def coerce_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def run_argv_capture(plan: ProcessInvocationPlan) -> InvocationResult:
    """Native agent adapter wrapper over the one subprocess owner.

    Correctness-by-construction boundary: callers must choose an explicit cwd,
    so a native agent never accidentally inherits the harness repo directory.
    `run_argv_with_timeout` owns spawn failure, process-group timeout cleanup,
    stderr capping, elapsed time, and returncode shape; this function only adapts
    that dict contract into `InvocationResult`."""
    if not isinstance(plan, ProcessInvocationPlan):
        raise TypeError("run_argv_capture requires a ProcessInvocationPlan")
    outcome = invoke_argv_with_timeout(plan)
    if outcome.returncode is None or outcome.elapsed_ms is None:
        raise RuntimeError("subprocess owner returned a non-process invocation outcome")
    return InvocationResult(stdout=outcome.stdout,
                            stderr=outcome.stderr[:4000],
                            returncode=outcome.returncode,
                            elapsed_ms=outcome.elapsed_ms,
                            invocation_state=outcome.state,
                            stdout_utf8_valid=(
                                outcome.metadata.get("stdout_utf8_valid") is not False),
                            stderr_utf8_valid=(
                                outcome.metadata.get("stderr_utf8_valid") is not False),
                            timed_out=outcome.timed_out,
                            adapter_metadata=dict(outcome.metadata))


def cleanup_codex_invoke_temp(path: Path) -> dict[str, Any]:
    """Remove one isolated Codex invocation directory without losing its result.

    macOS can report ``ENOTEMPTY`` while a just-finished plugin clone is still
    changing the tree; busy mounts can similarly report ``EBUSY``. Retry only
    those transient errors. A final ignore-errors removal is deliberately the
    last fallback, and every non-normal cleanup is returned as observable,
    path-free metadata for stderr/environment artifacts.
    """
    attempts = 0
    last_error: BaseException | None = None
    max_attempts = 1 + len(CODEX_TEMP_CLEANUP_RETRY_DELAYS_S)
    for attempt in range(max_attempts):
        attempts += 1
        try:
            shutil.rmtree(path)
        except FileNotFoundError as exc:
            # Before Python 3.13, a concurrent deletion of an interior entry can
            # escape from rmtree as FileNotFoundError even while the root remains.
            # Only call the invocation directory removed after checking the root.
            last_error = exc
            try:
                root_missing = not path.exists()
            except OSError:
                root_missing = False
            if root_missing:
                result = {"status": "removed", "attempts": attempts, "retry_count": attempts - 1}
                if attempts > 1:
                    result["warning"] = f"isolated Codex temporary-home cleanup recovered after {attempts} attempts (ENOENT)"
                return result
            if attempt >= len(CODEX_TEMP_CLEANUP_RETRY_DELAYS_S):
                break
            time.sleep(CODEX_TEMP_CLEANUP_RETRY_DELAYS_S[attempt])
        except OSError as exc:
            last_error = exc
            transient = exc.errno in {errno.ENOTEMPTY, errno.EBUSY}
            if not transient or attempt >= len(CODEX_TEMP_CLEANUP_RETRY_DELAYS_S):
                break
            time.sleep(CODEX_TEMP_CLEANUP_RETRY_DELAYS_S[attempt])
        except Exception as exc:  # cleanup must never replace captured provider output
            last_error = exc
            break
        else:
            result = {"status": "removed", "attempts": attempts, "retry_count": attempts - 1}
            if attempts > 1:
                code = errno.errorcode.get(getattr(last_error, "errno", None), type(last_error).__name__)
                result["warning"] = f"isolated Codex temporary-home cleanup recovered after {attempts} attempts ({code})"
            return result

    fallback_error: BaseException | None = None
    try:
        shutil.rmtree(path, ignore_errors=True)
    except Exception as exc:  # a patched/platform implementation may still raise
        fallback_error = exc
    try:
        retained = path.exists()
    except OSError:
        retained = True
    error = fallback_error or last_error
    code = errno.errorcode.get(getattr(error, "errno", None), type(error).__name__ if error is not None else "unknown")
    status = "retained" if retained else "removed_after_fallback"
    return {
        "status": status,
        "attempts": attempts,
        "retry_count": max(0, attempts - 1),
        "fallback_attempted": True,
        "warning": (
            f"isolated Codex temporary-home cleanup could not fully remove its unique directory after {attempts} attempts ({code}); it will not be reused"
            if retained else
            f"isolated Codex temporary-home cleanup required the final fallback after {attempts} attempts ({code})"
        ),
    }


def seed_codex_home(codex_home: Path) -> dict[str, Any]:
    """Copy portable Codex auth/config into an isolated CODEX_HOME.

    `$CODEX_HOME/skills` is also Codex's skill-discovery surface, so seeding is
    file-allowlisted: copy auth/config files only, never user skills/plugins.
    `--ignore-user-config` keeps copied config from influencing native harness
    runs unless Codex needs it for auth compatibility."""
    codex_home.mkdir(parents=True, exist_ok=True)
    source = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
    copied: list[str] = []
    for name in CODEX_HOME_FILES:
        src = source / name
        dst = codex_home / name
        if not src.is_file():
            continue
        if src.resolve() == dst.resolve():
            continue
        shutil.copy2(src, dst)
        copied.append(name)
    return {"codex_home": str(codex_home), "codex_home_files_copied": copied, "config_isolated": True}


def codex_env_for_home(codex_home: Path) -> tuple[dict[str, str], dict[str, Any]]:
    env = os.environ.copy()
    seeded = seed_codex_home(codex_home)
    env["CODEX_HOME"] = seeded["codex_home"]
    # Do not persist the scratch auth/config path into run artifacts. It is not
    # model-visible for answer/judge runs, but it is still a credential-bearing
    # directory and should not become a handle for later artifact readers.
    meta = {**seeded, "codex_home": "<isolated CODEX_HOME outside workdir>"}
    return env, meta


def _strip_json_comments(text: str) -> str:
    """Match Gemini's JSONC comment support without changing string bytes."""
    out: list[str] = []
    index = 0
    in_string = False
    escaped = False
    while index < len(text):
        char = text[index]
        following = text[index + 1] if index + 1 < len(text) else ""
        if in_string:
            out.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            index += 1
            continue
        if char == '"':
            in_string = True
            out.append(char)
            index += 1
            continue
        if char == "/" and following == "/":
            index += 2
            while index < len(text) and text[index] not in "\r\n":
                index += 1
            continue
        if char == "/" and following == "*":
            index += 2
            while index < len(text):
                if (text[index] == "*" and index + 1 < len(text)
                        and text[index + 1] == "/"):
                    index += 2
                    break
                if text[index] in "\r\n":
                    out.append(text[index])
                index += 1
            continue
        out.append(char)
        index += 1
    return "".join(out)


def _gemini_environment_auth_type(environment: Mapping[str, str]) -> str | None:
    """Mirror official ``getAuthTypeFromEnv`` precedence exactly."""
    if environment.get("GOOGLE_GENAI_USE_GCA") == "true":
        return "oauth-personal"
    if environment.get("GOOGLE_GENAI_USE_VERTEXAI") == "true":
        return "vertex-ai"
    if environment.get("GOOGLE_GEMINI_BASE_URL"):
        return "gateway"
    if environment.get("GEMINI_API_KEY"):
        return "gemini-api-key"
    if (environment.get("CLOUD_SHELL") == "true"
            or environment.get("GEMINI_CLI_USE_COMPUTE_ADC") == "true"):
        return "compute-default-credentials"
    return None


def seed_gemini_home(home_root: Path) -> dict[str, Any]:
    """Seed only auth identity into a fresh Gemini CLI home root.

    Gemini resolves user configuration below ``$GEMINI_CLI_HOME/.gemini``.
    Skills, extensions, policies, MCP servers, hooks, memory, and history are
    deliberately not copied. A configured auth type wins; environment selectors
    are consulted only when settings do not choose one, matching the official CLI.
    """
    target = home_root / ".gemini"
    target.mkdir(parents=True, exist_ok=True)
    source_root = Path(os.environ.get("GEMINI_CLI_HOME") or Path.home())
    source = source_root / ".gemini"
    environment_auth = [name for name in GEMINI_AUTH_ENV if os.environ.get(name)]
    environment_auth_type = _gemini_environment_auth_type(os.environ)
    copied: list[str] = []
    selected_type: str | None = None
    configured_type: str | None = None
    selected_source = "unconfigured"
    settings_error: str | None = None
    source_settings = source / "settings.json"
    if source_settings.is_file():
        try:
            content = source_settings.read_text(encoding="utf-8")
            raw = strict_json_loads(_strip_json_comments(content))
            security = raw.get("security") if isinstance(raw, dict) else None
            auth = security.get("auth") if isinstance(security, dict) else None
            candidate = auth.get("selectedType") if isinstance(auth, dict) else None
            if isinstance(auth, dict) and "selectedType" in auth and (
                    not isinstance(candidate, str) or not candidate.strip()):
                raise ValueError(
                    "Gemini auth selectedType must be a non-empty string")
            if isinstance(candidate, str) and candidate.strip():
                if candidate not in GEMINI_AUTH_TYPES:
                    raise ValueError(
                        f"unsupported Gemini auth selectedType {candidate!r}")
                configured_type = candidate
                selected_type = candidate
                selected_source = "settings"
        except (OSError, TypeError, ValueError, json.JSONDecodeError,
                RecursionError) as exc:
            # This metadata is durable and source-setting exceptions can carry
            # secret host paths or raw configured values. Persist only a
            # path/value-free category; the invocation has a generic preflight
            # diagnostic for operators.
            settings_error = type(exc).__name__
    if selected_type is None and settings_error is None:
        selected_type = environment_auth_type
        if selected_type is not None:
            selected_source = "environment"
    if (selected_type == "cloud-shell"
            and (os.environ.get("CLOUD_SHELL") == "true"
                 or os.environ.get("GEMINI_CLI_USE_COMPUTE_ADC") == "true")):
        # The official headless entry point normalizes this legacy configured
        # value before auth validation. Persist the effective child plan while
        # retaining the configured value as provenance.
        selected_type = "compute-default-credentials"
    credential_files = GEMINI_AUTH_FILES_BY_TYPE.get(selected_type or "", ())
    if (selected_type == "gemini-api-key" and os.environ.get("GEMINI_API_KEY")):
        credential_files = ()
    if selected_type == "oauth-personal":
        if (os.environ.get("GOOGLE_GENAI_USE_GCA") == "true"
                and os.environ.get("GOOGLE_CLOUD_ACCESS_TOKEN")):
            credential_files = ()
        elif os.environ.get("GEMINI_FORCE_ENCRYPTED_FILE_STORAGE") == "true":
            # Encrypted storage ignores legacy OAuth unless the shared store is
            # empty and migration is needed. Both files are part of this one
            # explicitly selected storage plan; no other shared state is copied.
            credential_files = (
                "gemini-credentials.json", "oauth_creds.json")
        else:
            credential_files = ("oauth_creds.json",)
    for name in credential_files:
        src = source / name
        dst = target / name
        if src.is_file() and src.resolve() != dst.resolve():
            shutil.copy2(src, dst)
            copied.append(name)
    isolated_settings: dict[str, Any] = {
        "advanced": {"ignoreLocalEnv": True},
        # The upstream default enables Clearcut usage statistics. Benchmark
        # isolation must not silently opt a user back into provider telemetry.
        "privacy": {"usageStatisticsEnabled": False},
    }
    if selected_type is not None:
        auth_settings: dict[str, Any] = {"selectedType": selected_type}
        # Gateway is an official AuthType but validateAuthMethod intentionally
        # has no gateway branch.  Headless mode reaches the core gateway
        # generator only through the documented external-auth setting.
        if selected_type == "gateway":
            auth_settings["useExternal"] = True
        isolated_settings["security"] = {"auth": auth_settings}
    write_json(target / "settings.json", isolated_settings)
    return {
        "gemini_home": str(home_root),
        "gemini_auth_files_copied": copied,
        "gemini_environment_auth": sorted(environment_auth),
        "gemini_configured_auth_type": configured_type,
        "gemini_auth_type": selected_type,
        "gemini_auth_type_source": (
            "invalid-settings" if settings_error is not None else selected_source),
        "gemini_auth_type_copied": selected_source == "settings",
        "local_env_ignored": True,
        "usage_statistics_disabled_requested": True,
        **({"gemini_settings_warning": settings_error}
           if settings_error is not None else {}),
    }


def gemini_env_for_home(
    home_root: Path, *, workspace: Path | None = None,
) -> tuple[dict[str, str], dict[str, Any]]:
    env = os.environ.copy()
    seeded = seed_gemini_home(home_root)
    control_names = sorted(
        name for name in env
        if (
            (name.startswith(("GEMINI_", "_GEMINI_"))
             and name not in GEMINI_ALLOWED_CONTROL_ENV)
            or name.startswith(("SANDBOX", "OTEL_"))
            or name in GEMINI_NONPREFIX_CONTROL_ENV
        )
    )
    custom_system_paths = sorted({
        "GEMINI_CLI_SYSTEM_SETTINGS_PATH",
        "GEMINI_CLI_SYSTEM_DEFAULTS_PATH",
    } & set(control_names))
    for name in control_names:
        env.pop(name, None)
    auth_type = seeded.get("gemini_auth_type")
    auth_env_by_type = {
        "oauth-personal": {
            "GOOGLE_GENAI_USE_GCA", "GOOGLE_CLOUD_ACCESS_TOKEN",
            "GOOGLE_APPLICATION_CREDENTIALS", "GOOGLE_CLOUD_PROJECT",
            "GOOGLE_CLOUD_PROJECT_ID", "GOOGLE_CLOUD_QUOTA_PROJECT",
            "GEMINI_FORCE_ENCRYPTED_FILE_STORAGE", "GEMINI_FORCE_FILE_STORAGE",
        },
        "gemini-api-key": {
            "GEMINI_API_KEY", "GEMINI_API_KEY_AUTH_MECHANISM",
            "GEMINI_FORCE_ENCRYPTED_FILE_STORAGE",
            "GEMINI_FORCE_FILE_STORAGE",
        },
        "vertex-ai": {
            "GOOGLE_GENAI_USE_VERTEXAI", "GOOGLE_API_KEY",
            "GEMINI_API_KEY_AUTH_MECHANISM",
            "GOOGLE_APPLICATION_CREDENTIALS", "GOOGLE_CLOUD_PROJECT",
            "GOOGLE_CLOUD_PROJECT_ID", "GOOGLE_CLOUD_LOCATION",
            "GOOGLE_CLOUD_QUOTA_PROJECT",
        },
        "compute-default-credentials": {
            "GOOGLE_APPLICATION_CREDENTIALS", "GOOGLE_CLOUD_PROJECT",
            "GOOGLE_CLOUD_PROJECT_ID", "GOOGLE_CLOUD_LOCATION",
            "GOOGLE_CLOUD_QUOTA_PROJECT", "CLOUD_SHELL",
            "GEMINI_CLI_USE_COMPUTE_ADC",
        },
        "gateway": {"GOOGLE_GEMINI_BASE_URL", "GEMINI_API_KEY"},
    }
    relevant_auth_env = auth_env_by_type.get(auth_type, set())
    irrelevant_auth_env = sorted(
        name for name in GEMINI_AUTH_ENV
        if name in env and name not in relevant_auth_env)
    if (env.get("GEMINI_API_KEY_AUTH_MECHANISM") not in {None, "bearer"}
            and "GEMINI_API_KEY_AUTH_MECHANISM" not in irrelevant_auth_env):
        irrelevant_auth_env.append("GEMINI_API_KEY_AUTH_MECHANISM")
        irrelevant_auth_env.sort()
    if (auth_type == "vertex-ai" and not env.get("GOOGLE_API_KEY")
            and "GEMINI_API_KEY_AUTH_MECHANISM" in env
            and "GEMINI_API_KEY_AUTH_MECHANISM" not in irrelevant_auth_env):
        irrelevant_auth_env.append("GEMINI_API_KEY_AUTH_MECHANISM")
        irrelevant_auth_env.sort()
    for name in irrelevant_auth_env:
        env.pop(name, None)
    auth_preflight_error: str | None = None
    project_id_canonicalized = False
    project_id = env.pop("GOOGLE_CLOUD_PROJECT_ID", None)
    project = env.get("GOOGLE_CLOUD_PROJECT")
    if project_id:
        if project and project != project_id:
            auth_preflight_error = (
                "conflicting Gemini Google Cloud project selectors")
        elif not project:
            # Gemini accepts PROJECT_ID on the host, but its container wrapper
            # forwards only GOOGLE_CLOUD_PROJECT. Own one canonical spelling.
            env["GOOGLE_CLOUD_PROJECT"] = project_id
            project_id_canonicalized = True
    env["GEMINI_CLI_HOME"] = seeded["gemini_home"]
    env["NO_BROWSER"] = "true"
    adc_copied = False
    raw_adc = env.get("GOOGLE_APPLICATION_CREDENTIALS")
    if raw_adc:
        adc_source = Path(raw_adc).expanduser()
        if not adc_source.is_absolute() and workspace is not None:
            adc_source = workspace / adc_source
        if adc_source.is_absolute() and adc_source.is_file():
            adc_target = home_root / "auth" / "application-default-credentials.json"
            adc_target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(adc_source, adc_target)
            env["GOOGLE_APPLICATION_CREDENTIALS"] = str(adc_target.resolve())
            adc_copied = True
    file_storage_forced = False
    encrypted_oauth = (
        auth_type == "oauth-personal"
        and env.get("GEMINI_FORCE_ENCRYPTED_FILE_STORAGE") == "true"
    )
    access_token_portable = bool(
        env.get("GOOGLE_GENAI_USE_GCA")
        and env.get("GOOGLE_CLOUD_ACCESS_TOKEN"))
    portable_oauth = bool(
        seeded.get("gemini_auth_files_copied")
        or adc_copied
        or access_token_portable)
    if encrypted_oauth and portable_oauth:
        # HybridTokenStorage otherwise prefers the fixed host keychain service
        # when one is available. Force the disposable file backend even for
        # ADC/access-token bootstrap because token listeners can still write.
        file_storage_forced = env.get("GEMINI_FORCE_FILE_STORAGE") != "true"
        env["GEMINI_FORCE_FILE_STORAGE"] = "true"
    elif encrypted_oauth:
        # A native-keychain token cannot be safely copied or redirected. Fail
        # closed rather than switch the child to an empty file store or permit
        # it to refresh credentials in the host keychain.
        auth_preflight_error = (
            "encrypted Gemini OAuth requires portable OAuth credential "
            "material; native-keychain-only credentials cannot be isolated")
    elif auth_type == "oauth-personal" and not portable_oauth:
        auth_preflight_error = (
            "Gemini OAuth requires portable credential material; interactive "
            "browser/device authentication is disabled for benchmark runs")
    api_key_file_auth = (
        auth_type == "gemini-api-key"
        and not env.get("GEMINI_API_KEY")
        and "gemini-credentials.json" in seeded.get(
            "gemini_auth_files_copied", ()))
    if api_key_file_auth:
        file_storage_forced = env.get("GEMINI_FORCE_FILE_STORAGE") != "true"
        env["GEMINI_FORCE_FILE_STORAGE"] = "true"
    elif (auth_type == "gemini-api-key"
          and not env.get("GEMINI_API_KEY")
          and auth_preflight_error is None):
        auth_preflight_error = (
            "Gemini API-key auth requires GEMINI_API_KEY or portable file "
            "credential material; native-keychain-only credentials cannot be isolated")
    if seeded.get("gemini_settings_warning") is not None:
        auth_preflight_error = (
            "source Gemini settings could not be validated; refusing to "
            "replace an invalid auth plan with environment-selected auth")
    home_outside_workdir = (
        True if workspace is None
        else not _path_is_within(home_root, workspace))
    return env, {
        **seeded,
        "gemini_home": "<isolated GEMINI_CLI_HOME outside workdir>",
        "gemini_home_outside_workdir": home_outside_workdir,
        "config_isolated": True,
        "user_skills_isolated": True,
        "user_extensions_isolated": True,
        "user_mcp_isolated": True,
        "user_hooks_isolated": True,
        "user_context_isolated": True,
        "gemini_control_env_removed": control_names,
        "gemini_irrelevant_auth_env_removed": irrelevant_auth_env,
        "custom_system_settings_paths_removed": custom_system_paths,
        # Gemini loads environment files before its parsed --skip-trust takes
        # effect. Leaving early trust unset prevents ancestor .gemini/.env from
        # being considered; the argv flag trusts the workspace only afterward.
        "workspace_trust_deferred_until_after_env_load": True,
        # The CLI may still load machine-owned default system policy paths.
        # That administrator tier is outside the per-run home boundary.
        "system_settings_may_be_inherited": True,
        "google_application_credentials_copied": adc_copied,
        "google_cloud_project_id_canonicalized": project_id_canonicalized,
        "gemini_file_storage_forced": file_storage_forced,
        "browser_auth_suppressed": True,
        **({"gemini_auth_preflight_error": auth_preflight_error}
           if auth_preflight_error is not None else {}),
    }


def _path_is_within(candidate: Path, root: Path) -> bool:
    """Containment under both lexical and symlink-resolved path spellings."""
    lexical_candidate = Path(os.path.abspath(candidate))
    lexical_root = Path(os.path.abspath(root))
    resolved_candidate = candidate.resolve(strict=False)
    resolved_root = root.resolve(strict=False)
    return any(
        child == parent or parent in child.parents
        for child, parent in (
            (lexical_candidate, lexical_root),
            (resolved_candidate, resolved_root),
        )
    )


def gemini_workspace_adc_path(
    environment: Mapping[str, str], workspace: Path,
) -> tuple[Path, Path] | None:
    raw = environment.get("GOOGLE_APPLICATION_CREDENTIALS")
    if not raw:
        return None
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        candidate = workspace / candidate
    # Preserve both spellings: lexical containment catches a workspace symlink
    # to an outside secret, while resolved containment catches an outside
    # symlink targeting a model-readable credential.
    lexical = Path(os.path.abspath(candidate))
    return lexical, candidate.resolve(strict=False)


def gemini_sandbox_plan(
    environment: Mapping[str, str], auth_metadata: Mapping[str, Any],
) -> dict[str, Any]:
    """Say whether Gemini's nested sandbox can receive the selected auth.

    Gemini's container sandbox re-runs authentication under a different
    hostname/user.  It forwards API keys and mounts an explicit ADC file, but
    it does not forward access-token GCA or FileKeychain's host-bound key.
    Omitting the provider sandbox for those plans keeps authentication working;
    the harness policy/config/workspace isolation remains active and the weaker
    containment is recorded explicitly.
    """
    auth_type = auth_metadata.get("gemini_auth_type")
    if sys.platform == "darwin" and shutil.which("sandbox-exec"):
        return {
            "requested": True,
            "engine": "macos-seatbelt",
            "credential_transport": "host-process-environment",
        }
    engine = next(
        (name for name in ("docker", "podman") if shutil.which(name)), None)
    if engine is None:
        return {
            "requested": False,
            "engine": "unavailable",
            "credential_transport": "not-applicable",
            "disabled_reason": (
                "no supported Gemini sandbox engine was found on this host"),
        }
    if (environment.get("GOOGLE_CLOUD_PROJECT_ID")
            and not environment.get("GOOGLE_CLOUD_PROJECT")):
        return {
            "requested": False,
            "engine": engine,
            "credential_transport": "nonportable-project-id-alias",
            "disabled_reason": (
                "Gemini container sandbox does not forward "
                "GOOGLE_CLOUD_PROJECT_ID"),
        }
    if environment.get("GOOGLE_CLOUD_QUOTA_PROJECT"):
        return {
            "requested": False,
            "engine": engine,
            "credential_transport": "nonportable-quota-project",
            "disabled_reason": (
                "Gemini container sandbox does not forward "
                "GOOGLE_CLOUD_QUOTA_PROJECT"),
        }
    if environment.get("GEMINI_API_KEY_AUTH_MECHANISM") == "bearer":
        return {
            "requested": False,
            "engine": engine,
            "credential_transport": "nonportable-api-key-auth-mechanism",
            "disabled_reason": (
                "Gemini container sandbox does not forward the selected "
                "API-key bearer-auth mechanism"),
        }
    explicit_adc = environment.get("GOOGLE_APPLICATION_CREDENTIALS")
    adc_portable = bool(
        explicit_adc and Path(explicit_adc).expanduser().is_absolute()
        and Path(explicit_adc).expanduser().is_file())
    if auth_type == "oauth-personal":
        copied = set(auth_metadata.get("gemini_auth_files_copied") or ())
        gca_token = bool(
            environment.get("GOOGLE_GENAI_USE_GCA")
            and environment.get("GOOGLE_CLOUD_ACCESS_TOKEN"))
        encrypted_store = (
            "gemini-credentials.json" in copied
            or (environment.get("GEMINI_FORCE_ENCRYPTED_FILE_STORAGE") == "true"
                and bool(copied)))
        if gca_token or encrypted_store:
            return {
                "requested": False,
                "engine": engine,
                "credential_transport": (
                    "nonportable-gca-access-token" if gca_token
                    else "nonportable-encrypted-file-keychain"),
                "disabled_reason": (
                    "Gemini container sandbox cannot transport the selected "
                    "host-bound OAuth credential material"),
            }
        if "oauth_creds.json" not in copied and not adc_portable:
            return {
                "requested": False,
                "engine": engine,
                "credential_transport": "unproven-oauth",
                "disabled_reason": (
                    "Gemini OAuth has no proven credential bridge into the "
                    "nested sandbox"),
            }
    if auth_type == "gemini-api-key" and not environment.get("GEMINI_API_KEY"):
        return {
            "requested": False,
            "engine": engine,
            "credential_transport": "nonportable-keychain-api-key",
            "disabled_reason": (
                "Gemini API key is selected from host credential storage, "
                "which is not portable across the container sandbox"),
        }
    if auth_type in {"vertex-ai", "compute-default-credentials", "cloud-shell"}:
        portable = (
            bool(environment.get("GOOGLE_API_KEY")) or adc_portable
            if auth_type == "vertex-ai" else adc_portable)
        if not portable:
            return {
                "requested": False,
                "engine": engine,
                "credential_transport": "unproven-implicit-adc",
                "disabled_reason": (
                    "implicit ADC/metadata authentication has no proven "
                    "credential bridge into Gemini's nested sandbox"),
            }
    return {
        "requested": True,
        "engine": engine,
        "credential_transport": (
            "explicit-adc-file" if adc_portable
            else "isolated-home-oauth-file"
            if auth_type == "oauth-personal"
            else "forwarded-environment" if auth_type is not None
            else "unconfigured-auth"),
    }


def gemini_policy_text(*, allow_read_tools: bool) -> str:
    rules = [
        (
            '[[rule]]\n'
            'toolName = "*"\n'
            'decision = "deny"\n'
            'priority = 900\n'
        ),
    ]
    if allow_read_tools:
        names = ", ".join(json.dumps(name) for name in GEMINI_READ_ONLY_TOOLS)
        rules.append(
            '[[rule]]\n'
            f'toolName = [{names}]\n'
            'decision = "allow"\n'
            'priority = 950\n'
        )
    return "\n".join(rules)


def validate_gemini_prompt(prompt: Any) -> str:
    """Reject provider-side prompt preprocessing outside trace evidence."""
    if not isinstance(prompt, str):
        raise TypeError("Gemini prompt must be text")
    validate_json_text(prompt, "Gemini prompt")
    if "\x00" in prompt:
        raise ValueError("Gemini prompt cannot contain NUL")
    if GEMINI_ACTIVE_AT_PATH.search(prompt):
        raise ValueError(
            "Gemini prompt contains active @path preprocessing syntax")
    if (prompt.startswith("/")
            and not prompt.startswith(("//", "/*"))):
        raise ValueError(
            "Gemini prompt contains active slash-command syntax")
    return prompt


def build_gemini_cli_argv(
    gemini_cmd: str | None, *, prompt: str, output_format: str,
    policy_path: Path, model: str | None, request_sandbox: bool = True,
) -> list[str]:
    if output_format not in {"json", "stream-json"}:
        raise ValueError(f"unsupported Gemini output format {output_format!r}")
    prompt = validate_gemini_prompt(prompt)
    if model is not None and (
            not isinstance(model, str) or not model.strip()):
        raise ValueError("Gemini model must be non-empty text or None")
    if model is not None:
        validate_json_text(model, "Gemini model")
    if gemini_cmd is not None and not isinstance(gemini_cmd, str):
        raise TypeError("--gemini-cmd must be one executable string")
    executable = gemini_cmd if gemini_cmd else GEMINI_DEFAULT_CMD
    validate_json_text(executable, "Gemini executable")
    if "\x00" in executable:
        raise ValueError("--gemini-cmd executable cannot contain NUL")
    if model is not None and "\x00" in model:
        raise ValueError("Gemini model cannot contain NUL")
    # This value is one literal exec token, never a shell-like command line.
    # Spaces therefore remain valid path characters and text resembling flags
    # cannot consume or reinterpret the harness-owned argv appended below.
    argv = [executable]
    # yargs treats a following dash-prefixed token as another option even when
    # the option declares nargs=1. Bind untrusted values in the same token so
    # prompts/models such as "--" remain data, never parser structure.
    argv += [f"--prompt={prompt}", "--output-format", output_format]
    if model:
        argv.append(f"--model={model}")
    argv += ["--policy", str(policy_path), "--skip-trust"]
    if request_sandbox:
        argv.append("--sandbox")
    return argv


def redact_gemini_argv(argv: list[str]) -> list[str]:
    redacted = list(argv)
    for index, value in enumerate(redacted[1:], 1):
        if value.startswith("--prompt="):
            redacted[index] = "--prompt=<prompt>"
    for flag, replacement in ((
            "--policy", "<isolated policy outside workdir>"),):
        # argv[0] is caller-controlled and can itself equal an owned flag.
        # Search only the harness-owned suffix.
        if flag in redacted[1:]:
            index = redacted.index(flag, 1)
            if index + 1 < len(redacted):
                redacted[index + 1] = replacement
    return redacted


def gemini_workspace_control_paths(workspace: Path) -> list[str]:
    """Provider control files in an eval fixture are ambient configuration."""
    controls: list[str] = []
    for path in workspace.rglob("*"):
        if path.name.casefold() in {
                ".gemini", ".agents", ".geminiignore", "gemini.md"}:
            controls.append(str(path.relative_to(workspace)))
    # With --skip-trust, Gemini marks the workspace trusted and its memory
    # discovery walks from cwd through the nearest ancestor containing .git.
    # Scan that exact upward range so a nested fixture cannot inherit a parent
    # GEMINI.md that is outside the model-visible subtree scanned above.
    resolved_workspace = workspace.resolve(strict=False)
    project_root: Path | None = None
    for candidate in (resolved_workspace, *resolved_workspace.parents):
        if (candidate / ".git").exists():
            project_root = candidate
            break
    if project_root is not None:
        current = resolved_workspace
        while True:
            try:
                children = tuple(current.iterdir())
            except OSError:
                children = ()
            for child in children:
                if child.name.casefold() == "gemini.md":
                    controls.append(
                        Path(os.path.relpath(child, resolved_workspace)).as_posix())
            if current == project_root:
                break
            current = current.parent
    return sorted(set(controls))


def probe_gemini_cli_version(
    argv: list[str], *, cwd: Path, env: dict[str, str], timeout: int,
) -> dict[str, Any]:
    """Capture the installed CLI version without exposing prompt/policy bytes."""
    prompt_index = next((
        index for index, value in enumerate(argv)
        if value.startswith("--prompt=")), None)
    if prompt_index is None:
        return {"gemini_cli_version_status": "unavailable",
                "gemini_cli_version_error": "command prefix unavailable"}
    command_prefix = argv[:prompt_index]
    outcome = run_argv_capture(ProcessInvocationPlan.from_values(
        [*command_prefix, "--version"],
        cwd=cwd,
        environment=env,
        timeout_s=max(1, min(timeout, 10)),
        input_text="",
    ))
    stdout_utf8_valid = outcome.stdout_utf8_valid
    stderr_utf8_valid = outcome.stderr_utf8_valid
    validity = {
        "gemini_cli_version_stdout_utf8_valid": stdout_utf8_valid,
        "gemini_cli_version_stderr_utf8_valid": stderr_utf8_valid,
    }
    if not stdout_utf8_valid or not stderr_utf8_valid:
        return {
            "gemini_cli_version_status": "unavailable",
            "gemini_cli_version_error": (
                "version probe output is not valid UTF-8"),
            **validity,
        }
    raw = outcome.stdout.strip() or outcome.stderr.strip()
    version = " ".join(raw.splitlines()[:1]).strip()[:200]
    if outcome.returncode == 0 and version:
        return {"gemini_cli_version_status": "reported",
                "gemini_cli_version": version, **validity}
    return {
        "gemini_cli_version_status": "unavailable",
        "gemini_cli_version_error": (
            "version probe timed out" if outcome.timed_out
            else f"version probe exited {outcome.returncode}"),
        **validity,
    }


def _cleanup_gemini_temp(path: Path) -> dict[str, Any]:
    try:
        shutil.rmtree(path)
        return {"status": "removed"}
    except Exception as exc:
        shutil.rmtree(path, ignore_errors=True)
        retained = path.exists()
        code = errno.errorcode.get(
            getattr(exc, "errno", None), type(exc).__name__)
        return {
            "status": "retained" if retained else "removed_after_fallback",
            "warning": (
                "isolated Gemini temporary-home cleanup could not fully remove "
                f"its unique directory ({code}); it will not be reused"
                if retained else
                "isolated Gemini temporary-home cleanup required the final "
                f"fallback ({code})"
            ),
        }


def _gemini_no_process_result(
    error: str, *, model: str | None, environment: Mapping[str, Any],
) -> dict[str, Any]:
    """One closed artifact-ready shape for every pre-spawn Gemini failure."""
    return {
        "answer": "", "stdout": "", "raw_response": "",
        "trace_text": "", "stderr": error,
        "returncode": 127, "timed_out": False, "elapsed_ms": 0,
        "invocation_state": InvocationState.SPAWN_FAILED.value,
        "usage": None, "cost_usd": None, "model": None,
        "trace_utf8_valid": True,
        "protocol_error": error, "provider_error": None,
        "metadata": {
            "requested_model": model,
            "configured_model": None,
            "resolved_model": None,
            "reported_models": [],
        },
        "environment": dict(environment),
    }


def gemini_cli_invoke(
    prompt: str, *, model: str | None = None,
    gemini_cmd: str | None = None, timeout: int = DEFAULT_RUNNER_TIMEOUT_S,
    cwd: str | Path | None = None, output_format: str = "stream-json",
    allow_read_tools: bool = True,
) -> dict[str, Any]:
    """Invoke official Gemini CLI behind isolated config and typed protocols."""
    validated_model: str | None = None
    try:
        prompt = validate_gemini_prompt(prompt)
        if model is not None and (
                not isinstance(model, str) or not model.strip()):
            raise ValueError("Gemini model must be non-empty text or None")
        if model is not None:
            validate_json_text(model, "Gemini model")
            if "\x00" in model:
                raise ValueError("Gemini model cannot contain NUL")
            validated_model = model
        if gemini_cmd is not None:
            if not isinstance(gemini_cmd, str):
                raise TypeError("--gemini-cmd must be one executable string")
            validate_json_text(gemini_cmd, "Gemini executable")
            if "\x00" in gemini_cmd:
                raise ValueError("--gemini-cmd executable cannot contain NUL")
        if output_format not in {"json", "stream-json"}:
            raise ValueError(
                f"unsupported Gemini output format {output_format!r}")
        if isinstance(timeout, bool) or not isinstance(timeout, int) or timeout <= 0:
            raise ValueError("Gemini timeout must be a positive integer")
        if not isinstance(allow_read_tools, bool):
            raise TypeError("Gemini allow_read_tools must be a boolean")
        if cwd is None:
            invocation_root = Path(tempfile.mkdtemp(prefix="gemini-invoke-"))
            workspace = invocation_root / "workspace"
            lexical_workspace_root = Path(os.path.abspath(workspace))
        else:
            requested_workspace = Path(cwd)
            lexical_workspace_root = Path(os.path.abspath(requested_workspace))
            workspace = requested_workspace.resolve(strict=False)
            workspace.mkdir(parents=True, exist_ok=True)
            # An ambient TMPDIR may itself be the eval workspace. An explicit
            # sibling root keeps policy/auth material outside the model workdir.
            invocation_root = Path(tempfile.mkdtemp(
                prefix="gemini-invoke-", dir=workspace.parent))
    except (OSError, TypeError, ValueError) as exc:
        error = (
            f"Gemini invocation setup failed before spawn ({type(exc).__name__})")
        return _gemini_no_process_result(
            error, model=validated_model,
            environment={
                "temporary_home_cleanup": {"status": "not_created"},
                "command_boundary": "not-constructed",
                "command_executable_source": "not-constructed",
            })
    cleanup_meta: dict[str, Any]
    result: InvocationResult | None = None
    argv: list[str] = []
    env_meta: dict[str, Any] = {}
    version_meta: dict[str, Any] = {
        "gemini_cli_version_status": "unavailable",
        "gemini_cli_version_error": "version probe not attempted",
    }
    sandbox_plan: dict[str, Any] = {
        "requested": False, "engine": "not-evaluated",
        "credential_transport": "not-applicable"}
    home_outside_workdir = not _path_is_within(
        invocation_root / "home", workspace)
    preflight_error: str | None = (
        None if home_outside_workdir else
        "isolated Gemini home must be outside the model-readable workspace")
    setup_phase = "workspace validation"
    try:
        workspace.mkdir(parents=True, exist_ok=True)
        controls = (gemini_workspace_control_paths(workspace)
                    if preflight_error is None else [])
        if controls:
            preflight_error = (
                "Gemini workspace contains provider control files: "
                + ", ".join(controls[:10]))
        elif preflight_error is None:
            adc_paths = gemini_workspace_adc_path(os.environ, workspace)
            workspace_root = workspace.resolve(strict=False)
            if (adc_paths is not None and any(
                    path == root or root in path.parents
                    for path, root in zip(
                        adc_paths,
                        (lexical_workspace_root, workspace_root), strict=True))):
                preflight_error = (
                    "GOOGLE_APPLICATION_CREDENTIALS must be outside the "
                    "model-readable Gemini workspace")
            setup_phase = "isolated authentication setup"
            env, env_meta = gemini_env_for_home(
                invocation_root / "home", workspace=workspace)
            sandbox_plan = gemini_sandbox_plan(env, env_meta)
            auth_preflight_error = env_meta.get("gemini_auth_preflight_error")
            if (preflight_error is None
                    and isinstance(auth_preflight_error, str)):
                preflight_error = auth_preflight_error
            if preflight_error is None:
                setup_phase = "isolated policy setup"
                # Gemini's container sandbox mounts GEMINI_CLI_HOME, but not
                # an arbitrary sibling path. Keep policy outside the workspace
                # and inside that provider-owned mount.
                policy_path = invocation_root / "home" / "harness-policy.toml"
                policy_path.parent.mkdir(parents=True, exist_ok=True)
                policy_path.write_text(
                    gemini_policy_text(allow_read_tools=allow_read_tools),
                    encoding="utf-8",
                )
                try:
                    argv = build_gemini_cli_argv(
                        gemini_cmd, prompt=prompt, output_format=output_format,
                        policy_path=policy_path, model=model,
                        request_sandbox=bool(sandbox_plan["requested"]),
                    )
                except (TypeError, ValueError) as exc:
                    preflight_error = str(exc)
            if preflight_error is None:
                setup_phase = "provider invocation"
                version_meta = probe_gemini_cli_version(
                    argv, cwd=workspace, env=env, timeout=timeout)
                result = run_argv_capture(ProcessInvocationPlan.from_values(
                    argv,
                    input_text="",
                    cwd=workspace,
                    environment=env,
                    timeout_s=timeout,
                ))
    except (OSError, RuntimeError) as exc:
        # Setup failures are closed no-process observations. Do not leak the
        # exception's credential/policy path text into durable artifacts.
        preflight_error = (
            f"Gemini {setup_phase} failed before spawn ({type(exc).__name__})")
    finally:
        cleanup_meta = _cleanup_gemini_temp(invocation_root)

    environment = {
        **env_meta,
        **version_meta,
        **(dict(result.adapter_metadata or {}) if result is not None else {}),
        "gemini_wire_contract_commit": GEMINI_WIRE_CONTRACT_COMMIT,
        "gemini_wire_contract_package_version": (
            GEMINI_WIRE_CONTRACT_PACKAGE_VERSION),
        "tool_policy": (
            "read-only allowlist" if allow_read_tools else "deny all tools"),
        "sandbox_requested": bool(sandbox_plan["requested"]),
        "sandbox_engine": sandbox_plan["engine"],
        "sandbox_credential_transport": sandbox_plan["credential_transport"],
        **({"sandbox_disabled_reason": sandbox_plan["disabled_reason"]}
           if isinstance(sandbox_plan.get("disabled_reason"), str) else {}),
        "admin_policy_may_override": True,
        "workspace_provider_controls": "rejected",
        "gemini_home_outside_workdir": home_outside_workdir,
        "temporary_home_cleanup": cleanup_meta,
        "command": (
            " ".join(shlex.quote(arg) for arg in redact_gemini_argv(argv))
            if argv else GEMINI_DEFAULT_CMD),
        "cwd": "<isolated workspace>",
        "command_boundary": "direct-executable" if argv else "not-constructed",
        "command_executable_source": (
            "default-unverified" if argv and gemini_cmd is None
            else "caller-supplied-unverified" if argv
            else "not-constructed"),
    }
    cleanup_warning = cleanup_meta.get("warning")
    if preflight_error is not None:
        return _gemini_no_process_result(
            preflight_error, model=model, environment=environment)
    assert result is not None
    stderr = result.stderr
    if isinstance(cleanup_warning, str) and cleanup_warning:
        stderr = _stderr_with_warning(stderr, cleanup_warning)
    if output_format == "stream-json":
        parsed_stream = GeminiStream.parse(result.stdout)
        answer = parsed_stream.answer
        usage = (dict(parsed_stream.usage)
                 if parsed_stream.usage is not None else None)
        protocol_error = parsed_stream.protocol_error
        provider_error = parsed_stream.provider_error
        # Multiple reported models are a known ambiguity, not permission to
        # relabel the run with the requested/configured model.
        resolved_model = parsed_stream.resolved_model
        metadata = {
            "session_id": parsed_stream.session_id,
            "requested_model": model,
            "configured_model": parsed_stream.configured_model,
            "resolved_model": resolved_model,
            "reported_models": list(parsed_stream.models),
            "provider_warnings": list(parsed_stream.warnings),
            # The lifecycle count is the judge-safe evidence. Gemini's own
            # conformance suite proves its aggregate counter is independent.
            "provider_tool_calls": len(parsed_stream.tool_calls),
            "provider_reported_tool_calls": parsed_stream.reported_tool_calls,
            "provider_tool_call_source": "stream-lifecycle",
            **({"provider_duration_ms": parsed_stream.duration_ms}
               if parsed_stream.duration_ms is not None else {}),
        }
        trace_text = result.stdout
    else:
        parsed_json = GeminiJsonResponse.parse(result.stdout)
        answer = parsed_json.response
        usage = (dict(parsed_json.usage)
                 if parsed_json.usage is not None else None)
        protocol_error = parsed_json.protocol_error
        provider_error = parsed_json.provider_error
        resolved_model = parsed_json.resolved_model
        metadata = {
            "session_id": parsed_json.session_id,
            "requested_model": model,
            "configured_model": None,
            "resolved_model": resolved_model,
            "reported_models": list(parsed_json.models),
            "provider_tool_calls": parsed_json.tool_calls,
            "provider_warnings": list(parsed_json.warnings),
        }
        trace_text = ""
    if (result.adapter_metadata or {}).get("stdout_utf8_valid") is False:
        answer = ""
        usage = None
        resolved_model = None
        protocol_error = "Gemini stdout is not valid UTF-8"
        metadata["resolved_model"] = None
    return {
        "answer": answer,
        "stdout": result.stdout,
        "raw_response": result.stdout,
        "trace_text": trace_text,
        "stderr": stderr[:4000],
        "returncode": result.returncode,
        "timed_out": result.timed_out,
        "invocation_state": result.invocation_state.value,
        "elapsed_ms": result.elapsed_ms,
        "usage": usage,
        "cost_usd": None,
        "model": resolved_model,
        "trace_utf8_valid": (
            (result.adapter_metadata or {}).get("stdout_utf8_valid") is not False),
        "protocol_error": protocol_error,
        "provider_error": provider_error,
        "metadata": metadata,
        "environment": environment,
    }


def seed_vibe_home(vibe_home: Path) -> dict[str, Any]:
    """Create an isolated VIBE_HOME for native Vibe runs.

    Vibe discovers user skills/tools from VIBE_HOME, so every harness run gets a
    fresh home by default. Auth is carried by MISTRAL_API_KEY; if that env var is
    absent but the user has the documented ~/.vibe/.env file, copy only that env
    file and no skills/config/agents, preserving baseline isolation."""
    vibe_home.mkdir(parents=True, exist_ok=True)
    copied_env = False
    if not os.environ.get("MISTRAL_API_KEY"):
        source_home = Path(os.environ.get("VIBE_HOME") or (Path.home() / ".vibe"))
        src = source_home / ".env"
        if src.is_file() and src.resolve() != (vibe_home / ".env").resolve():
            shutil.copy2(src, vibe_home / ".env")
            copied_env = True
    return {"vibe_home": str(vibe_home), "vibe_env_file_copied": copied_env}


def vibe_env_for_home(vibe_home: Path, model: str | None = None) -> tuple[dict[str, str], dict[str, Any]]:
    env = os.environ.copy()
    seeded = seed_vibe_home(vibe_home)
    env["VIBE_HOME"] = seeded["vibe_home"]
    if model:
        env["VIBE_ACTIVE_MODEL"] = model
    meta = {
        **seeded,
        "vibe_home": "<isolated VIBE_HOME outside workdir>",
        "vibe_home_outside_workdir": True,
        "config_isolated": True,
        **({"active_model_env": "VIBE_ACTIVE_MODEL"} if model else {}),
    }
    return env, meta


def build_vibe_cli_argv(vibe_cmd: str | None = None, *, prompt: str, cwd: Path | str | None = None,
                        output: str = "streaming", tools: Iterable[str] | None = VIBE_READ_ONLY_TOOLS,
                        auto_approve: bool = True, max_turns: int | None = None,
                        max_price: float | None = None, max_tokens: int | None = None) -> list[str]:
    try:
        argv = shlex.split(vibe_cmd or VIBE_DEFAULT_CMD)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid --vibe-cmd: {exc}") from exc
    if not argv:
        argv = [VIBE_DEFAULT_CMD]
    argv += ["--prompt", prompt, "--output", output]
    if cwd is not None:
        argv += ["--workdir", str(cwd)]
    argv.append("--trust")
    if auto_approve:
        argv.append("--auto-approve")
    for tool in (tools or ()):  # `re:^$` is the explicit no-tools sentinel.
        argv += ["--enabled-tools", str(tool)]
    if max_turns is not None:
        argv += ["--max-turns", str(max_turns)]
    if max_price is not None:
        argv += ["--max-price", str(max_price)]
    if max_tokens is not None:
        argv += ["--max-tokens", str(max_tokens)]
    return argv


def redact_vibe_prompt_arg(argv: list[str]) -> list[str]:
    redacted = list(argv)
    # The owned prompt is appended after the caller-controlled command prefix.
    for idx in range(len(redacted) - 2, -1, -1):
        arg = redacted[idx]
        if arg == "--prompt":
            redacted[idx + 1] = "<prompt>"
            break
    return redacted


def parse_vibe_messages_with_errors(
    stdout: str,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Parse Vibe --output json (one list) or --output streaming (JSONL).

    The Vibe CLI emits LLMMessage dictionaries, not a provider-enforced answer
    schema. The harness therefore treats the final assistant message content as
    the answer/verdict, while preserving all parsed messages as trace JSONL."""
    text = coerce_text(stdout).strip()
    if not text:
        return [], ["Vibe stream is empty"]
    try:
        parsed = strict_json_loads(text)
    except json.JSONDecodeError:
        return parse_trace_jsonl_text(text)
    if isinstance(parsed, list):
        errors = [f"Vibe message {index} is not an object"
                  for index, item in enumerate(parsed, 1)
                  if not isinstance(item, dict)]
        return [item for item in parsed if isinstance(item, dict)], errors
    if isinstance(parsed, dict):
        if isinstance(parsed.get("messages"), list):
            values = parsed["messages"]
            errors = [f"Vibe message {index} is not an object"
                      for index, item in enumerate(values, 1)
                      if not isinstance(item, dict)]
            return [item for item in values if isinstance(item, dict)], errors
        return [parsed], []
    return [], ["Vibe output must be a message object, message list, or JSONL"]


def parse_vibe_messages(stdout: str) -> list[dict[str, Any]]:
    """Compatibility projection; execution boundaries consume parser errors too."""
    messages, _ = parse_vibe_messages_with_errors(stdout)
    return messages


def _vibe_content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict):
                if isinstance(item.get("text"), str):
                    parts.append(item["text"])
                elif isinstance(item.get("content"), str):
                    parts.append(item["content"])
            elif isinstance(item, str):
                parts.append(item)
        return "\n".join(parts)
    return ""


def vibe_final_answer(messages: list[dict[str, Any]]) -> str:
    """Return only a validated assistant message; trace bytes are not answers."""
    for msg in reversed(messages):
        if str(msg.get("role", "")).casefold() != "assistant":
            continue
        text = _vibe_content_text(msg.get("content")).strip()
        if text:
            return text
    return ""


def _walk_dicts(value: Any) -> Iterable[dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk_dicts(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_dicts(child)


def vibe_usage_and_cost(messages: list[dict[str, Any]]) -> tuple[dict[str, Any] | None, float | None]:
    terminal = next((message for message in reversed(messages)
                     if message.get("role") == "assistant"
                     and isinstance(message.get("content"), str)
                     and message["content"].strip()), None)
    if terminal is None:
        return None, None
    candidate = terminal.get("usage") or terminal.get("tokens")
    usage = candidate if (isinstance(candidate, dict)
                          and normalize_usage(candidate).get("source") != "missing") else None
    cost: float | None = None
    for key in ("cost_usd", "total_cost_usd", "total_cost", "cost"):
        value = terminal.get(key)
        normalized = normalize_cost(value)
        if normalized.get("source") != "missing":
            cost = float(normalized["total_cost"])
            break
    return usage, cost


def vibe_trace_text(messages: list[dict[str, Any]], stdout: str) -> str:
    # Raw provider bytes are the audit artifact. Reserializing the clean subset
    # would erase malformed/non-object records and manufacture trace completeness.
    return coerce_text(stdout)


def vibe_skill_tool_evidence(stdout: str, skill_names: list[str]) -> list[str]:
    """Detect only completed, schema-valid Vibe `skill` tool lifecycles."""
    records, errors = parse_trace_jsonl_text(stdout)
    if errors:
        return []
    events, metrics = normalize_trace_records(records, source="vibe")
    if metrics.get("trace_protocol_errors"):
        return []
    names = set(skill_names)
    evidence = []
    for event in events["events"]:
        invoked = str(event.get("input_summary") or "")
        if (event.get("type") == "skill_load" and event_is_completed(event)
                and invoked in names):
            evidence.append(f"Vibe skill tool invoked: {invoked}")
    return evidence[:5]


def vibe_cli_invoke(prompt: str, *, model: str | None = None, vibe_cmd: str | None = None,
                    timeout: int = DEFAULT_RUNNER_TIMEOUT_S, cwd: str | Path | None = None,
                    output: str = "streaming", tools: Iterable[str] | None = VIBE_READ_ONLY_TOOLS,
                    auto_approve: bool = True, max_turns: int | None = None,
                    max_price: float | None = None, max_tokens: int | None = None) -> dict[str, Any]:
    if cwd is None:
        with tempfile.TemporaryDirectory(prefix="vibe-invoke-") as td:
            return vibe_cli_invoke(prompt, model=model, vibe_cmd=vibe_cmd, timeout=timeout, cwd=Path(td),
                                   output=output, tools=tools, auto_approve=auto_approve,
                                   max_turns=max_turns, max_price=max_price, max_tokens=max_tokens)
    workspace = Path(cwd)
    with tempfile.TemporaryDirectory(prefix="vibe-home-") as vibe_home:
        env, env_meta = vibe_env_for_home(Path(vibe_home), model)
        try:
            argv = build_vibe_cli_argv(vibe_cmd, prompt=prompt, cwd=workspace, output=output, tools=tools,
                                       auto_approve=auto_approve, max_turns=max_turns,
                                       max_price=max_price, max_tokens=max_tokens)
        except ValueError as exc:
            return {"answer": "", "stdout": "", "stderr": str(exc), "returncode": 127,
                    "timed_out": False, "elapsed_ms": None, "usage": None, "cost_usd": None,
                    "model": model, "trace_text": "",
                    "invocation_state": InvocationState.SPAWN_FAILED.value,
                    "environment": env_meta}
        result = run_argv_capture(ProcessInvocationPlan.from_values(
            argv,
            input_text="",
            cwd=workspace,
            environment=env,
            timeout_s=timeout,
        ))
    messages, parse_errors = (
        parse_vibe_messages_with_errors(result.stdout)
        if result.stdout_utf8_valid
        else ([], ["Vibe stdout is not valid UTF-8"])
    )
    answer = vibe_final_answer(messages)
    usage, cost = vibe_usage_and_cost(messages) if not parse_errors else (None, None)
    return {
        "answer": answer,
        "provider_error": (
            f"Vibe stream parse error: {parse_errors[0]}" if parse_errors
            else None if answer or result.returncode != 0
            else "Vibe stream has no final assistant message"),
        "stdout": result.stdout,
        "stderr": result.stderr,
        "returncode": result.returncode,
        "timed_out": result.timed_out,
        "invocation_state": result.invocation_state.value,
        "elapsed_ms": result.elapsed_ms,
        "usage": usage,
        "cost_usd": cost,
        "model": model,
        "trace_utf8_valid": result.stdout_utf8_valid,
        "trace_text": vibe_trace_text(messages, result.stdout),
        "environment": {
            **env_meta, **dict(result.adapter_metadata or {}),
            "command": " ".join(shlex.quote(a) for a in redact_vibe_prompt_arg(argv)),
            "cwd": "<isolated workspace>"},
    }


# --------------------------------------------------------------------------- #
# First-class Claude adapter — `claude -p --output-format json`.
#
# The Codex/Jetty/Pi runners each own their provider's wire format; Claude's is
# an envelope `{result, total_cost_usd, usage}`. Parsing it in ONE place lets the
# runner AND the judge capture the same cost/usage fields, and lets those land in
# the run's metrics.json so the benchmark report can total real dollars — the
# thing every other adapter leaves the caller to reconstruct out of band.
# --------------------------------------------------------------------------- #

# The Claude envelope's normalized keys, aliased through the ONE table
# (telemetry_blocks.USAGE_ALIASES).
# (`cache_creation_tokens` is Claude's historical metrics.json field name for
# what USAGE_ALIASES normalizes as cache_write_tokens.)
CLAUDE_USAGE_KEYS = {
    "input_tokens": USAGE_ALIASES["input_tokens"],
    "output_tokens": USAGE_ALIASES["output_tokens"],
    "cache_read_tokens": USAGE_ALIASES["cache_read_tokens"],
    "cache_creation_tokens": USAGE_ALIASES["cache_write_tokens"],
}


def parse_claude_cli_json(stdout: str) -> dict[str, Any]:
    """Parse `claude -p` output in either output format.

    `--output-format json` emits one result envelope; `--output-format
    stream-json` emits a JSONL event stream whose TERMINAL `type:"result"`
    event carries the same envelope fields — so the runner and the judge keep
    one parser for Claude's wire format. A stream that dies before its result
    event, like malformed protocol bytes, remains diagnostics and can never
    become a final answer merely because the subprocess exited zero.
    """
    text = stdout if isinstance(stdout, str) else ""
    env: dict[str, Any] | None = None
    stripped = text.strip()
    try:
        single = strict_json_loads(stripped)
    except json.JSONDecodeError:
        records, errors = parse_trace_jsonl_text(text)
        results = [record for record in records if record.get("type") == "result"]
        if errors:
            return {"answer": "", "raw_response": text, "cost_usd": None,
                    "usage": {}, "parse_error": f"malformed Claude stream: {errors[0]}"}
        if len(results) != 1 or not records or records[-1] is not results[0]:
            return {"answer": "", "raw_response": text, "cost_usd": None,
                    "usage": {}, "parse_error": (
                        "Claude stream must contain exactly one terminal result event")}
        env = results[0]
    else:
        if isinstance(single, dict):
            env = single
    if not isinstance(env, dict) or "result" not in env:
        return {"answer": "", "raw_response": text, "cost_usd": None,
                "usage": {}, "parse_error": "not a claude -p json envelope"}
    if "type" in env and env.get("type") != "result":
        return {"answer": "", "raw_response": text, "cost_usd": None,
                "usage": {}, "parse_error": "Claude envelope type must be 'result'"}
    if "is_error" in env and not isinstance(env.get("is_error"), bool):
        return {"answer": "", "raw_response": text, "cost_usd": None,
                "usage": {}, "parse_error": "Claude is_error must be boolean"}
    api_error_status = env.get("api_error_status")
    if (api_error_status is not None
            and (isinstance(api_error_status, bool)
                 or not isinstance(api_error_status, int)
                 or not 100 <= api_error_status <= 599)):
        return {"answer": "", "raw_response": text, "cost_usd": None,
                "usage": {}, "parse_error": "Claude api_error_status must be an HTTP status integer"}
    raw_usage = env.get("usage") if isinstance(env.get("usage"), dict) else {}
    try:
        normalized_usage = normalize_usage(raw_usage, source="provider_reported")
    except ValueError as exc:
        return {"answer": "", "raw_response": text, "cost_usd": None,
                "usage": {}, "parse_error": f"invalid Claude usage: {exc}"}
    usage = {key: value for key, value in normalized_usage.items() if key != "source"}
    cost = env.get("total_cost_usd")
    normalized_cost = _num(cost)
    result = env.get("result")
    result_error = None if isinstance(result, str) else "claude result must be a string"
    if cost is not None and (normalized_cost is None or normalized_cost < 0):
        result_error = "claude total_cost_usd must be a finite nonnegative number"
    return {
        "answer": result if isinstance(result, str) else "",
        "cost_usd": (normalized_cost
                     if normalized_cost is not None and normalized_cost >= 0
                     else None),
        "usage": usage,
        "parse_error": result_error,
        "is_error": env.get("is_error", False),
        "api_error_status": api_error_status,
    }


def claude_cli_invoke(prompt: str, *, model: str | None = None, claude_bin: str = "claude",
                      timeout: int = DEFAULT_RUNNER_TIMEOUT_S, extra_args: list[str] | None = None, cwd: str | Path | None = None,
                      output_format: str = "json") -> dict[str, Any]:
    """Single owner for invoking Claude via `claude -p`.

    Returns the parsed envelope plus returncode/elapsed_ms/stderr/raw_response.
    `claude_bin` is an executable path (tests inject a stub that emits a canned
    envelope), NOT a shell string — so there is no shell-quoting seam between
    the harness and the model. `output_format` selects `json` (one envelope; the
    judge default) or `stream-json` (the full event stream the answer runner
    keeps as the run's raw trace; `-p` requires `--verbose` with it). If no cwd
    is supplied, run in an empty temporary directory rather than inheriting the
    harness repo cwd."""
    if output_format not in {"json", "stream-json"}:
        raise ValueError(f"unsupported claude output_format {output_format!r}")
    argv = [claude_bin, "-p", "--output-format", output_format]
    if output_format == "stream-json":
        argv.append("--verbose")
    argv.append("--no-session-persistence")
    if model:
        argv += ["--model", model]
    if extra_args:
        argv += list(extra_args)

    def invoke(cwd_path: Path | str) -> InvocationResult:
        return run_argv_capture(ProcessInvocationPlan.from_values(
            argv,
            input_text=prompt,
            cwd=cwd_path,
            timeout_s=timeout,
        ))

    if cwd is None:
        with tempfile.TemporaryDirectory(prefix="claude-invoke-cwd-") as td:
            result = invoke(Path(td))
    else:
        result = invoke(cwd)
    command = " ".join(shlex.quote(a) for a in ["claude", *argv[1:]])
    if result.timed_out:
        return {"answer": "", "cost_usd": None, "usage": {}, "parse_error": None,
                "returncode": 124, "timed_out": True, "elapsed_ms": result.elapsed_ms,
                "stderr": result.stderr, "raw_response": result.stdout, "command": command,
                "invocation_state": result.invocation_state.value,
                "trace_utf8_valid": result.stdout_utf8_valid}
    parsed = (
        parse_claude_cli_json(result.stdout)
        if result.stdout_utf8_valid
        else {"answer": "", "cost_usd": None, "usage": {},
              "parse_error": "Claude stdout is not valid UTF-8"}
    )
    provider_error = (
        "Claude provider error"
        + (f" (HTTP {parsed['api_error_status']})"
           if isinstance(parsed.get("api_error_status"), int) else "")
        if parsed.get("is_error") else None
    )
    parsed.update({
        "returncode": result.returncode,
        "timed_out": False,
        "invocation_state": result.invocation_state.value,
        "provider_error": provider_error,
        "trace_utf8_valid": result.stdout_utf8_valid,
        "elapsed_ms": result.elapsed_ms,
        "stderr": result.stderr,
        # The raw wire bytes always ride along: in stream mode they ARE the
        # run's trace; in envelope mode they preserve the failure diagnostics.
        "raw_response": result.stdout,
        "command": command,
    })
    return parsed


def claude_run_metrics(result: dict[str, Any]) -> dict[str, Any]:
    """The metrics.json body for one Claude run: the token usage, the real dollar
    cost, and timing — the fields the benchmark report aggregates."""
    usage = result.get("usage") or {}
    metrics: dict[str, Any] = {"schema_version": 1, "source": "claude"}
    for k in ("input_tokens", "output_tokens", "total_tokens", "cache_read_tokens", "cache_creation_tokens"):
        if isinstance(usage.get(k), (int, float)):
            metrics[k] = int(usage[k])
    if isinstance(result.get("cost_usd"), (int, float)):
        metrics["cost_usd"] = float(result["cost_usd"])
    if isinstance(result.get("elapsed_ms"), (int, float)):
        metrics["elapsed_ms"] = int(result["elapsed_ms"])
    if result.get("returncode") is not None:
        metrics["returncode"] = result["returncode"]
    return metrics


def codex_structured_output_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Return a Codex/OpenAI structured-output-compatible copy of a verdict schema.

    The harness's canonical verdict schemas allow optional fields. Codex's
    `--output-schema` path is stricter: object schemas must set
    `additionalProperties:false`, and optional properties are safest as required
    nullable fields. Keep the canonical schema for harness validation and prompt
    text; adapt only the provider-facing schema here."""
    converted = copy.deepcopy(schema)

    def nullable(node: dict[str, Any]) -> None:
        t = node.get("type")
        if isinstance(t, str):
            if t != "null":
                node["type"] = [t, "null"]
        elif isinstance(t, list) and "null" not in t:
            node["type"] = [*t, "null"]

    def walk(node: Any) -> None:
        if not isinstance(node, dict):
            return
        if node.get("type") == "object":
            props = node.get("properties") if isinstance(node.get("properties"), dict) else {}
            original_required = set(node.get("required") or [])
            node["additionalProperties"] = False
            node["properties"] = props
            node["required"] = list(props.keys())
            for name, child in props.items():
                if isinstance(child, dict) and name not in original_required:
                    nullable(child)
                walk(child)
        if node.get("type") == "array" and isinstance(node.get("items"), dict):
            walk(node["items"])
        for key in ("anyOf", "oneOf", "allOf"):
            if isinstance(node.get(key), list):
                for child in node[key]:
                    walk(child)

    walk(converted)
    return converted


def codex_cli_invoke(prompt: str, *, model: str | None = None, codex_cmd: str = "codex exec", timeout: int = DEFAULT_RUNNER_TIMEOUT_S,
                      output_schema: dict[str, Any] | None = None, cwd: str | Path | None = None,
                      sandbox: str = "read-only", json_events: bool = True) -> dict[str, Any]:
    """Native Codex invocation for judge-style calls.

    Codex's event stream is useful for telemetry, but the verdict/answer should
    come from `--output-last-message` so callers never parse JSONL as if it were
    the final JSON object. Tests inject a Python command via `codex_cmd`; the
    command only needs to honor `--output-last-message` for native-judge tests."""
    try:
        argv = shlex.split(codex_cmd)
    except (TypeError, ValueError) as exc:
        return {"answer": "", "trace_text": "", "stderr": f"invalid --codex-cmd: {exc}", "returncode": 127,
                "timed_out": False, "elapsed_ms": 0, "usage": {}, "cost_usd": None,
                "model": f"codex/{model}" if model else "codex/default",
                "invocation_state": InvocationState.SPAWN_FAILED.value}
    if not argv:
        argv = ["codex", "exec"]
    if json_events and "--json" not in argv:
        argv.append("--json")
    if model:
        argv += ["--model", model]
    if "--skip-git-repo-check" not in argv:
        argv.append("--skip-git-repo-check")
    if "--ephemeral" not in argv:
        argv.append("--ephemeral")
    if "--ignore-user-config" not in argv:
        argv.append("--ignore-user-config")
    if "--ignore-rules" not in argv:
        argv.append("--ignore-rules")
    if sandbox and "--sandbox" not in argv:
        argv += ["--sandbox", sandbox]
    tmp = Path(tempfile.mkdtemp(prefix="codex-invoke-"))
    cleanup_meta: dict[str, Any]
    try:
        env, env_meta = codex_env_for_home(tmp / "codex-home")
        invoke_cwd = Path(cwd) if cwd is not None else tmp / "cwd"
        invoke_cwd.mkdir(parents=True, exist_ok=True)
        last_message = tmp / "last-message.json"
        if "--output-last-message" in argv:
            idx = argv.index("--output-last-message")
            if idx + 1 < len(argv):
                supplied = Path(argv[idx + 1])
                last_message = supplied if supplied.is_absolute() else invoke_cwd / supplied
        else:
            argv += ["--output-last-message", str(last_message)]
        if output_schema is not None:
            schema_path = tmp / "schema.json"
            write_json(schema_path, codex_structured_output_schema(output_schema))
            if "--output-schema" not in argv:
                argv += ["--output-schema", str(schema_path)]
        if "-" not in argv:
            argv.append("-")
        result = run_argv_capture(ProcessInvocationPlan.from_values(
            argv,
            input_text=prompt,
            cwd=invoke_cwd,
            environment=env,
            timeout_s=timeout,
        ))
        last_message_found = last_message.exists()
        last_message_utf8_valid = True
        if last_message_found:
            try:
                final_text = last_message.read_text(encoding="utf-8", errors="strict")
            except UnicodeDecodeError:
                final_text = None
                last_message_utf8_valid = False
        else:
            final_text = None
    finally:
        try:
            cleanup_meta = cleanup_codex_invoke_temp(tmp)
        except Exception as exc:  # an unexpected cleanup failure cannot replace a captured result
            code = errno.errorcode.get(getattr(exc, "errno", None), type(exc).__name__)
            cleanup_meta = {
                "status": "retained",
                "attempts": 0,
                "retry_count": 0,
                "fallback_attempted": False,
                "warning": f"isolated Codex temporary-home cleanup failed unexpectedly ({code}); its unique directory will not be reused",
            }
    command = " ".join(shlex.quote(a) for a in argv)
    usage: dict[str, Any] = {}
    cost_usd = None
    protocol_error: str | None = (
        "Codex final message is not valid UTF-8"
        if not last_message_utf8_valid else None)
    if result.stdout.strip() and result.stdout_utf8_valid:
        records, parse_errors = parse_trace_jsonl_text(result.stdout)
        trace_protocol_error = (
            _codex_trace_protocol_error(records, None)
            if records and not parse_errors else "invalid Codex JSON stream")
        if records and not parse_errors and trace_protocol_error is None:
            _, metrics = normalize_trace_records(records, source="codex")
            for k in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens", "total_tokens"):
                if isinstance(metrics.get(k), (int, float)):
                    usage[k] = int(metrics[k])
            if isinstance(metrics.get("cost_usd"), (int, float)):
                cost_usd = float(metrics["cost_usd"])
    cleanup_warning = cleanup_meta.get("warning")
    stderr = result.stderr
    if isinstance(cleanup_warning, str) and cleanup_warning:
        stderr = _stderr_with_warning(stderr, cleanup_warning)
    adapter_meta = dict(result.adapter_metadata or {})
    return {
        "answer": final_text,
        "trace_text": result.stdout,
        "stderr": stderr[:4000],
        "returncode": result.returncode,
        "timed_out": result.timed_out,
        "invocation_state": result.invocation_state.value,
        "elapsed_ms": result.elapsed_ms,
        "usage": usage,
        "cost_usd": cost_usd,
        "model": f"codex/{model}" if model else "codex/default",
        "protocol_error": protocol_error,
        "trace_utf8_valid": result.stdout_utf8_valid,
        "environment": {
            **env_meta,
            **adapter_meta,
            "last_message_utf8_valid": last_message_utf8_valid,
            "temporary_home_cleanup": cleanup_meta,
            "command": command,
            "cwd": "<isolated workspace>",
        },
    }
