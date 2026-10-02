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
- `pi`      — the Pi coding agent, with skills under an isolated
              PI_CODING_AGENT_DIR. `skill-pi-trigger-eval` is this matrix
              with the Pi adapter alone.
- `stub`    — offline and deterministic: "triggers" iff the query shares
              enough words with the mounted description, and emits the same
              stream shape the detector reads. It exists so the whole matrix
              pipeline runs in CI with no model, and so a weakened description
              measurably under-triggers even offline.

To add another agent: subclass AgentAdapter, implement mount() (copy the
canonical tree where that agent discovers skills) and invoke() (run the agent
headless on the raw query, return its JSON event stream), then add one row to
agent_capabilities.BACKENDS and its explicit trace-dialect semantics. detect()
only needs overriding when load evidence is not a file path in the stream.

Every number this emits is a RAW autonomous-trigger measurement — a rate to
steer description edits, not a provenance-verified causal comparison.
"""
from __future__ import annotations

import argparse
import hashlib
import inspect
import json
import os
import re
import shlex
import shutil
import sys
import tempfile
import time
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

# Preserve one module identity under the documented direct-script entrypoint;
# lazy trigger bindings resolve the canonical ``run_trigger_matrix`` name.
if __name__ == "__main__":
    sys.modules.setdefault("run_trigger_matrix", sys.modules[__name__])

from ablation_model import TRIGGER_MEASUREMENT_EVIDENCE_CLASS, EvidenceClass, Provenance
from agent_capabilities import (
    AGENT_CAPABILITIES,
    CODEX_TRIGGER_DEFAULT_CMD,
    add_surface_cli_options,
    binding_for,
    surface_implementations,
    surface_option_values,
)
from completion_contracts import StopClass, StopObservation, claude_result_stop
from content_digests import file_sha256
from skill_benchmark import (
    CODEX_HOME_FILES,
    VALID_SPLITS,
    VIBE_DEFAULT_CMD,
    VIBE_READ_ONLY_TOOLS,
    AblationError,
    PiStream,
    ProcessInvocationPlan,
    build_canonical_skill_tree,
    build_vibe_cli_argv,
    canonical_json_sha256,
    canonical_trigger_query,
    codex_env_for_home,
    detect_trigger_detection,
    detect_trigger_records,
    expected_trigger_polarity,
    frontmatter_value,
    invoke_argv_with_timeout,
    is_trigger_case,
    iter_cases,
    iter_json_objects,
    load_manifest_source,
    materialize_trigger_ablation,
    mount_skill_tree,
    normalize_trace_records,
    parse_trace_jsonl_text,
    repo_root_for_manifest,
    safe_trace_label,
    skill_tree_hash,
    stream_usage_and_cost,
    strict_json_loads,
    trace_dialect_for,
    trigger_harness_identity,
    trigger_manifest_identity,
    vibe_env_for_home,
    vibe_final_answer,
    vibe_skill_tool_evidence,
    write_json,
    write_trace_artifacts,
)
from trigger_contracts import (
    InvocationOutcome,
    InvocationState,
    TriggerDetection,
    TriggerEvidence,
    TriggerEvidenceKind,
    TriggerExpectation,
    TriggerObservation,
    TriggerRepetitionIdentity,
    validated_trigger_model,
    validated_trigger_protocol_limits,
)
from trigger_reporting import (
    summarize_trigger_cohort,
    summarize_trigger_matrix,
    trigger_cohort_as_dict,
)

STOPWORDS = {"this", "that", "with", "have", "what", "your", "from", "each", "then", "them", "were", "will", "would", "should", "could", "please", "give", "tell"}
DEFAULT_CODEX_CMD = CODEX_TRIGGER_DEFAULT_CMD
CLAUDE_PORTABLE_AUTH_FILES = (".credentials.json",)
SENSITIVE_WORKSPACE_FILES = (
    ".trigger-config/.credentials.json",
    ".codex/auth.json",
    ".codex/config.toml",
    ".vibe-home/.env",
)
SENSITIVE_ENV_VARS = ("MISTRAL_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "CODEX_ACCESS_TOKEN",
                      "CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_AUTH_TOKEN")


# Trigger rows: the manifest's trigger cases or an --eval-set file, validated
# once for every agent.

def load_manifest(path: Path) -> dict[str, Any]:
    """The harness's manifest loader (JSON or YAML, dataset files resolved,
    clean FAIL on bad input) — never a private json.loads fork that would make
    YAML manifests or dataset_files work in `benchmark` but break here."""
    return load_manifest_source(path)


def skill_name_from_manifest(manifest: dict[str, Any]) -> str:
    return str(manifest.get("skill_name") or "skill-under-test")


def trigger_query_from_case(case: dict[str, Any]) -> str:
    prompt = str(case.get("prompt") or case.get("scenario") or case.get("id"))
    # Shared manifests often store trigger fixtures as a meta-classification prompt:
    # "Trigger decision eval. User prompt: <real prompt>\n\nReturn exactly ...".
    # Autonomous trigger testing must run the real user prompt, not the meta prompt,
    # otherwise skill discovery is being tested on the wrong task.
    match = re.search(r"User prompt:\s*(.*?)(?:\n\s*\n\s*Return exactly|$)", prompt, re.IGNORECASE | re.DOTALL)
    if match:
        return match.group(1).strip()
    return prompt


def cases_from_manifest(manifest: dict[str, Any], split: str | None) -> list[dict[str, Any]]:
    out = []
    # iter_cases (not raw manifest["cases"]) so dataset-templated trigger cases
    # fan out here exactly as they do for validation, audit, and the benchmark.
    for c in iter_cases(manifest, split):
        if is_trigger_case(c):
            prompt = trigger_query_from_case(c)
            # Single shared resolver with the manifest audit (skill_benchmark), so the
            # eval and the audit cannot disagree on a case's expected polarity.
            should = expected_trigger_polarity(c) == "TRIGGER"
            out.append({"query_id": str(c.get("id") or ""),
                        "query": prompt, "should_trigger": should})
    return out


def validate_trigger_rows(rows: Any, source: str) -> list[dict[str, Any]]:
    """Validate the shared trigger-row JSON boundary.

    `should_trigger` must already be a JSON boolean; using Python truthiness here
    would turn strings like "false" into True and invert the measurement."""
    if not isinstance(rows, list):
        raise SystemExit(f"{source}: expected a list of trigger rows or an object with evals/queries")
    out: list[dict[str, Any]] = []
    seen: dict[str, tuple[str, bool]] = {}
    seen_definitions: dict[str, tuple[str, bool]] = {}
    for i, row in enumerate(rows, 1):
        if not isinstance(row, dict):
            raise SystemExit(f"{source}: row {i} must be an object")
        query = row.get("query")
        if not isinstance(query, str) or not query.strip():
            raise SystemExit(f"{source}: row {i} query must be a non-empty string")
        should_trigger = row.get("should_trigger")
        if not isinstance(should_trigger, bool):
            raise SystemExit(f"{source}: row {i} should_trigger must be true or false")
        if ("query_id" in row and "id" in row
                and row.get("query_id") != row.get("id")):
            raise SystemExit(
                f"{source}: row {i} has conflicting query_id and id aliases")
        query_id = row.get("query_id", row.get("id"))
        if query_id is None or query_id == "":
            encoded = json.dumps([query, should_trigger], ensure_ascii=False,
                                 separators=(",", ":")).encode("utf-8")
            query_id = "query-" + hashlib.sha256(encoded).hexdigest()
        if not isinstance(query_id, str) or not query_id.strip():
            raise SystemExit(f"{source}: row {i} query_id must be a non-empty string")
        authored = (query, should_trigger)
        if query_id in seen:
            if seen[query_id] != authored:
                raise SystemExit(
                    f"{source}: duplicate query_id {query_id!r} identifies conflicting queries")
            raise SystemExit(f"{source}: duplicate query_id {query_id!r}")
        inference_query = canonical_trigger_query(query)
        prior = seen_definitions.setdefault(
            inference_query, (query_id, should_trigger))
        if prior != (query_id, should_trigger):
            raise SystemExit(
                f"{source}: canonical query aliases alias the same query and must share one query ID and polarity; "
                f"got {prior!r} and {(query_id, should_trigger)!r}")
        seen[query_id] = authored
        normalized = dict(row)
        normalized.pop("id", None)
        normalized["query_id"] = query_id
        normalized["query"] = query
        normalized["should_trigger"] = should_trigger
        out.append(normalized)
    return out


def eval_rows_from_args(args: Any, manifest_path: Path) -> list[dict[str, Any]]:
    """Resolve the trigger rows for a runner invocation: an explicit --eval-set
    file ({query, should_trigger} rows, bare list or under evals/queries), else
    the manifest's kind:'trigger' cases. Shared with run_trigger_matrix."""
    if args.eval_set:
        rows = strict_json_loads(Path(args.eval_set).read_text(encoding="utf-8"))
        if isinstance(rows, dict):
            aliases = [key for key in ("evals", "queries") if key in rows]
            if len(aliases) != 1:
                raise SystemExit(
                    f"{args.eval_set}: expected exactly one of evals or queries")
            rows = rows[aliases[0]]
        return validate_trigger_rows(rows, str(args.eval_set))
    return validate_trigger_rows(cases_from_manifest(load_manifest(manifest_path), args.split), str(manifest_path))


