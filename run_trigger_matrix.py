#!/usr/bin/env python3
"""Measure autonomous skill activation across an agent x model matrix.

Activation is not a property of the skill alone: the same description can load
on every Opus run, half of Sonnet's, and none of Haiku's — and a different
agent harness (Pi, Codex, Jetty) shifts those rates again. So this runner takes
the manifest's trigger cases (real user prompts, positive AND negative), mounts
the skill where the agent discovers skills on its own — never forcing the load —
runs every (agent, model, query) cell `--runs-per-query` times, and reports a
per-cell trigger rate. You tune the skill description against that matrix; see
docs/tuning-skill-activation.md for the loop.

Five adapters ship:

- `claude`  — Claude Code CLI subagents (`claude -p`), defaulting to the
              haiku / sonnet / opus aliases. The skill mounts as a project
              skill; loading is detected from the Skill tool-use event and,
              as a fallback, path evidence of the model reading the mounted
              SKILL.md. It uses an isolated CLAUDE_CONFIG_DIR when auth can be
              copied, otherwise preserves the normal Claude config so
              OAuth/keychain logins still work.
- `codex`   — Codex CLI (`codex exec --json` by default), with skills mounted
              under an isolated external `$CODEX_HOME/skills` and exposed as a
              skills-only read root. It is detected through the shared
              path-evidence detector. Override the command with `--codex-cmd`
              when a local wrapper or a newer CLI surface is needed.
- `vibe`    — Mistral Vibe CLI (`vibe --prompt ...`), with skills mounted under
              workspace `.agents/skills` and `VIBE_HOME` isolated outside the
              model workdir. Native `skill` tool calls are primary evidence.
- `pi`      — the Pi coding agent, same mount/detect approach as
              run_pi_trigger_eval.py (which remains a compatibility wrapper
              for the Pi-only entry point).
- `stub`    — offline and deterministic: "triggers" iff the query shares
              enough words with the mounted description, and emits the same
              stream shape the detector reads. It exists so the whole matrix
              pipeline runs in CI with no model, and so a weakened description
              measurably under-triggers even offline.

To add another agent: subclass AgentAdapter, implement mount() (copy the
canonical tree where that agent discovers skills) and invoke() (run the agent
headless on the raw query, return its JSON event stream), then register it in
ADAPTERS and agent_capabilities.AGENT_CAPABILITIES. detect() only needs
overriding when load evidence is not a file path in the stream.

Every number this emits is a RAW autonomous-trigger measurement (the same
evidence class as run_pi_trigger_eval.py) — a rate to steer description edits,
not a provenance-verified causal comparison.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Iterable

from agent_capabilities import AGENT_CAPABILITIES
from skill_benchmark import (
    VALID_SPLITS,
    AblationError,
    build_canonical_skill_tree,
    canonical_skill_tree_hash,
    detect_trigger,
    frontmatter_value,
    iter_json_objects,
    materialize_trigger_ablation,
    mount_skill_tree,
    repo_root_for_manifest,
    run_argv_with_timeout,
    safe_trace_label,
    stream_usage_and_cost,
    build_vibe_cli_argv,
    codex_env_for_home,
    vibe_env_for_home,
    vibe_skill_tool_evidence,
    VIBE_DEFAULT_CMD,
    VIBE_READ_ONLY_TOOLS,
    write_json,
    write_trace_artifacts,
)
from run_pi_trigger_eval import cases_from_manifest, eval_rows_from_args, load_manifest, pi_argv, skill_name_from_manifest, seed_config_dir, validate_trigger_rows
from ablation_model import TRIGGER_MEASUREMENT_EVIDENCE_CLASS, EvidenceClass, Provenance

STOPWORDS = {"this", "that", "with", "have", "what", "your", "from", "each", "then", "them", "were", "will", "would", "should", "could", "please", "give", "tell"}
DEFAULT_CODEX_CMD = "codex exec --json --sandbox read-only --skip-git-repo-check --ephemeral --ignore-user-config --ignore-rules"
INVOKE_RESULT_KEYS = ("stdout", "stderr", "returncode", "timed_out", "elapsed_ms", "observation_complete")
INVOKE_RESULT_METADATA_KEYS = (
    "config_isolated", "config_isolation_warning",
    "codex_home_files_copied", "codex_home_outside_workdir",
    "vibe_env_file_copied", "vibe_home_outside_workdir",
)
CLAUDE_PORTABLE_AUTH_FILES = (".credentials.json",)
SENSITIVE_WORKSPACE_FILES = (
    ".trigger-config/.credentials.json",
    ".codex/auth.json",
    ".codex/config.toml",
    ".pi-config/auth.json",
    ".pi-config/settings.json",
    ".pi-config/APPEND_SYSTEM.md",
    ".vibe-home/.env",
)
SENSITIVE_ENV_VARS = ("MISTRAL_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "CODEX_ACCESS_TOKEN")


def mounted_skill_names(copied: list[Path]) -> list[str]:
    """The `name:` each mounted SKILL.md declares in frontmatter (falling back
    to its directory name). Claude Code invokes skills by this name, so it is
    the needle for Skill-tool detection. Parsed with the harness's real
    frontmatter parser, not a regex that breaks on quoted/folded values."""
    names: list[str] = []
    for p in copied:
        skill_md = p if p.name == "SKILL.md" else p / "SKILL.md"
        name = skill_md.parent.name
        if skill_md.exists():
            declared = frontmatter_value(skill_md.read_text(encoding="utf-8"), "name")
            if declared:
                name = str(declared)
        names.append(name)
    return names


def require_agent_capabilities(name: str) -> Any:
    try:
        return AGENT_CAPABILITIES[name]
    except KeyError as exc:
        raise SystemExit(f"agent {name!r} is registered in ADAPTERS but missing agent_capabilities.AGENT_CAPABILITIES[{name!r}]") from exc


def validate_invoke_result(agent: str, result: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(result, dict):
        raise TypeError(f"{agent}.invoke must return a dict, got {type(result).__name__}")
    missing = [key for key in INVOKE_RESULT_KEYS if key not in result]
    if missing:
        raise KeyError(f"{agent}.invoke missing required result key(s): {', '.join(missing)}")
    return result


def seed_claude_config_dir(config_dir: Path, source_config: Path | None = None) -> bool:
    """Copy portable Claude CLI auth into an isolated config dir when present."""
    source = Path(source_config or os.environ.get("CLAUDE_CONFIG_DIR", str(Path.home() / ".claude")))
    copied = False
    for name in CLAUDE_PORTABLE_AUTH_FILES:
        src = source / name
        if not src.is_file():
            continue
        config_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, config_dir / name)
        copied = True
    return copied


def reject_duplicates(values: list[Any], label: str) -> None:
    seen: set[Any] = set()
    duplicates: list[str] = []
    for value in values:
        key = value if value is not None else "<default>"
        if key in seen and str(key) not in duplicates:
            duplicates.append(str(key))
        seen.add(key)
    if duplicates:
        raise SystemExit(f"duplicate {label} value(s): {', '.join(duplicates)}; use --runs-per-query for repeated measurements")


def _json_secret_values(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value] if len(value) >= 8 else []
    if isinstance(value, dict):
        out: list[str] = []
        for child in value.values():
            out.extend(_json_secret_values(child))
        return out
    if isinstance(value, list):
        out: list[str] = []
        for child in value:
            out.extend(_json_secret_values(child))
        return out
    return []


def _text_secret_values(text: str) -> list[str]:
    secrets: list[str] = []
    if len(text.strip()) >= 8:
        secrets.append(text)
    try:
        secrets.extend(_json_secret_values(json.loads(text)))
    except json.JSONDecodeError:
        secrets.extend(m.group(1) for m in re.finditer(r"""["']([^"']{8,})["']""", text))
        for line in text.splitlines():
            if "=" not in line or line.lstrip().startswith("#"):
                continue
            _, value = line.split("=", 1)
            value = value.strip().strip('"\'')
            if len(value) >= 8:
                secrets.append(value)
    return secrets


def _secret_values_from_files(paths: Iterable[Path]) -> list[str]:
    secrets: list[str] = []
    for path in paths:
        if not path.is_file():
            continue
        secrets.extend(_text_secret_values(path.read_text(encoding="utf-8", errors="replace")))
    return secrets


def workspace_secret_values(workspace: Path) -> list[str]:
    secrets = _secret_values_from_files(workspace / rel for rel in SENSITIVE_WORKSPACE_FILES)
    # Longest first handles whole-file redaction before nested token values.
    return sorted({s for s in secrets if s}, key=len, reverse=True)


def ambient_secret_values() -> list[str]:
    secrets = [value for name in SENSITIVE_ENV_VARS if len(value := os.environ.get(name, "")) >= 8]
    codex_source = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
    vibe_source = Path(os.environ.get("VIBE_HOME", str(Path.home() / ".vibe")))
    secrets.extend(_secret_values_from_files((codex_source / "auth.json", codex_source / "config.toml", vibe_source / ".env")))
    return sorted({s for s in secrets if s}, key=len, reverse=True)


def redact_sensitive_text(text: str, secrets: list[str]) -> str:
    redacted = text
    for secret in secrets:
        redacted = redacted.replace(secret, "[REDACTED]")
    return redacted


def safe_trace_segment(text: str, fallback: str) -> str:
    label = safe_trace_label(text, fallback).strip(".-")
    return label if label and label not in {".", ".."} else fallback


class AgentAdapter:
    """One agent harness in the matrix. Subclass and register in ADAPTERS.

    mount(tree_dir, workspace)  -> copy the canonical or materialized skill tree
        to wherever THIS agent discovers skills autonomously; return the copied
        SKILL.md (or root dir) paths — they become the detection needles.
    invoke(query, model, workspace, timeout) -> run the agent headless on the
        RAW user query (no skill mention, no forced load) and return
        {stdout, stderr, returncode, timed_out, elapsed_ms,
         observation_complete} where stdout is the agent's JSON event stream.
        observation_complete means the agent got a fair window to load the
        skill; a crash or timeout is a failed run, never a no-trigger pass.
    detect(stdout, skill_names, copied) -> (triggered, evidence). The default
        is the shared path-evidence detector; override only when load evidence
        is not a file path (e.g. Claude Code's Skill tool carries a name).
    """

    name = "base"
    default_models: list[str | None] = [None]

    def mount(self, tree_dir: Path, workspace: Path) -> list[Path]:
        raise NotImplementedError

    def invoke(self, query: str, model: str | None, workspace: Path, timeout: int) -> dict[str, Any]:
        raise NotImplementedError

    def detect(self, stdout: str, skill_names: list[str], copied: list[Path]) -> tuple[bool, list[str]]:
        return detect_trigger(stdout, copied)

    # The shared mount and subprocess conventions (skill_benchmark owns them;
    # the Pi runner uses the very same functions, so adapters cannot drift).
    _mount_tree = staticmethod(mount_skill_tree)
    _run_argv = staticmethod(run_argv_with_timeout)


class ClaudeAdapter(AgentAdapter):
    """Claude Code CLI subagents. One `claude -p` process per run; `--model`
    selects haiku/sonnet/opus (or any full model id)."""

    name = "claude"
    default_models: list[str | None] = ["haiku", "sonnet", "opus"]

    def __init__(self, claude_bin: str = "claude", max_turns: int = 6) -> None:
        self.claude_bin = claude_bin
        self.max_turns = max_turns

    def mount(self, tree_dir: Path, workspace: Path) -> list[Path]:
        # Project skills: Claude Code discovers <cwd>/.claude/skills on its own.
        return self._mount_tree(tree_dir, workspace / ".claude" / "skills")

    def invoke(self, query: str, model: str | None, workspace: Path, timeout: int) -> dict[str, Any]:
        # Use a fresh config dir when auth is portable, so personal config does
        # not bleed into the run. Claude Code's current OAuth/keychain login is
        # not file-seedable; pointing CLAUDE_CONFIG_DIR at an empty directory
        # turns a valid login into "not logged in", so preserve the normal CLI
        # config path in that case.
        config_dir = workspace / ".trigger-config"
        argv = [self.claude_bin, "-p", query, "--output-format", "stream-json", "--verbose",
                "--max-turns", str(self.max_turns),
                "--allowedTools", "Skill", "Read", "Glob", "Grep"]
        if model:
            argv += ["--model", model]
        env = os.environ.copy()
        config_isolated = False
        if os.environ.get("ANTHROPIC_API_KEY") or seed_claude_config_dir(config_dir):
            env["CLAUDE_CONFIG_DIR"] = str(config_dir)
            config_isolated = True
        result = self._run_argv(argv, cwd=workspace, env=env, timeout=timeout)
        result["config_isolated"] = config_isolated
        if not config_isolated:
            result["config_isolation_warning"] = (
                "Claude OAuth/keychain auth was not portable; preserved the normal Claude config, "
                "so personal config may influence this measurement"
            )
        # Hitting --max-turns exits nonzero, but the model HAD its window to
        # load the skill — that is a completed observation, not a broken run.
        if result["returncode"] != 0 and self._result_subtype(result["stdout"]) == "error_max_turns":
            result["observation_complete"] = True
        return result

    @staticmethod
    def _result_subtype(stdout: str) -> str | None:
        for event in iter_json_objects(stdout):
            if isinstance(event, dict) and event.get("type") == "result":
                return event.get("subtype")
        return None

    def detect(self, stdout: str, skill_names: list[str], copied: list[Path]) -> tuple[bool, list[str]]:
        # Primary evidence: the Skill tool invoked with a mounted skill's name.
        # Fallback: the shared path detector (the model Read the mounted files).
        evidence: list[str] = []
        for event in iter_json_objects(stdout):
            if not isinstance(event, dict) or event.get("type") != "assistant":
                continue
            for block in (event.get("message") or {}).get("content") or []:
                if not isinstance(block, dict) or block.get("type") != "tool_use" or block.get("name") != "Skill":
                    continue
                invoked = str((block.get("input") or {}).get("skill") or "")
                if invoked in skill_names:
                    evidence.append(f"Skill tool invoked: {invoked}")
        if evidence:
            return True, evidence[:5]
        return super().detect(stdout, skill_names, copied)


class CodexAdapter(AgentAdapter):
    """Codex CLI trigger adapter. It deliberately runs the raw query through
    `codex exec --json` instead of the answer-run prompt builder: trigger
    measurement is about autonomous discovery, not task scaffolding."""

    name = "codex"
    default_models: list[str | None] = [None]

    def __init__(self, codex_cmd: str = DEFAULT_CODEX_CMD) -> None:
        self.codex_cmd = codex_cmd

    @staticmethod
    def _codex_home(workspace: Path) -> Path:
        return workspace.parent / f"{workspace.name}-codex-home"

    def mount(self, tree_dir: Path, workspace: Path) -> list[Path]:
        # Codex discovers skills from $CODEX_HOME/skills. Keep that home outside
        # the model workspace so copied auth/config files are not in the cwd tree;
        # invoke() grants the skills directory only via --add-dir.
        return self._mount_tree(tree_dir, self._codex_home(workspace) / "skills")

    def invoke(self, query: str, model: str | None, workspace: Path, timeout: int) -> dict[str, Any]:
        argv = shlex.split(self.codex_cmd)
        codex_home = self._codex_home(workspace)
        skills_dir = codex_home / "skills"
        if "--add-dir" not in argv:
            argv += ["--add-dir", str(skills_dir)]
        if model:
            argv += ["--model", model]
        argv.append(query)
        env, meta = codex_env_for_home(codex_home)
        try:
            result = self._run_argv(argv, cwd=workspace, env=env, timeout=timeout)
        finally:
            shutil.rmtree(codex_home, ignore_errors=True)
        result.update({k: v for k, v in meta.items() if k != "codex_home"})
        result["codex_home_outside_workdir"] = True
        return result


class PiAdapter(AgentAdapter):
    """The Pi coding agent, mounted and detected exactly like
    run_pi_trigger_eval.py (which stays as the compatibility entry point for
    people already using `skill-pi-trigger-eval`)."""

    name = "pi"

    def mount(self, tree_dir: Path, workspace: Path) -> list[Path]:
        config_dir = workspace / ".pi-config"
        config_dir.mkdir(parents=True, exist_ok=True)
        seed_config_dir(config_dir)   # auth/settings, never the user's skills
        return self._mount_tree(tree_dir, config_dir / "skills")

    def invoke(self, query: str, model: str | None, workspace: Path, timeout: int) -> dict[str, Any]:
        env = os.environ.copy()
        env["PI_CODING_AGENT_DIR"] = str(workspace / ".pi-config")
        return self._run_argv(pi_argv(query, model), cwd=workspace, env=env, timeout=timeout)


class VibeAdapter(AgentAdapter):
    """Mistral Vibe trigger adapter. Vibe natively discovers Agent Skills from
    project `.agents/skills`, so the trigger matrix can measure real autonomous
    skill loading rather than a forced-load answer prompt."""

    name = "vibe"
    default_models: list[str | None] = [None]

    def __init__(self, vibe_cmd: str = VIBE_DEFAULT_CMD, max_turns: int = 6) -> None:
        self.vibe_cmd = vibe_cmd
        self.max_turns = max_turns

    def mount(self, tree_dir: Path, workspace: Path) -> list[Path]:
        return self._mount_tree(tree_dir, workspace / ".agents" / "skills")

    def invoke(self, query: str, model: str | None, workspace: Path, timeout: int) -> dict[str, Any]:
        with tempfile.TemporaryDirectory(prefix=f"{workspace.name}-vibe-home-") as vibe_home:
            env, env_meta = vibe_env_for_home(Path(vibe_home), model)
            try:
                argv = build_vibe_cli_argv(self.vibe_cmd, prompt=query, cwd=workspace, output="streaming",
                                           tools=VIBE_READ_ONLY_TOOLS, auto_approve=True,
                                           max_turns=self.max_turns)
            except ValueError as exc:
                return {"stdout": "", "stderr": str(exc), "returncode": 127, "timed_out": False,
                        "elapsed_ms": None, "observation_complete": False,
                        "config_isolated": True,
                        "vibe_env_file_copied": env_meta.get("vibe_env_file_copied", False),
                        "vibe_home_outside_workdir": True}
            result = self._run_argv(argv, input_text="", cwd=workspace, env=env, timeout=timeout)
        result["config_isolated"] = True
        result["vibe_env_file_copied"] = bool(env_meta.get("vibe_env_file_copied", False))
        result["vibe_home_outside_workdir"] = True
        return result

    def detect(self, stdout: str, skill_names: list[str], copied: list[Path]) -> tuple[bool, list[str]]:
        evidence = vibe_skill_tool_evidence(stdout, skill_names)
        if evidence:
            return True, evidence
        return super().detect(stdout, skill_names, copied)


class StubAdapter(AgentAdapter):
    """Deterministic in-process 'agent' for offline runs and CI: it reads the
    description of the skill that was ACTUALLY mounted and triggers iff the
    query shares >= 2 content words with it. Like the demo's stub_runner, the
    behavior is genuine — weaken the mounted description and the stub
    measurably under-triggers."""

    name = "stub"

    def mount(self, tree_dir: Path, workspace: Path) -> list[Path]:
        return self._mount_tree(tree_dir, workspace / "skills")

    @staticmethod
    def _content_words(text: str) -> set[str]:
        return {w for w in re.findall(r"[a-z]{4,}", text.lower()) if w not in STOPWORDS}

    def invoke(self, query: str, model: str | None, workspace: Path, timeout: int) -> dict[str, Any]:
        lines: list[str] = []
        for skill_md in sorted((workspace / "skills").glob("*/SKILL.md")):
            description = str(frontmatter_value(skill_md.read_text(encoding="utf-8"), "description") or "")
            if len(self._content_words(query) & self._content_words(description)) >= 2:
                # Same stream shape the real agents emit, so the shared
                # detector — not stub-private logic — decides "triggered".
                lines.append(json.dumps({"type": "assistant", "message": {"content": [
                    {"type": "tool_use", "name": "Read", "input": {"file_path": str(skill_md)}}]}}))
        lines.append(json.dumps({"type": "result", "subtype": "success"}))
        return {"stdout": "\n".join(lines) + "\n", "stderr": "", "returncode": 0, "timed_out": False,
                "elapsed_ms": None, "observation_complete": True}


ADAPTERS: dict[str, type[AgentAdapter]] = {"claude": ClaudeAdapter, "codex": CodexAdapter, "pi": PiAdapter, "vibe": VibeAdapter, "stub": StubAdapter}


def adapter_instance(name: str, *, claude_bin: str = "claude", codex_cmd: str | None = None, vibe_cmd: str | None = None, max_turns: int = 6) -> AgentAdapter:
    adapter_cls = ADAPTERS[name]
    if adapter_cls is ClaudeAdapter:
        return ClaudeAdapter(claude_bin=claude_bin, max_turns=max_turns)
    if adapter_cls is CodexAdapter:
        return CodexAdapter(codex_cmd=codex_cmd or DEFAULT_CODEX_CMD)
    if adapter_cls is VibeAdapter:
        return VibeAdapter(vibe_cmd=vibe_cmd or VIBE_DEFAULT_CMD, max_turns=max_turns)
    return adapter_cls()


def matrix_capabilities() -> dict[str, Any]:
    """Capability rows for exactly the agents accepted by this command."""
    return {name: require_agent_capabilities(name) for name in sorted(ADAPTERS)}


def trigger_tree_for_manifest(repo_root: Path, manifest: dict[str, Any], work_dir: Path, ablation: str | None) -> tuple[Path, str, dict[str, Any] | None]:
    """Build the skill tree every cell will mount. Without --ablation it is the
    canonical tree; with --ablation it is a real materialized trigger-population
    ablation, so Claude/Pi/Codex all measure the same altered bytes."""
    if not ablation:
        tree_dir = Path(build_canonical_skill_tree(repo_root, manifest, work_dir / "canonical"))
        tree_hash = canonical_skill_tree_hash(repo_root, manifest)
        return tree_dir, tree_hash, {"mode": "baseline", "skill_tree_hash": tree_hash}

    try:
        provenance = materialize_trigger_ablation(repo_root, manifest, ablation, work_dir / "materialized" / str(ablation))
    except AblationError as exc:
        raise SystemExit(str(exc)) from exc
    prov = Provenance.from_dict(provenance)
    return Path(provenance["dir"]), prov.identity.canonical, prov.as_dict()


def matrix_failure_row(agent: str, model: str | None, query: str, should_trigger: bool,
                       exc: BaseException, metadata: dict[str, Any] | None = None) -> dict[str, Any]:
    message = f"{type(exc).__name__}: {exc}"
    row: dict[str, Any] = {
        "population": "trigger",
        "agent": agent,
        "model": model,
        "query": query,
        "should_trigger": should_trigger,
        "triggered": False,
        "pass": False,
        "observation_complete": False,
        "returncode": None,
        "timed_out": False,
        "elapsed_ms": None,
        "evidence": [],
        "usage_normalized": {"source": "missing"},
        "cost_normalized": {"source": "missing"},
        "stderr": message[-1000:],
        "error": message,
    }
    if metadata:
        row.update({k: v for k, v in metadata.items() if k not in row})
    return row


def run_cell_query(adapter: AgentAdapter, tree_dir: Path, query: str, should_trigger: bool,
                   model: str | None, timeout: int, trace_dir: Path | None = None,
                   metadata: dict[str, Any] | None = None) -> dict[str, Any]:
    """One run of one query in one (agent, model) cell, in a fresh workspace."""
    secrets: list[str] = []
    with tempfile.TemporaryDirectory(prefix=f"trigger-{adapter.name}-") as td:
        workspace = Path(td)
        copied = adapter.mount(tree_dir, workspace)
        names = mounted_skill_names(copied)
        result = validate_invoke_result(adapter.name, adapter.invoke(query, model, workspace, timeout))
        stdout = str(result["stdout"])
        secrets = workspace_secret_values(workspace) + ambient_secret_values()
        triggered, evidence = adapter.detect(stdout, names, copied)
    redacted_stdout = redact_sensitive_text(stdout, secrets)
    redacted_evidence = [redact_sensitive_text(str(item), secrets) for item in evidence]
    redacted_stderr = redact_sensitive_text(str(result["stderr"] or ""), secrets)
    telemetry_error = None
    try:
        usage, cost = stream_usage_and_cost(stdout)
    except Exception as exc:
        telemetry_error = f"{type(exc).__name__}: {exc}"
        usage, cost = {"source": "missing"}, {"source": "missing"}
    observation_complete = bool(result["observation_complete"])
    row = {
        "population": "trigger",
        "agent": adapter.name,
        "model": model,
        "query": query,
        "should_trigger": should_trigger,
        "triggered": triggered,
        "pass": observation_complete and triggered == should_trigger,
        "observation_complete": observation_complete,
        "returncode": result["returncode"],
        "timed_out": bool(result["timed_out"]),
        "elapsed_ms": result["elapsed_ms"],
        "evidence": redacted_evidence,
        "usage_normalized": usage,
        "cost_normalized": cost,
        "stderr": redacted_stderr[-1000:],
    }
    for key in INVOKE_RESULT_METADATA_KEYS:
        if key in result:
            row[key] = result[key]
    if telemetry_error:
        row["telemetry_error"] = telemetry_error
    if metadata:
        row.update({k: v for k, v in metadata.items() if k not in row})
    if trace_dir is not None:
        row["trace_dir"] = str(trace_dir)
        trace_metadata = {
            "population": "trigger",
            "provider": adapter.name,
            "model": model,
            "returncode": row["returncode"],
            "timed_out": row["timed_out"],
            "observation_complete": observation_complete,
            "triggered": triggered,
            "evidence": redacted_evidence,
            "usage_normalized": usage,
            "cost_normalized": cost,
            **(metadata or {}),
        }
        for key in INVOKE_RESULT_METADATA_KEYS:
            if key in result:
                trace_metadata[key] = result[key]
        try:
            write_trace_artifacts(
                trace_dir,
                redacted_stdout,
                source=adapter.name,
                metadata=trace_metadata,
                extra_metrics={
                    "elapsed_ms": row["elapsed_ms"],
                    "returncode": row["returncode"],
                    "timed_out": row["timed_out"],
                    "skill_invoked": triggered,
                    "skill_invocation_evidence": redacted_evidence,
                },
                environment={"runner": adapter.name, "model": model, "trigger_eval": True},
                write_metadata=True,
            )
        except Exception as exc:
            row["trace_error"] = f"{type(exc).__name__}: {exc}"
    return row


def summarize_matrix(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Fold per-run rows into per-(agent, model) cells with per-query trigger
    rates, split by polarity so an over-trigger cannot hide behind an
    under-trigger in the same aggregate."""
    cells: dict[tuple[str, str | None], list[dict[str, Any]]] = {}
    for r in results:
        cells.setdefault((r["agent"], r["model"]), []).append(r)
    matrix = []
    for (agent, model), rows in sorted(cells.items(), key=lambda kv: (kv[0][0], str(kv[0][1]))):
        queries: dict[tuple[str, bool], list[dict[str, Any]]] = {}
        for r in rows:
            queries.setdefault((r["query"], r["should_trigger"]), []).append(r)
        query_rows = []
        for (query, should), runs in queries.items():
            triggered = sum(1 for r in runs if r["triggered"])
            query_rows.append({
                "query": query, "should_trigger": should, "runs": len(runs),
                "triggered_runs": triggered, "trigger_rate": triggered / len(runs),
                "passed_runs": sum(1 for r in runs if r["pass"]),
            })

        def polarity(should: bool) -> dict[str, Any]:
            pol = [r for r in rows if r["should_trigger"] is should]
            return {"total": len(pol), "passed": sum(1 for r in pol if r["pass"]),
                    "pass_rate": (sum(1 for r in pol if r["pass"]) / len(pol)) if pol else None}
        passed = sum(1 for r in rows if r["pass"])
        matrix.append({
            "agent": agent, "model": model,
            "summary": {"total": len(rows), "passed": passed, "pass_rate": passed / len(rows),
                        "should_trigger": polarity(True), "should_not_trigger": polarity(False),
                        "incomplete_observations": sum(1 for r in rows if not r["observation_complete"])},
            "queries": query_rows,
        })
    return matrix


def print_matrix(matrix: list[dict[str, Any]]) -> None:
    header = f"{'agent':<8} {'model':<10} {'should-fire':>12} {'should-not-fire':>16} {'overall':>9}"
    print(header)
    print("-" * len(header))
    for cell in matrix:
        s = cell["summary"]

        def frac(block: dict[str, Any]) -> str:
            return f"{block['passed']}/{block['total']}" if block["total"] else "-"
        print(f"{cell['agent']:<8} {str(cell['model'] or 'default'):<10} "
              f"{frac(s['should_trigger']):>12} {frac(s['should_not_trigger']):>16} "
              f"{str(s['passed']) + '/' + str(s['total']):>9}")


def run_matrix(manifest_path: Path, rows: list[dict[str, Any]], agents: list[str],
               models: list[str] | None, runs_per_query: int, timeout: int, workers: int,
               claude_bin: str = "claude", codex_cmd: str | None = None,
               vibe_cmd: str | None = None, max_turns: int = 6,
               trace_runs: Path | None = None, ablation: str | None = None) -> dict[str, Any]:
    manifest = load_manifest(manifest_path)
    repo_root = repo_root_for_manifest(manifest_path)
    reject_duplicates(agents, "--agent")
    if models is not None:
        reject_duplicates(models, "--model")
    adapters: list[AgentAdapter] = []
    capability_rows: dict[str, Any] = {}
    for name in agents:
        if name not in ADAPTERS:
            raise SystemExit(f"unknown agent {name!r}; known: {sorted(ADAPTERS)} (subclass AgentAdapter to add one)")
        cap = require_agent_capabilities(name)
        if not cap.autonomous_trigger:
            raise SystemExit(f"agent {name!r} is not registered for autonomous trigger measurement")
        if ablation and not cap.trigger_ablation:
            raise SystemExit(f"agent {name!r} does not support trigger ablations")
        capability_rows[name] = cap
        adapter = adapter_instance(name, claude_bin=claude_bin, codex_cmd=codex_cmd, vibe_cmd=vibe_cmd, max_turns=max_turns)
        if adapter.name != name:
            raise SystemExit(f"ADAPTERS[{name!r}] returned adapter with name {adapter.name!r}; set the adapter's name to {name!r}")
        adapters.append(adapter)
    with tempfile.TemporaryDirectory(prefix="trigger-tree-") as td:
        # One skill tree for the whole matrix: every cell mounts the exact same
        # bytes, and the recorded hash/provenance proves which revision was measured.
        tree_dir, tree_hash, provenance = trigger_tree_for_manifest(repo_root, manifest, Path(td), ablation)
        trace_root = None
        if trace_runs is not None:
            trace_runs.mkdir(parents=True, exist_ok=True)
            trace_root = Path(tempfile.mkdtemp(prefix="matrix-", dir=trace_runs))
        futures, results = [], []
        future_context: dict[Any, tuple[str, str | None, str, bool, dict[str, Any]]] = {}
        with ThreadPoolExecutor(max_workers=workers) as ex:
            for adapter in adapters:
                for model in (models if models is not None else adapter.default_models):
                    for row_index, row in enumerate(rows, 1):
                        query = str(row["query"])
                        for run_number in range(1, runs_per_query + 1):
                            trace_dir = None
                            if trace_root is not None:
                                agent_segment = safe_trace_segment(adapter.name, "agent")
                                model_segment = safe_trace_segment(str(model or "default"), "default")
                                trace_dir = (trace_root / agent_segment / model_segment /
                                             f"query-{row_index:03d}-{safe_trace_label(query, f'query-{row_index}')}" /
                                             f"run-{run_number}")
                            metadata = {
                                "measurement": EvidenceClass.RAW_MEASUREMENT.value,
                                "ablation": ablation,
                                "skill_tree_hash": tree_hash,
                            }
                            should_trigger = row["should_trigger"]
                            future = ex.submit(run_cell_query, adapter, tree_dir,
                                               query, should_trigger,
                                               model, timeout, trace_dir, metadata)
                            futures.append(future)
                            future_context[future] = (adapter.name, model, query, should_trigger, metadata)
            for fut in as_completed(futures):
                try:
                    results.append(fut.result())
                except Exception as exc:
                    agent, model, query, should_trigger, metadata = future_context[fut]
                    results.append(matrix_failure_row(agent, model, query, should_trigger, exc, metadata))
    matrix = summarize_matrix(results)
    passed = sum(1 for r in results if r["pass"])
    return {
        "skill_name": skill_name_from_manifest(manifest),
        "generated_at": int(time.time()),
        # Same caveat as run_pi_trigger_eval.py: single-arm raw measurements —
        # rates that steer description edits, not confirmed causal effects.
        "evidence_class": TRIGGER_MEASUREMENT_EVIDENCE_CLASS,
        "skill_tree_hash": tree_hash,
        "ablation": ablation,
        "provenance": provenance,
        "agents": {name: capability_rows[name].as_dict() for name in sorted(capability_rows)},
        "runs_per_query": runs_per_query,
        "summary": {"total": len(results), "passed": passed,
                    "pass_rate": (passed / len(results)) if results else None},
        "matrix": matrix,
        "results": results,
    }


def build_arg_parser() -> argparse.ArgumentParser:
    """The runner's CLI surface, buildable without parsing (shared-constant
    guards in the tests introspect it, e.g. --split choices == VALID_SPLITS)."""
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("manifest")
    ap.add_argument("--eval-set", help="JSON file with {query, should_trigger} rows; defaults to the manifest's kind:'trigger' cases")
    ap.add_argument("--split", choices=sorted(VALID_SPLITS))
    ap.add_argument("--agent", action="append", choices=sorted(ADAPTERS), help="agent adapter, repeatable (default: claude)")
    ap.add_argument("--model", action="append", help="model for every selected agent, repeatable (default: the adapter's own list; claude = haiku, sonnet, opus)")
    ap.add_argument("--runs-per-query", type=int, default=3, help="repetitions per (agent, model, query); a trigger RATE needs repetition (default 3)")
    ap.add_argument("--timeout", type=int, default=240)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--max-turns", type=int, default=6, help="claude/vibe adapters: turns the model gets to load the skill (its observation window)")
    ap.add_argument("--claude-bin", default="claude")
    ap.add_argument("--codex-cmd", default=DEFAULT_CODEX_CMD,
                    help="codex adapter command prefix; the raw query is appended as the final argv item")
    ap.add_argument("--vibe-cmd", default=VIBE_DEFAULT_CMD,
                    help="vibe adapter command prefix; the raw query is passed as the --prompt argument")
    ap.add_argument("--trace-runs", help="optional directory for per-run trace.jsonl/events.json/metrics.json artifacts for every selected agent")
    ap.add_argument("--ablation", help="materialize this discovery/trigger-population ablation id and trigger-test the altered skill")
    ap.add_argument("--out", required=True)
    return ap


def main() -> int:
    ap = build_arg_parser()
    args = ap.parse_args()

    manifest_path = Path(args.manifest)
    rows = eval_rows_from_args(args, manifest_path)
    if not rows:
        raise SystemExit("no trigger queries: add kind:'trigger' cases to the manifest or pass --eval-set")

    report = run_matrix(manifest_path, rows, agents=args.agent or ["claude"], models=args.model,
                        runs_per_query=args.runs_per_query, timeout=args.timeout, workers=args.workers,
                        claude_bin=args.claude_bin, codex_cmd=args.codex_cmd,
                        vibe_cmd=args.vibe_cmd, max_turns=args.max_turns,
                        trace_runs=Path(args.trace_runs) if args.trace_runs else None,
                        ablation=args.ablation)
    write_json(Path(args.out), report)
    print_matrix(report["matrix"])
    print(f"\nreport: {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
