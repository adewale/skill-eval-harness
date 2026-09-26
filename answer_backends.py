"""Native answer backends and the `run-agent` family of commands.

`AGENT_BACKENDS` and `WORKSPACE_BUILDERS` are compatibility views of the
`agent_capabilities.BACKENDS` registry, materialized at import.
"""
from __future__ import annotations

import argparse
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

from ablation_model import (
    AnswerOutcome,
    PreparedTask,
    RunnerOutcome,
    outcome_context,
    outcome_with_context,
)
from agent_capabilities import (
    CODEX_ANSWER_DEFAULT_CMD,
    GEMINI_DEFAULT_CMD,
    VIBE_DEFAULT_CMD,
    binding_for,
    registry_payload,
    surface_implementations,
    surface_option_values,
    workspace_builder_implementations,
)
from agent_clis import (
    VIBE_READ_ONLY_TOOLS,
    claude_cli_invoke,
    codex_cli_invoke,
    gemini_cli_invoke,
    vibe_cli_invoke,
)
from harness_io import DEFAULT_RUNNER_TIMEOUT_S, die, emit_report, load_jsonl
from invocation_contracts import InvocationRequest
from prepared_tasks import (
    answer_design_identity,
    persist_answer_design,
    prepared_task_model,
)
from run_artifacts import build_task_prompt, safe_child_path, write_runner_outcome

# Compatibility view of the unified backend registry. ONE parameterized
# invariant (tests/test_confidence_floor.py) proves the without_skill baseline
# is skill-free by construction for every registered answer path.
WORKSPACE_BUILDERS: dict[str, Any] = workspace_builder_implementations()


def register_workspace_builder(name: str, builder: Any) -> None:
    WORKSPACE_BUILDERS[name] = builder


def registered_workspace_builder(name: str) -> Any:
    """Resolve a replacement-compatible workspace builder or fail closed."""
    try:
        return WORKSPACE_BUILDERS[name]
    except KeyError:
        die(
            f"answer backend {name!r} has no registered workspace builder; "
            "add a complete agent_capabilities.BACKENDS row")


class AgentBackend:
    name = "agent"

    def invoke_answer(self, request: InvocationRequest, **options: Any) -> AnswerOutcome:
        raise NotImplementedError


class CodexBackend(AgentBackend):
    name = "codex"

    def invoke_answer(self, request: InvocationRequest, **options: Any) -> AnswerOutcome:
        result = codex_cli_invoke(
            request.prompt,
            model=request.model,
            codex_cmd=str(options.get("codex_cmd") or CODEX_ANSWER_DEFAULT_CMD),
            timeout=request.timeout_s,
            cwd=request.workspace,
            output_schema=None,
            sandbox="read-only",
            json_events=True,
        )
        return RunnerOutcome(
            provider="codex", answer=result.get("answer"),
            returncode=result.get("returncode"), timed_out=bool(result.get("timed_out", False)), timeout_s=request.timeout_s,
            invocation_state=result.get("invocation_state"),
            elapsed_ms=result.get("elapsed_ms") if isinstance(result.get("elapsed_ms"), (int, float)) else None, stderr=result.get("stderr", ""),
            error=(result.get("protocol_error")
                   if result.get("returncode") == 0 else None),
            trace_text=result.get("trace_text") or "", model=request.model,
            trace_utf8_valid=(result.get("trace_utf8_valid") is not False),
            environment={"runner": "codex", **dict(result.get("environment") or {})})


class ClaudeBackend(AgentBackend):
    name = "claude"

    def invoke_answer(self, request: InvocationRequest, **options: Any) -> AnswerOutcome:
        # stream-json, not the single envelope: the stream is the run's raw
        # trace, so Claude answer runs carry the same tool-use trajectory
        # evidence the trigger matrix already observes — without it every
        # process assertion on a Claude run fails closed for missing evidence.
        result = claude_cli_invoke(request.prompt, model=request.model, claude_bin=str(options.get("claude_bin") or "claude"),
                                   timeout=request.timeout_s, cwd=str(request.workspace), output_format="stream-json")
        return RunnerOutcome(
            provider="claude", answer=result.get("answer") or "",
            returncode=result.get("returncode"), timed_out=bool(result.get("timed_out", False)),
            invocation_state=result.get("invocation_state"),
            timeout_s=request.timeout_s, elapsed_ms=result.get("elapsed_ms") if isinstance(result.get("elapsed_ms"), (int, float)) else None, stderr=result.get("stderr", ""),
            error=(result.get("provider_error") or result.get("parse_error")),
            trace_text=result.get("raw_response") or "",
            trace_utf8_valid=(result.get("trace_utf8_valid") is not False),
            usage=result.get("usage"), cost_usd=result.get("cost_usd"), model=request.model,
            environment={
                "runner": "claude",
                "command": result.get("command") or "claude -p",
                "cwd": "<isolated workspace>",
                "stdout_utf8_valid": result.get("trace_utf8_valid") is not False,
            })