def mounted_skill_names(copied: list[Path]) -> list[str]:
    """Every name an agent may use to invoke a mounted skill: the `name:` its
    SKILL.md declares (parsed with the harness's real frontmatter parser) and
    the directory it is mounted under. Claude Code 2.1.269 invokes project
    skills by directory name (`Skill` called with `skills_demo_SKILL.md`),
    while older builds and other agents use the declared name, so both are
    load evidence. Each is an exact-match needle for the Claude and Vibe
    skill-tool detectors; a name in prose or another skill firing never
    matches."""
    names: list[str] = []
    for p in copied:
        skill_md = p if p.name == "SKILL.md" else p / "SKILL.md"
        candidates = [skill_md.parent.name]
        if skill_md.exists():
            declared = frontmatter_value(skill_md.read_text(encoding="utf-8"), "name")
            if declared:
                candidates.insert(0, str(declared))
        for name in candidates:
            if name not in names:
                names.append(name)
    return names


def require_agent_capabilities(name: str) -> Any:
    try:
        return AGENT_CAPABILITIES[name]
    except KeyError as exc:
        raise SystemExit(f"agent {name!r} is registered in ADAPTERS but missing agent_capabilities.AGENT_CAPABILITIES[{name!r}]") from exc


def validate_invoke_result(agent: str, result: InvocationOutcome | dict[str, Any]) -> InvocationOutcome:
    """Strict compatibility parser; native adapters already return the typed state."""
    if isinstance(result, InvocationOutcome):
        return result
    return InvocationOutcome.from_legacy_dict(agent, result)


def json_stream_protocol_error(stdout: str, agent: str) -> str | None:
    """Reject malformed/empty event streams as incomplete observations."""
    records, errors = parse_trace_jsonl_text(stdout)
    if errors:
        return f"{agent} JSON stream is malformed: {errors[0]}"
    if not records:
        return f"{agent} JSON stream contains no event objects"
    return None


def codex_stream_protocol_error(stdout: str) -> str | None:
    """Require Codex's semantic turn terminator, not merely parseable JSON."""
    error = json_stream_protocol_error(stdout, "codex")
    if error is not None:
        return error
    records, _ = parse_trace_jsonl_text(stdout)
    terminals = [i for i, record in enumerate(records)
                 if str(record.get("type") or "").casefold() == "turn.completed"]
    if terminals != [len(records) - 1]:
        return "Codex JSON stream must contain exactly one final turn.completed event"
    return None


