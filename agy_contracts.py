"""Typed, fail-closed contracts for Google Antigravity (``agy``) CLI output.

``agy``'s ``stream-json`` format is a provider wire protocol, not the harness's
internal evidence model.  This leaf module validates that protocol once and
constructs only complete observations or explicit failures.

STEP 2 STUB — see PLAN.md.  The public surface below is final, but the
internals deliberately reproduce the three defects the earlier ``agy-adapter``
branch shipped (D1, D2, D3), so that the tests written against this module fail
for the reason they are meant to protect against rather than failing to import.
Step 4 replaces the internals; the surface stays.
"""
from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

# ---------------------------------------------------------------------------
# Closed vocabularies, observed across real agy 1.1.8 runs.
# ---------------------------------------------------------------------------

AGY_EVENT_TYPES = frozenset({"init", "step_update", "result"})
AGY_STEP_STATES = frozenset({"ACTIVE", "DONE"})
AGY_STEP_TYPES = frozenset({"tool", "user_input", "agent_response", "checkpoint",
                            "system_message", "unknown"})
AGY_SUCCESS_STATUSES = frozenset({"SUCCESS", "COMPLETED", "OK"})
AGY_DONE_STATE = "DONE"

AGY_SHELL_TOOLS = ("run_command",)

# D1 fix (step 4) splits these two apart.  The stub keeps them merged, exactly
# as the reference branch's AGY_READ_TOOLS did.
AGY_FILE_READ_TOOLS = ("view_file",)
AGY_SEARCH_TOOLS = ("grep_search", "find_by_name", "list_dir", "code_search",
                    "skill_search")

AGY_WRITE_TOOLS = ("write_to_file", "replace_file_content",
                   "multi_replace_file_content", "notebook_edit")


def _legacy_read_tools() -> tuple[str, ...]:
    """The reference branch's single merged read partition — the D1 defect."""
    return AGY_FILE_READ_TOOLS + AGY_SEARCH_TOOLS


# ---------------------------------------------------------------------------
# Telemetry as an explicit three-state value.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AgyUsagePresent:
    """Token counters the provider actually reported for a run that ran."""

    counters: Mapping[str, int]


@dataclass(frozen=True)
class AgyUsageAbsent:
    """No usable token accounting.  Never a zero-valued measurement."""

    reason: str


@dataclass(frozen=True)
class AgyUsageInvalid:
    """Token accounting that contradicts itself."""

    reason: str


AgyUsage = AgyUsagePresent | AgyUsageAbsent | AgyUsageInvalid


# ---------------------------------------------------------------------------
# Model identity kept as three separate facts.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AgyModelIdentity:
    """What was asked for, what the launcher implied, and what agy reported."""

    requested: str | None = None
    configured: str | None = None
    reported: tuple[str, ...] = ()

    @property
    def resolved(self) -> str | None:
        """The single reported model, or None for zero or several."""
        return self.reported[0] if len(self.reported) == 1 else None


# ---------------------------------------------------------------------------
# Tool evidence.  Search is not a read.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AgyFileRead:
    """A completed open of one specific file."""

    path: str


@dataclass(frozen=True)
class AgySearch:
    """A completed discovery operation.  Carries no read attribution."""

    tool: str


@dataclass(frozen=True)
class AgyShellCommand:
    command: str


@dataclass(frozen=True)
class AgyFileWrite:
    path: str


@dataclass(frozen=True)
class AgyGenericCall:
    tool: str


AgyToolEvidence = (
    AgyFileRead | AgySearch | AgyShellCommand | AgyFileWrite | AgyGenericCall
)


# ---------------------------------------------------------------------------
# Skill activation observation.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AgySkillActivated:
    """A completed read of the exact mounted SKILL.md path."""

    path: str


@dataclass(frozen=True)
class AgySkillNotActivated:
    """A complete observation in which the skill was demonstrably not read."""


@dataclass(frozen=True)
class AgySkillObservationUnavailable:
    """Neither activation nor a clean negative could be established."""

    reason: str


AgySkillObservation = (
    AgySkillActivated | AgySkillNotActivated | AgySkillObservationUnavailable
)


# ---------------------------------------------------------------------------
# Stream parsing.
# ---------------------------------------------------------------------------