class GeminiBackend(AgentBackend):
    name = "gemini"

    def invoke_answer(self, request: InvocationRequest, **options: Any) -> AnswerOutcome:
        result = gemini_cli_invoke(
            request.prompt,
            model=request.model,
            gemini_cmd=str(options.get("gemini_cmd") or GEMINI_DEFAULT_CMD),
            timeout=request.timeout_s,
            cwd=request.workspace,
            output_format="stream-json",
            allow_read_tools=True,
        )
        returncode = cast(int, result.get("returncode"))
        provider_error = result.get("provider_error")
        protocol_error = result.get("protocol_error")
        error = (
            provider_error
            if isinstance(provider_error, str) and provider_error
            else protocol_error
            if (returncode == 0 and not result.get("timed_out")
                and isinstance(protocol_error, str) and protocol_error)
            else None
        )
        raw_metadata = result.get("metadata")
        metadata = (dict(raw_metadata)
                    if isinstance(raw_metadata, Mapping) else {})
        return RunnerOutcome(
            provider="gemini",
            answer=result.get("answer") or "",
            returncode=returncode,
            timed_out=bool(result.get("timed_out", False)),
            invocation_state=result.get("invocation_state"),
            timeout_s=request.timeout_s,
            elapsed_ms=(result.get("elapsed_ms")
                        if isinstance(result.get("elapsed_ms"), int) else None),
            stderr=result.get("stderr", "") or "",
            error=error,
            usage=(result.get("usage")
                   if isinstance(result.get("usage"), Mapping) else None),
            cost_usd=None,
            model=(result.get("model")
                   if isinstance(result.get("model"), str) else None),
            trace_text=result.get("trace_text") or "",
            trace_utf8_valid=(result.get("trace_utf8_valid") is not False),
            metadata_extra=metadata,
            environment={
                "runner": "gemini", **dict(result.get("environment") or {}),
            },
        )


class VibeBackend(AgentBackend):
    name = "vibe"

    def invoke_answer(self, request: InvocationRequest, **options: Any) -> AnswerOutcome:
        result = vibe_cli_invoke(
            request.prompt,
            model=request.model,
            vibe_cmd=str(options.get("vibe_cmd") or VIBE_DEFAULT_CMD),
            timeout=request.timeout_s,
            cwd=request.workspace,
            output="streaming",
            tools=VIBE_READ_ONLY_TOOLS,
            auto_approve=True,
        )
        env = dict(result.get("environment") or {})
        return RunnerOutcome(
            provider="vibe", answer=result.get("answer") or "",
            returncode=result.get("returncode"), timed_out=bool(result.get("timed_out", False)),
            invocation_state=result.get("invocation_state"),
            timeout_s=request.timeout_s, elapsed_ms=result.get("elapsed_ms") if isinstance(result.get("elapsed_ms"), (int, float)) else None, stderr=result.get("stderr", ""),
            error=result.get("provider_error"),
            usage=result.get("usage"), cost_usd=result.get("cost_usd"), model=request.model,
            trace_text=result.get("trace_text") or "",
            trace_utf8_valid=(result.get("trace_utf8_valid") is not False),
            environment={"runner": "vibe", **env})


# Backwards-compatible materialized view. Registration lives in BACKENDS; this
# name remains mutable so tests/integrations can replace an existing provider
# implementation temporarily. Adding a provider requires a registry row.
AGENT_BACKENDS: dict[str, AgentBackend] = surface_implementations(
    "answer", instantiate=True)


def registered_agent_backend(name: str) -> AgentBackend:
    """Resolve a replacement-compatible backend without identity drift."""
    try:
        backend = AGENT_BACKENDS[name]
    except KeyError:
        die(f"unknown agent backend {name!r}; expected one of {sorted(AGENT_BACKENDS)}")
    if backend.name != name:
        die(
            f"agent backend {name!r} replacement identifies as {backend.name!r}; "
            "replacement names must match their registry row")
    return backend