def vibe_stream_protocol_error(stdout: str) -> str | None:
    """Require a final assistant answer so empty event objects cannot certify absence."""
    error = json_stream_protocol_error(stdout, "vibe")
    if error is not None:
        return error
    records, _ = parse_trace_jsonl_text(stdout)
    terminal_answer = (records[-1].get("role") == "assistant"
                       and isinstance(records[-1].get("content"), str)
                       and bool(records[-1]["content"].strip()))
    if not terminal_answer:
        return "Vibe JSON stream must end with one non-empty assistant response"
    return None


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
        secrets.extend(_json_secret_values(strict_json_loads(text)))
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


def cell_secret_values(adapter: AgentAdapter, workspace: Path) -> list[str]:
    """Secret values in the cell's credential files, read after the run and
    before release(), so a token the agent refreshed during the run counts."""
    secrets = _secret_values_from_files(adapter.secret_files(workspace))
    # Longest first handles whole-file redaction before nested token values.
    return sorted({s for s in secrets if s}, key=len, reverse=True)


def ambient_secret_values() -> list[str]:
    secrets = [value for name in SENSITIVE_ENV_VARS if len(value := os.environ.get(name, "")) >= 8]
    codex_source = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
    vibe_source = Path(os.environ.get("VIBE_HOME", str(Path.home() / ".vibe")))
    secrets.extend(_secret_values_from_files((
        codex_source / "auth.json", codex_source / "config.toml", vibe_source / ".env",
        pi_source_config_dir() / "auth.json")))
    return sorted({s for s in secrets if s}, key=len, reverse=True)


def redact_sensitive_text(text: str, secrets: list[str]) -> str:
    redacted = text
    for secret in secrets:
        redacted = redacted.replace(secret, "[REDACTED]")
    return redacted


def redact_sensitive_value(value: Any, secrets: list[str]) -> Any:
    if isinstance(value, str):
        return redact_sensitive_text(value, secrets)
    if isinstance(value, dict):
        return {
            redact_sensitive_text(str(key), secrets): redact_sensitive_value(child, secrets)
            for key, child in value.items()
        }
    if isinstance(value, list):
        return [redact_sensitive_value(child, secrets) for child in value]
    if isinstance(value, tuple):
        return tuple(redact_sensitive_value(child, secrets) for child in value)
    return value


def redact_invocation(invocation: InvocationOutcome, secrets: list[str]) -> InvocationOutcome:
    """The invocation's wire text and metadata with every secret removed."""
    return invocation.with_wire_text(
        stdout=redact_sensitive_text(invocation.stdout, secrets),
        stderr=redact_sensitive_text(invocation.stderr, secrets),
        provider_error=(redact_sensitive_text(invocation.provider_error, secrets)
                        if invocation.provider_error is not None else None),
    ).with_metadata(redact_sensitive_value(dict(invocation.metadata), secrets))