def _param_path(params: Mapping[str, Any]) -> str:
    for key in ("AbsolutePath", "TargetFile", "Path", "path", "file", "FilePath"):
        value = params.get(key)
        if isinstance(value, str) and value:
            return value
    for key, value in params.items():
        if isinstance(value, str) and value and key.endswith(
                ("Path", "File", "path", "file")):
            return value
    return ""


@dataclass(frozen=True)
class AgyStream:
    """One classified ``agy --output-format stream-json`` observation."""

    records: tuple[Mapping[str, Any], ...] = ()
    conversation_id: str | None = None
    model: AgyModelIdentity = AgyModelIdentity()
    answer: str = ""
    usage: AgyUsage = AgyUsageAbsent("not parsed")
    tools: tuple[AgyToolEvidence, ...] = ()
    provider_error: str | None = None
    protocol_error: str | None = None
    truncated: bool = False

    @property
    def complete(self) -> bool:
        return (self.protocol_error is None
                and self.provider_error is None
                and bool(self.answer.strip()))

    @classmethod
    def parse(cls, raw_text: str, *, returncode: int = 0,
              requested_model: str | None = None) -> AgyStream:
        """STEP 2 STUB: reproduces the reference branch's defective semantics."""
        records: list[Mapping[str, Any]] = []
        truncated = False
        for line in raw_text.splitlines():
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except ValueError:
                truncated = True
                continue
            if isinstance(value, Mapping):
                records.append(value)

        reported: list[str] = []
        answer = ""
        provider_error: str | None = None
        usage_raw: Mapping[str, Any] | None = None
        tools: list[AgyToolEvidence] = []

        for record in records:
            event = record.get("event")
            if event == "init":
                init = record.get("init")
                if isinstance(init, Mapping):
                    name = init.get("model")
                    if isinstance(name, str) and name:
                        reported.append(name)
            elif event == "step_update":
                step = record.get("step_update")
                if not isinstance(step, Mapping):
                    continue
                if step.get("state") != AGY_DONE_STATE:
                    continue
                if step.get("step_type") != "tool":
                    continue
                name = step.get("tool_name")
                info = step.get("tool_info")
                params = (info.get("parameters")
                          if isinstance(info, Mapping) else None)
                params = params if isinstance(params, Mapping) else {}
                if not isinstance(name, str):
                    continue
                # D1: search tools are classified as file reads, and the search
                # path is attached as if it had been opened.
                if name in _legacy_read_tools():
                    tools.append(AgyFileRead(path=_param_path(params)))
                elif name in AGY_WRITE_TOOLS:
                    tools.append(AgyFileWrite(path=_param_path(params)))
                elif name in AGY_SHELL_TOOLS:
                    command = params.get("CommandLine")
                    tools.append(AgyShellCommand(
                        command=command if isinstance(command, str) else ""))
                else:
                    tools.append(AgyGenericCall(tool=name))
            elif event == "result":
                result = record.get("result")
                if not isinstance(result, Mapping):
                    continue
                response = result.get("response")
                if isinstance(response, str):
                    answer = response
                error = result.get("error")
                if isinstance(error, str) and error:
                    provider_error = error
                raw_usage = result.get("usage")
                if isinstance(raw_usage, Mapping):
                    usage_raw = raw_usage

        # D2: an all-zero counter block from a run that never reached a model is
        # propagated as a present, provider-reported measurement.
        usage: AgyUsage = (
            AgyUsagePresent(counters={
                key: value for key, value in usage_raw.items()
                if isinstance(value, int) and not isinstance(value, bool)})
            if usage_raw is not None
            else AgyUsageAbsent("no usage block in result event")
        )

        # D3: the structured provider error is discarded when the process exits
        # nonzero.
        if returncode != 0:
            provider_error = None

        return cls(
            records=tuple(records),
            model=AgyModelIdentity(
                requested=requested_model, reported=tuple(reported)),
            answer=answer,
            usage=usage,
            tools=tuple(tools),
            provider_error=provider_error,
            truncated=truncated,
        )


def observe_skill_activation(stream: AgyStream,
                             skill_path: str) -> AgySkillObservation:
    """STEP 2 STUB: reproduces the reference branch's defective semantics."""
    if stream.truncated:
        return AgySkillObservationUnavailable("truncated stream")
    for evidence in stream.tools:
        # D1: an AgyFileRead here may have come from a search, because the stub
        # classifies search tools as reads.
        if isinstance(evidence, AgyFileRead) and evidence.path == skill_path:
            return AgySkillActivated(path=skill_path)
    return AgySkillNotActivated()