def run_agent_tasks(tasks: list[dict[str, Any]], runs: Path, backend: AgentBackend, *, model: str | None = None, timeout: int = DEFAULT_RUNNER_TIMEOUT_S, **options: Any) -> int:
    """Shared answer-runner loop for native CLI backends.

    Existing `run-claude` and `run-codex` now use this path, and the new
    `run-agent` command exposes it directly. Provider-specific code returns a
    RunnerOutcome; this loop owns PreparedTask handling, workspace construction,
    provenance, and the run-output contract."""
    workspace_builder = registered_workspace_builder(backend.name)
    validated: list[tuple[dict[str, Any], PreparedTask, str | None, Path]] = []
    seen_identities: set[tuple[str, str | None, str, int, str]] = set()
    seen_destinations: set[Path] = set()
    for task in tasks:
        try:
            pt = PreparedTask.from_row(task)
        except (TypeError, ValueError) as exc:
            die(f"invalid prepared task: {exc}")
        try:
            row_model = prepared_task_model(task, model)
        except ValueError as exc:
            die(f"invalid prepared task: {exc}")
        if task.get("turns"):
            die(f"{backend.name} backend does not support multi-turn prepared tasks")
        identity = (pt.case_id, row_model, pt.variant_truth, pt.run_number, "answer")
        if identity in seen_identities:
            die(f"duplicate prepared task identity: {identity}")
        seen_identities.add(identity)
        base = safe_child_path(runs, pt.run_dir)
        if base in seen_destinations:
            die(f"duplicate prepared task run_dir: {pt.run_dir}")
        seen_destinations.add(base)
        validated.append((task, pt, row_model, base))
    runs.mkdir(parents=True, exist_ok=True)
    design = persist_answer_design(runs, tasks, default_model=model)
    for task, pt, row_model, base in validated:
        base.mkdir(parents=True, exist_ok=True)
        prov_extra = {
            "population": "answer",
            "case_id": pt.case_id,
            "run_number": pt.run_number,
            "variant": pt.variant_truth,
            "billing_scope": "run",
            "answer_design_sha256": design["design_sha256"],
            "answer_task_sha256": answer_design_identity(
                design, pt, row_model)["task_sha256"],
            "answer_instruction_sha256": answer_design_identity(
                design, pt, row_model)["instruction_sha256"],
            **({"ablation": pt.ablation.as_dict()} if pt.ablation else {}),
        }
        with tempfile.TemporaryDirectory(prefix=f"{backend.name}-ws-") as wd:
            ws = Path(wd)
            workspace = workspace_builder(pt, ws)
            skill_rel, input_rel = workspace
            attestation = workspace.attestation
            if attestation.mounted_skill_tree_hash is not None:
                prov_extra["skill_tree_hash"] = attestation.mounted_skill_tree_hash
            prov_extra["fixture_tree_hash"] = attestation.fixture_tree_hash
            prompt = build_task_prompt(pt, skill_paths=skill_rel, input_files=input_rel)
            outcome = backend.invoke_answer(InvocationRequest.parse(
                prompt=prompt,
                workspace=ws,
                model=row_model,
                timeout_s=timeout,
            ), **options)
        context = outcome_context(outcome)
        env = dict(context.environment or {})
        env.setdefault("runner", backend.name)
        env["variant"] = pt.variant_truth
        outcome = outcome_with_context(
            outcome,
            context.enriched(metadata=prov_extra, environment=env),
        )
        write_runner_outcome(base, outcome)
    return 0


def run_agent(args: argparse.Namespace) -> int:
    agent = getattr(args, "agent", None)
    if not isinstance(agent, str):
        die(f"unknown agent backend {agent!r}; expected one of {sorted(AGENT_BACKENDS)}")
    backend = registered_agent_backend(agent)
    provider_options = binding_for(agent, "answer").option_values(
        surface_option_values(args, "answer"))
    return run_agent_tasks(load_jsonl(Path(args.tasks)), Path(args.runs), backend,
                           model=getattr(args, "model", None), timeout=int(getattr(args, "timeout", DEFAULT_RUNNER_TIMEOUT_S)),
                           **provider_options)


def agent_capabilities_command(args: argparse.Namespace) -> int:
    """List every registered backend and its capability-gated surfaces."""
    emit_report(
        {"schema_version": 1, "backends": registry_payload()},
        getattr(args, "out", None),
    )
    return 0


def run_codex(args: argparse.Namespace) -> int:
    return run_agent_tasks(load_jsonl(Path(args.tasks)), Path(args.runs), registered_agent_backend("codex"),
                           timeout=int(getattr(args, "timeout", DEFAULT_RUNNER_TIMEOUT_S)),
                           codex_cmd=getattr(args, "codex_cmd", None) or CODEX_ANSWER_DEFAULT_CMD)


def run_claude(args: argparse.Namespace) -> int:
    return run_agent_tasks(load_jsonl(Path(args.tasks)), Path(args.runs), registered_agent_backend("claude"),
                           model=getattr(args, "model", None), timeout=int(getattr(args, "timeout", DEFAULT_RUNNER_TIMEOUT_S)),
                           claude_bin=getattr(args, "claude_bin", None) or "claude")