def redact_detection(detection: TriggerDetection, secrets: list[str]) -> TriggerDetection:
    return TriggerDetection(tuple(
        TriggerEvidence(item.kind, redact_sensitive_text(item.text, secrets))
        for item in detection.evidence
    ))


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

    def invoke(self, query: str, model: str | None, workspace: Path, timeout: int) -> InvocationOutcome:
        raise NotImplementedError

    def release(self, workspace: Path) -> None:
        """Remove anything mount() created outside the workspace. Called once
        the cell ends, including when mounting or invoking failed."""

    def secret_files(self, workspace: Path) -> list[Path]:
        """Files holding the credentials this cell's agent runs with. They are
        scanned for redaction after invoke() and before release()."""
        return [workspace / rel for rel in SENSITIVE_WORKSPACE_FILES]

    def detect(self, invocation: InvocationOutcome, skill_names: list[str], copied: list[Path]) -> TriggerDetection:
        if isinstance(invocation.provider_payload, PiStream):
            return detect_trigger_records(
                invocation.provider_payload.records, copied, source=self.name,
                pi_stream=invocation.provider_payload)
        return detect_trigger_detection(invocation.stdout, copied, source=self.name)

    # The shared mount and subprocess conventions (skill_benchmark owns them;
    # the Pi runner uses the very same functions, so adapters cannot drift).
    _mount_tree = staticmethod(mount_skill_tree)
    _run_argv = staticmethod(invoke_argv_with_timeout)

    def protocol_parameters(self) -> dict[str, Any]:
        """Behavior-affecting adapter inputs, excluding prompt and treatment."""
        try:
            source = inspect.getsource(type(self))
        except (OSError, TypeError):
            source = f"{type(self).__module__}.{type(self).__qualname__}"
        return {
            "adapter": f"{type(self).__module__}.{type(self).__qualname__}",
            "agent": self.name,
            "implementation_sha256": "sha256:" + hashlib.sha256(source.encode("utf-8")).hexdigest(),
            "producer_sha256": "sha256:" + file_sha256(Path(__file__)),
            "trace_dialect": self.name,
            "required_observations": {},
        }


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

    def protocol_parameters(self) -> dict[str, Any]:
        return {
            **super().protocol_parameters(),
            "command": executable_identity(self.claude_bin),
            "max_turns": self.max_turns,
            "allowed_tools": ["Skill", "Read", "Glob", "Grep"],
            "isolation_policy": "isolated config when portable auth exists; otherwise normal config",
            "required_observations": {"config_isolated": True},
        }

    def invoke(self, query: str, model: str | None, workspace: Path, timeout: int) -> InvocationOutcome:
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
        result = validate_invoke_result(
            self.name, self._run_argv(ProcessInvocationPlan.from_values(
                argv, input_text="", cwd=workspace, timeout_s=timeout,
                environment=env))
        )
        metadata: dict[str, Any] = {"config_isolated": config_isolated}
        if not config_isolated:
            metadata["config_isolation_warning"] = (
                "Claude OAuth/keychain auth was not portable; preserved the normal Claude config, "
                "so personal config may influence this measurement"
            )
        result = result.with_metadata(metadata)
        # Hitting --max-turns exits nonzero, but the model HAD its window to
        # load the skill. The only legal non-zero completion uses this explicit transition.
        if (result.state is InvocationState.PROCESS_FAILED
                and self._terminal_stop(result.stdout).stop_class is StopClass.TURN_LIMIT):
            result = result.as_agent_window_complete()
        if result.observation_complete:
            error = json_stream_protocol_error(result.stdout, self.name)
            records, _ = parse_trace_jsonl_text(result.stdout)
            terminal = next((record for record in reversed(records)
                             if record.get("type") == "result"), None)
            if error is None and terminal is None:
                error = "Claude JSON stream has no terminal result event"
            if error is None:
                _, metrics = normalize_trace_records(records, source="claude")
                protocol_errors = metrics.get("trace_protocol_errors")
                if isinstance(protocol_errors, list) and protocol_errors:
                    error = f"Claude JSON stream protocol error: {protocol_errors[0]}"
            if (error is None and isinstance(terminal, dict)
                    and terminal.get("is_error") is True
                    and claude_result_stop(terminal).stop_class is not StopClass.TURN_LIMIT):
                error = "Claude terminal result reports an error"
            result = result.with_provider_error(error)
        return result

    @staticmethod
    def _terminal_stop(stdout: str) -> StopObservation:
        # completion_contracts owns what a Claude result event means; the
        # answer runner reads the same classification.
        terminal = next((event for event in iter_json_objects(stdout)
                         if isinstance(event, dict) and event.get("type") == "result"), None)
        return claude_result_stop(terminal)

    def detect(self, invocation: InvocationOutcome, skill_names: list[str], copied: list[Path]) -> TriggerDetection:
        # Primary evidence: the Skill tool invoked with a mounted skill's name.
        # Fallback: the shared path detector (the model Read the mounted files).
        evidence: list[str] = []
        for event in iter_json_objects(invocation.stdout):
            if not isinstance(event, dict) or event.get("type") != "assistant":
                continue
            for block in (event.get("message") or {}).get("content") or []:
                if not isinstance(block, dict) or block.get("type") != "tool_use" or block.get("name") != "Skill":
                    continue
                invoked = str((block.get("input") or {}).get("skill") or "")
                if invoked in skill_names:
                    evidence.append(f"Skill tool invoked: {invoked}")
        if evidence:
            return TriggerDetection.from_texts(TriggerEvidenceKind.SKILL_TOOL, evidence[:5])
        return super().detect(invocation, skill_names, copied)


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

    def release(self, workspace: Path) -> None:
        shutil.rmtree(self._codex_home(workspace), ignore_errors=True)

    def secret_files(self, workspace: Path) -> list[Path]:
        home = self._codex_home(workspace)
        return [*super().secret_files(workspace), *(home / name for name in CODEX_HOME_FILES)]

    def protocol_parameters(self) -> dict[str, Any]:
        return {
            **super().protocol_parameters(),
            "command": executable_identity(self.codex_cmd),
            "isolation_policy": "external ephemeral CODEX_HOME plus skills-only add-dir",
            "required_observations": {"codex_home_outside_workdir": True},
        }

    def invoke(self, query: str, model: str | None, workspace: Path, timeout: int) -> InvocationOutcome:
        argv = shlex.split(self.codex_cmd)
        codex_home = self._codex_home(workspace)
        skills_dir = codex_home / "skills"
        if "--add-dir" not in argv:
            argv += ["--add-dir", str(skills_dir)]
        if model:
            argv += ["--model", model]
        argv.append(query)
        env, meta = codex_env_for_home(codex_home)
        # The home outlives the run until release(): Codex may have refreshed
        # the token in auth.json, and redaction reads the refreshed value.
        result = validate_invoke_result(
            self.name, self._run_argv(ProcessInvocationPlan.from_values(
                argv, input_text="", cwd=workspace, timeout_s=timeout,
                environment=env))
        )
        if result.observation_complete:
            result = result.with_provider_error(
                codex_stream_protocol_error(result.stdout))
        # Which files were seeded is a record, not an isolation control, so it
        # is kept under a key that is not a (boolean) protocol observation.
        return result.with_metadata(
            {k: v for k, v in meta.items() if k not in {"codex_home", "codex_home_files_copied"}},
            codex_home_files=list(meta.get("codex_home_files_copied") or ()),
            codex_home_outside_workdir=True,
        )


def pi_source_config_dir() -> Path:
    """The user's own Pi config dir, the source of the auth a run copies."""
    return Path(os.environ.get("PI_CODING_AGENT_DIR", str(Path.home() / ".pi" / "agent")))


def seed_config_dir(config_dir: Path) -> None:
    """Copy authentication only; ambient settings/system prompts are behavior."""
    source = pi_source_config_dir()
    for name in ["auth.json"]:
        src = source / name
        if src.exists() and src.is_file():
            shutil.copy2(src, config_dir / name)


def pi_invocation_outcome(run: InvocationOutcome) -> InvocationOutcome:
    """Attach Pi's one parsed provider stream to its classified process state."""
    if not isinstance(run, InvocationOutcome):
        raise TypeError("Pi invocation requires InvocationOutcome")
    stream = PiStream.parse(run.stdout)
    if stream.terminal_error or (run.observation_complete and stream.protocol_error):
        return run.with_provider_error(stream.failure_error, payload=stream)
    return run.with_provider_payload(stream)


def pi_argv(query: str, model: str | None = None) -> list[str]:
    """THE Pi CLI invocation for trigger evals — isolated JSON-stream mode with
    read-only tools. The trigger matrix's Pi adapter uses this same argv, so the
    two runners cannot drift apart on flags."""
    argv = [
        "pi", "--no-session", "--mode", "json", "--no-context-files", "--no-prompt-templates", "--no-extensions",
        "--thinking", "minimal", "--tools", "read,grep,find,ls", "-p", query,
    ]
    if model:
        argv[1:1] = ["--model", model]
    return argv


class PiAdapter(AgentAdapter):
    """The Pi coding agent. Skills mount under an isolated PI_CODING_AGENT_DIR
    seeded with auth only; `skill-pi-trigger-eval` runs this adapter alone.

    Pi runs with read, grep, find and ls, so its home sits beside the workspace
    rather than in it, as Codex's does: the copied auth is not readable from the
    working directory, and the skill is reachable only through Pi's own skill
    discovery, not by listing the working directory."""

    name = "pi"

    @staticmethod
    def _pi_home(workspace: Path) -> Path:
        return workspace.parent / f"{workspace.name}-pi-home"

    def mount(self, tree_dir: Path, workspace: Path) -> list[Path]:
        config_dir = self._pi_home(workspace)
        config_dir.mkdir(parents=True, exist_ok=True)
        seed_config_dir(config_dir)   # auth only, never the user's skills or settings
        return self._mount_tree(tree_dir, config_dir / "skills")

    def release(self, workspace: Path) -> None:
        shutil.rmtree(self._pi_home(workspace), ignore_errors=True)

    def secret_files(self, workspace: Path) -> list[Path]:
        return [*super().secret_files(workspace), self._pi_home(workspace) / "auth.json"]

    def protocol_parameters(self) -> dict[str, Any]:
        return {
            **super().protocol_parameters(),
            "command": executable_identity("pi"),
            "tools": ["read", "grep", "find", "ls"],
            "thinking": "minimal",
            "isolation_policy": "external ephemeral PI_CODING_AGENT_DIR seeded with auth only",
            "required_observations": {"config_isolated": True, "pi_home_outside_workdir": True},
        }

    def invoke(self, query: str, model: str | None, workspace: Path, timeout: int) -> InvocationOutcome:
        env = os.environ.copy()
        env["PI_CODING_AGENT_DIR"] = str(self._pi_home(workspace))
        # The home outlives the run until release(): Pi may have refreshed the
        # OAuth token in auth.json, and redaction reads the refreshed value.
        result = validate_invoke_result(
            self.name,
            self._run_argv(ProcessInvocationPlan.from_values(
                pi_argv(query, model), input_text="", cwd=workspace,
                timeout_s=timeout, environment=env)))
        return pi_invocation_outcome(result).with_metadata(
            config_isolated=True, pi_home_outside_workdir=True)


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

    def protocol_parameters(self) -> dict[str, Any]:
        return {
            **super().protocol_parameters(),
            "command": executable_identity(self.vibe_cmd),
            "max_turns": self.max_turns,
            "tools": list(VIBE_READ_ONLY_TOOLS),
            "isolation_policy": "temporary VIBE_HOME outside model workdir",
            "required_observations": {
                "config_isolated": True, "vibe_home_outside_workdir": True,
            },
        }

    def invoke(self, query: str, model: str | None, workspace: Path, timeout: int) -> InvocationOutcome:
        with tempfile.TemporaryDirectory(prefix=f"{workspace.name}-vibe-home-") as vibe_home:
            env, env_meta = vibe_env_for_home(Path(vibe_home), model)
            try:
                argv = build_vibe_cli_argv(self.vibe_cmd, prompt=query, cwd=workspace, output="streaming",
                                           tools=VIBE_READ_ONLY_TOOLS, auto_approve=True,
                                           max_turns=self.max_turns)
            except ValueError as exc:
                return InvocationOutcome.spawn_failed(
                    stderr=str(exc), elapsed_ms=0,
                ).with_metadata(
                    config_isolated=True,
                    vibe_env_file_copied=bool(env_meta.get("vibe_env_file_copied", False)),
                    vibe_home_outside_workdir=True,
                )
            result = validate_invoke_result(
                self.name,
                self._run_argv(ProcessInvocationPlan.from_values(
                    argv, input_text="", cwd=workspace, timeout_s=timeout,
                    environment=env)),
            )
            if result.observation_complete:
                result = result.with_provider_error(
                    vibe_stream_protocol_error(result.stdout))
        return result.with_metadata(
            config_isolated=True,
            vibe_env_file_copied=bool(env_meta.get("vibe_env_file_copied", False)),
            vibe_home_outside_workdir=True,
        )

    def detect(self, invocation: InvocationOutcome, skill_names: list[str], copied: list[Path]) -> TriggerDetection:
        evidence = vibe_skill_tool_evidence(invocation.stdout, skill_names)
        if evidence:
            return TriggerDetection.from_texts(TriggerEvidenceKind.VIBE_SKILL_TOOL, evidence)
        return super().detect(invocation, skill_names, copied)


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

    def invoke(self, query: str, model: str | None, workspace: Path, timeout: int) -> InvocationOutcome:
        started = time.monotonic()
        lines: list[str] = []
        for skill_md in sorted((workspace / "skills").glob("*/SKILL.md")):
            description = str(frontmatter_value(skill_md.read_text(encoding="utf-8"), "description") or "")
            if len(self._content_words(query) & self._content_words(description)) >= 2:
                # Same stream shape the real agents emit, so the shared
                # detector — not stub-private logic — decides "triggered".
                lines.append(json.dumps({"type": "file_read", "path": str(skill_md),
                                         "status": "completed"}))
        lines.append(json.dumps({"type": "result", "subtype": "success"}))
        return InvocationOutcome.from_process(
            stdout="\n".join(lines) + "\n", stderr="", returncode=0,
            elapsed_ms=int((time.monotonic() - started) * 1000),
        )


# Mutable compatibility view for tests that replace an existing adapter. Adding
# an adapter requires an atomic agent_capabilities.BACKENDS row.
ADAPTERS: dict[str, type[AgentAdapter]] = surface_implementations("trigger")


def executable_identity(command: str) -> dict[str, Any]:
    """Command declaration plus content identity of its resolved executable."""
    try:
        argv = shlex.split(command)
        executable = argv[0]
    except (ValueError, IndexError):
        argv = [command] if command else []
        executable = command
    resolved = shutil.which(executable)
    payload: dict[str, Any] = {"spec": command, "resolved": resolved}
    if resolved:
        path = Path(resolved)
        if path.is_file():
            payload["executable_sha256"] = "sha256:" + file_sha256(path)
    argument_files: dict[str, str] = {}
    for argument in argv[1:]:
        candidate = Path(argument).expanduser()
        if not candidate.is_absolute():
            candidate = Path.cwd() / candidate
        if not candidate.is_file():
            continue
        resolved_argument = candidate.resolve()
        argument_files[str(resolved_argument)] = "sha256:" + file_sha256(resolved_argument)
    if argument_files:
        payload["argument_files"] = argument_files
    interpreter = Path(executable).name.casefold()
    if (interpreter.startswith("python") or interpreter in {"bash", "sh", "zsh", "node", "bun"}):
        if "-m" in argv:
            raise ValueError(
                "module-based wrapper commands are not fingerprintable; use a script path")
        inline = any(flag in argv for flag in ("-c", "-e", "--eval"))
        if len(argv) > 1 and not inline and not argument_files:
            raise ValueError(
                "interpreter wrapper command must name an existing script file")
    return payload


def trigger_protocol(
    adapters: list[AgentAdapter], models: list[str | None] | None, *,
    runs_per_query: int, timeout: int, workers: int,
) -> dict[str, Any]:
    timeout, runs_per_query, workers = validated_trigger_protocol_limits(
        timeout_seconds=timeout, runs_per_query=runs_per_query, workers=workers)
    effective_models: dict[AgentAdapter, list[str | None]] = {}
    for adapter in adapters:
        candidates = models if models is not None else adapter.default_models
        if not isinstance(candidates, list) or not candidates:
            raise ValueError(
                f"models for adapter {adapter.name!r} must be a non-empty list")
        effective_models[adapter] = [
            validated_trigger_model(
                model, f"models[{index}] for adapter {adapter.name!r}")
            for index, model in enumerate(candidates)
        ]
    return {
        "schema_version": 1,
        "producer": "skill-trigger-matrix",
        "harness_identity": trigger_harness_identity(),
        "timeout_seconds": timeout,
        "runs_per_query": runs_per_query,
        "workers": workers,
        "adapters": [
            {**adapter.protocol_parameters(),
             "models": effective_models[adapter]}
            for adapter in adapters
        ],
    }


def adapter_instance(
    name: str, *, claude_bin: str = "claude", codex_cmd: str | None = None,
    vibe_cmd: str | None = None, max_turns: int = 6,
    backend_options: dict[str, Any] | None = None,
) -> AgentAdapter:
    values = {
        "claude_bin": claude_bin,
        "codex_cmd": codex_cmd or DEFAULT_CODEX_CMD,
        "vibe_cmd": vibe_cmd or VIBE_DEFAULT_CMD,
        "max_turns": max_turns,
        **dict(backend_options or {}),
    }
    return ADAPTERS[name](**binding_for(name, "trigger").option_values(values))


def trigger_tree_for_manifest(repo_root: Path, manifest: dict[str, Any], work_dir: Path, ablation: str | None) -> tuple[Path, str, dict[str, Any] | None]:
    """Build the skill tree every cell will mount. Without --ablation it is the
    canonical tree; with --ablation it is a real materialized trigger-population
    ablation, so Claude/Pi/Codex all measure the same altered bytes."""
    if not ablation:
        tree_dir = Path(build_canonical_skill_tree(repo_root, manifest, work_dir / "canonical"))
        tree_hash = skill_tree_hash(tree_dir)
        return tree_dir, tree_hash, {"mode": "baseline", "skill_tree_hash": tree_hash}

    try:
        provenance = materialize_trigger_ablation(repo_root, manifest, ablation, work_dir / "materialized" / str(ablation))
    except AblationError as exc:
        raise SystemExit(str(exc)) from exc
    prov = Provenance.from_dict(provenance)
    return Path(provenance["dir"]), prov.identity.edited, prov.as_dict()


def matrix_failure_observation(
    agent: str,
    model: str | None,
    query: str,
    should_trigger: bool,
    exc: BaseException,
    metadata: dict[str, Any] | None = None,
    identity: TriggerRepetitionIdentity | None = None,
) -> TriggerObservation:
    return TriggerObservation.harness_failure(
        agent=agent,
        model=model,
        query=query,
        expectation=TriggerExpectation.from_bool(should_trigger),
        error=exc,
        metadata=metadata,
        identity=identity,
    )


def observe_cell_query(
    adapter: AgentAdapter,
    tree_dir: Path,
    query: str,
    should_trigger: bool,
    model: str | None,
    timeout: int,
    trace_dir: Path | None = None,
    metadata: dict[str, Any] | None = None,
    identity: TriggerRepetitionIdentity | None = None,
) -> TriggerObservation:
    """Observe one cell without erasing its domain type before aggregation."""
    secrets: list[str] = []
    with tempfile.TemporaryDirectory(prefix=f"trigger-{adapter.name}-") as td:
        workspace = Path(td)
        try:
            copied = adapter.mount(tree_dir, workspace)
            mounted_roots = [path.parent if path.name == "SKILL.md" else path for path in copied]
            mounted_parents = {root.parent.resolve() for root in mounted_roots}
            if not mounted_roots or len(mounted_parents) != 1:
                raise ValueError(f"{adapter.name} mount did not expose one complete skill tree")
            mounted_hash = skill_tree_hash(next(iter(mounted_parents)))
            expected_hash = str((metadata or {}).get("skill_tree_hash") or "")
            if mounted_hash != expected_hash:
                raise ValueError(
                    f"{adapter.name} mounted skill tree hash {mounted_hash} does not match {expected_hash}")
            names = mounted_skill_names(copied)
            invocation = validate_invoke_result(adapter.name, adapter.invoke(query, model, workspace, timeout))
            secrets = cell_secret_values(adapter, workspace) + ambient_secret_values()
            detection = adapter.detect(invocation, names, copied)
        finally:
            adapter.release(workspace)

    redacted_invocation = redact_invocation(invocation, secrets)
    redacted_detection = redact_detection(detection, secrets)

    telemetry_error = None
    if invocation.observation_complete:
        try:
            parsed_pi = invocation.provider_payload if isinstance(invocation.provider_payload, PiStream) else None
            usage, cost = stream_usage_and_cost(
                invocation.stdout, source=adapter.name, pi_stream=parsed_pi,
            )
        except Exception as exc:
            telemetry_error = f"{type(exc).__name__}: {exc}"
            usage, cost = {"source": "missing"}, {"source": "missing"}
        capability = AGENT_CAPABILITIES.get(adapter.name)
        if capability is not None:
            telemetry_contract = capability.telemetry_contract()
            if telemetry_contract["usage"].availability == "not_applicable":
                usage = {"source": "not_applicable"}
            if telemetry_contract["cost"].availability == "not_applicable":
                cost = {"source": "not_applicable"}
    else:
        usage, cost = {"source": "missing"}, {"source": "missing"}

    observation_metadata = redact_sensitive_value(dict(metadata or {}), secrets)
    observation_metadata["protocol_observation"] = {
        key: value for key, value in dict(redacted_invocation.metadata).items()
        if key.endswith(("_isolated", "_outside_workdir", "_copied", "_warning"))
    }
    if telemetry_error:
        observation_metadata["telemetry_error"] = telemetry_error
    if trace_dir is not None:
        observation_metadata["trace_dir"] = str(trace_dir)
    observation = TriggerObservation(
        agent=adapter.name,
        model=model,
        query=query,
        expectation=TriggerExpectation.from_bool(should_trigger),
        invocation=redacted_invocation,
        detection=redacted_detection,
        usage=usage,
        cost=cost,
        metadata=observation_metadata,
        identity=identity,
    )
    row = observation.as_row()

    if trace_dir is not None:
        trace_metadata = {
            "population": "trigger",
            "provider": adapter.name,
            "model": model,
            "query": row["query"],
            "should_trigger": row["should_trigger"],
            "pass": row["pass"],
            "returncode": row["returncode"],
            "timed_out": row["timed_out"],
            "observation_complete": row["observation_complete"],
            "triggered": row["triggered"],
            "evidence": row["evidence"],
            "usage_normalized": usage,
            "cost_normalized": cost,
            **(identity.as_dict() if identity is not None else {}),
            **dict(redacted_invocation.metadata),
            **observation_metadata,
        }
        if redacted_invocation.provider_error is not None:
            trace_metadata["provider_error"] = redacted_invocation.provider_error
        try:
            # Detection and telemetry above share the one provider payload the
            # invocation retained; the artifact reparses only a stream that
            # redaction changed.
            artifact_pi_stream = None
            if isinstance(invocation.provider_payload, PiStream):
                artifact_pi_stream = (
                    invocation.provider_payload
                    if redacted_invocation.stdout == invocation.stdout
                    else PiStream.parse(redacted_invocation.stdout))
            write_trace_artifacts(
                trace_dir,
                redacted_invocation.stdout,
                source=adapter.name,
                metadata=trace_metadata,
                extra_metrics={
                    "elapsed_ms": row["elapsed_ms"],
                    "returncode": row["returncode"],
                    "timed_out": row["timed_out"],
                },
                environment={"runner": adapter.name, "model": model, "trigger_eval": True},
                write_metadata=True,
                pi_stream=artifact_pi_stream,
                process_observation_complete=(
                    redacted_invocation.process_observation_complete),
            )
        except Exception as exc:
            trace_error = f"{type(exc).__name__}: {exc}"
            observation = observation.with_metadata({"trace_error": trace_error})
    return observation


def summarize_matrix(observations: list[TriggerObservation]) -> list[dict[str, Any]]:
    """Serialize the shared typed matrix aggregation at the report boundary."""
    return summarize_trigger_matrix(observations)


def print_matrix(matrix: list[dict[str, Any]]) -> None:
    header = f"{'agent':<8} {'model':<10} {'should-fire':>12} {'should-not-fire':>16} {'overall':>9}"
    print(header)
    print("-" * len(header))
    for cell in matrix:
        s = cell["summary"]

        def frac(block: dict[str, Any]) -> str:
            if block["measurement_status"] == "incomplete":
                return f"INCOMPLETE {block['observed']}/{block['total']}"
            return f"{block['passed']}/{block['total']}" if block["total"] else "-"
        overall = frac(s)
        print(f"{cell['agent']:<8} {cell['model'] or 'default'!s:<10} "
              f"{frac(s['should_trigger']):>12} {frac(s['should_not_trigger']):>16} "
              f"{overall:>9}")


def run_matrix(manifest_path: Path, rows: list[dict[str, Any]], agents: list[str],
               models: list[str | None] | None, runs_per_query: int, timeout: int, workers: int,
               claude_bin: str = "claude", codex_cmd: str | None = None,
               vibe_cmd: str | None = None, max_turns: int = 6,
               backend_options: dict[str, Any] | None = None,
               trace_runs: Path | None = None, ablation: str | None = None) -> dict[str, Any]:
    manifest = load_manifest(manifest_path)
    rows = validate_trigger_rows(rows, "trigger matrix rows")
    if not rows:
        raise SystemExit("no trigger queries")
    if not agents:
        raise SystemExit("select at least one --agent")
    if models is not None and not models:
        raise SystemExit("select at least one --model or omit --model for adapter defaults")
    try:
        timeout, runs_per_query, workers = validated_trigger_protocol_limits(
            timeout_seconds=timeout,
            runs_per_query=runs_per_query,
            workers=workers,
        )
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
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
        try:
            trace_dialect_for(name)
        except ValueError as exc:
            raise SystemExit(f"agent {name!r} has no registered trace dialect") from exc
        if not cap.autonomous_trigger:
            raise SystemExit(f"agent {name!r} is not registered for autonomous trigger measurement")
        if ablation and not cap.trigger_ablation:
            raise SystemExit(f"agent {name!r} does not support trigger ablations")
        capability_rows[name] = cap
        adapter = adapter_instance(
            name, claude_bin=claude_bin, codex_cmd=codex_cmd,
            vibe_cmd=vibe_cmd, max_turns=max_turns,
            backend_options=backend_options,
        )
        if adapter.name != name:
            raise SystemExit(f"ADAPTERS[{name!r}] returned adapter with name {adapter.name!r}; set the adapter's name to {name!r}")
        adapters.append(adapter)
    with tempfile.TemporaryDirectory(prefix="trigger-tree-") as td:
        # One skill tree for the whole matrix: every cell mounts the exact same
        # bytes, and the recorded hash/provenance proves which revision was measured.
        tree_dir, tree_hash, provenance = trigger_tree_for_manifest(repo_root, manifest, Path(td), ablation)
        protocol = trigger_protocol(
            adapters, models, runs_per_query=runs_per_query,
            timeout=timeout, workers=workers)
        protocol_sha256 = canonical_json_sha256(protocol)
        manifest_identity = trigger_manifest_identity(manifest)
        trace_root = None
        if trace_runs is not None:
            trace_runs.mkdir(parents=True, exist_ok=True)
            trace_root = Path(tempfile.mkdtemp(prefix="matrix-", dir=trace_runs))
        futures, observations, design = [], [], []
        future_context: dict[Any, tuple[str, str | None, str, bool, dict[str, Any], TriggerRepetitionIdentity]] = {}
        with ThreadPoolExecutor(max_workers=workers) as ex:
            for adapter in adapters:
                for model in (models if models is not None else adapter.default_models):
                    for row_index, row in enumerate(rows, 1):
                        query = str(row["query"])
                        design.append({
                            "agent": adapter.name, "model": model,
                            "query_id": row["query_id"], "query": query,
                            "should_trigger": row["should_trigger"],
                        })
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
                                "protocol_sha256": protocol_sha256,
                                "protocol_observation": {},
                            }
                            should_trigger = row["should_trigger"]
                            identity = TriggerRepetitionIdentity(row["query_id"], run_number)
                            future = ex.submit(observe_cell_query, adapter, tree_dir,
                                               query, should_trigger,
                                               model, timeout, trace_dir, metadata, identity)
                            futures.append(future)
                            future_context[future] = (
                                adapter.name, model, query, should_trigger, metadata, identity)
            for fut in as_completed(futures):
                try:
                    observations.append(fut.result())
                except Exception as exc:
                    agent, model, query, should_trigger, metadata, identity = future_context[fut]
                    observations.append(matrix_failure_observation(
                        agent, model, query, should_trigger, exc, metadata, identity))
    observations.sort(key=lambda observation: (
        observation.agent, str(observation.model or ""),
        str(observation.identity.query_id if observation.identity else ""),
        int(observation.identity.run_number if observation.identity else 0),
    ))
    matrix = summarize_matrix(observations)
    summary = trigger_cohort_as_dict(summarize_trigger_cohort(observations))
    return {
        "skill_name": skill_name_from_manifest(manifest),
        "generated_at": int(time.time()),
        # Single-arm raw measurements: rates that steer description edits,
        # not confirmed causal effects.
        # Pair a baseline report with an --ablation report through
        # `skill-benchmark trigger-compare` to reach a causal evidence class.
        "evidence_class": TRIGGER_MEASUREMENT_EVIDENCE_CLASS,
        "skill_tree_hash": tree_hash,
        "ablation": ablation,
        "provenance": provenance,
        "manifest_identity": manifest_identity,
        "protocol": protocol,
        "protocol_sha256": protocol_sha256,
        "agents": {name: capability_rows[name].as_dict() for name in sorted(capability_rows)},
        "runs_per_query": runs_per_query,
        "design": design,
        "summary": summary,
        "matrix": matrix,
        "results": [observation.as_row() for observation in observations],
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
    ap.add_argument("--trace-runs", help="optional directory for per-run trace.jsonl/events.json/metrics.json artifacts for every selected agent")
    ap.add_argument("--ablation", help="materialize this discovery/trigger-population ablation id and trigger-test the altered skill")
    ap.add_argument("--out", required=True)
    add_surface_cli_options(ap, "trigger")
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
                        max_turns=args.max_turns,
                        backend_options=surface_option_values(args, "trigger"),
                        trace_runs=Path(args.trace_runs) if args.trace_runs else None,
                        ablation=args.ablation)
    write_json(Path(args.out), report)
    print_matrix(report["matrix"])
    print(f"\nreport: {args.out}")
    return 0 if report["summary"]["measurement_status"] == "complete" else 1


if __name__ == "__main__":
    raise SystemExit(main())
