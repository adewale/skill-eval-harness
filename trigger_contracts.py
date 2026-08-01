"""Typed contracts for autonomous-trigger subprocesses and observations.

Wire formats remain dictionaries at CLI/artifact boundaries. Internally, completion,
provider failure, trigger detection, and pass/fail are derived from closed states so
contradictory booleans cannot be assembled independently.
"""
from __future__ import annotations

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from enum import Enum
from types import MappingProxyType
from typing import Any, TypeAlias

import telemetry as telemetry_domain
from invocation_contracts import InvocationState, validate_invocation_lifecycle
from json_contracts import (
    freeze_json_mapping,
    strict_json_equal,
    validate_json_text,
)


class CompletionEvidence(str, Enum):
    NORMAL_EXIT = "normal_exit"
    AGENT_WINDOW_EXHAUSTED = "agent_window_exhausted"


_INVOCATION_RESERVED_METADATA = {
    "stdout", "stderr", "returncode", "timed_out", "elapsed_ms",
    "observation_complete", "provider_error", "completion_evidence",
    "invocation_state",
}


@dataclass(frozen=True)
class InvocationOutcome:
    """One fully classified subprocess invocation.

    ``observation_complete`` and ``timed_out`` are derived properties, never
    constructor inputs. A non-zero process can be complete only through the
    explicit ``AGENT_WINDOW_EXHAUSTED`` transition used by bounded agents.
    """

    stdout: str
    stderr: str
    returncode: int | None
    elapsed_ms: int | None
    state: InvocationState
    completion_evidence: CompletionEvidence | None = None
    provider_error: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict, compare=False)
    provider_payload: Any = field(default=None, compare=False, repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.stdout, str) or not isinstance(self.stderr, str):
            raise TypeError("invocation stdout and stderr must be strings")
        validate_json_text(self.stdout, "invocation stdout")
        validate_json_text(self.stderr, "invocation stderr")
        if self.elapsed_ms is not None and (
            isinstance(self.elapsed_ms, bool) or not isinstance(self.elapsed_ms, int)
            or self.elapsed_ms < 0 or self.elapsed_ms > 2**63 - 1
        ):
            raise ValueError(
                "invocation elapsed_ms must be a supported non-negative integer or None")
        if self.returncode is not None and (isinstance(self.returncode, bool) or not isinstance(self.returncode, int)):
            raise TypeError("invocation returncode must be an integer or None")
        if not isinstance(self.state, InvocationState):
            raise TypeError("invocation state must be InvocationState")
        if self.completion_evidence is not None and not isinstance(self.completion_evidence, CompletionEvidence):
            raise TypeError("completion_evidence must be CompletionEvidence or None")
        if self.provider_error is not None and (not isinstance(self.provider_error, str) or not self.provider_error.strip()):
            raise ValueError("provider_error must be a non-empty string")
        if self.provider_error is not None:
            validate_json_text(self.provider_error, "invocation provider_error")

        if self.state is InvocationState.COMPLETE:
            if self.returncode == 0:
                if self.completion_evidence not in {None, CompletionEvidence.NORMAL_EXIT}:
                    raise ValueError("zero-exit completion cannot claim an exhausted agent window")
            elif (self.returncode is None
                  or self.completion_evidence is not CompletionEvidence.AGENT_WINDOW_EXHAUSTED):
                raise ValueError("non-zero completion requires a spawned process and explicit agent-window evidence")
        elif self.completion_evidence is not None:
            raise ValueError("only complete invocations can carry completion evidence")

        validate_invocation_lifecycle(
            self.state,
            self.returncode,
            self.provider_error,
            allow_harness_failure=True,
            allow_nonzero_completion=(
                self.completion_evidence is CompletionEvidence.AGENT_WINDOW_EXHAUSTED),
        )
        if self.state is InvocationState.HARNESS_FAILED:
            if self.returncode is not None or self.elapsed_ms is not None:
                raise ValueError("harness failure has no process returncode or measured elapsed time")
        elif self.returncode is None or self.elapsed_ms is None:
            raise ValueError("process invocation states require returncode and elapsed_ms")

        metadata = freeze_json_mapping(self.metadata, "invocation metadata")
        collisions = sorted(_INVOCATION_RESERVED_METADATA & set(metadata))
        if collisions:
            raise ValueError(f"invocation metadata collides with derived field(s): {', '.join(collisions)}")
        object.__setattr__(self, "metadata", metadata)

    @property
    def observation_complete(self) -> bool:
        return self.state is InvocationState.COMPLETE

    @property
    def timed_out(self) -> bool:
        return self.state is InvocationState.TIMED_OUT

    @property
    def process_observation_complete(self) -> bool:
        """Whether a provider process was spawned and reached an exit."""
        return self.state in {
            InvocationState.COMPLETE,
            InvocationState.PROCESS_FAILED,
            InvocationState.PROVIDER_FAILED,
        }

    @classmethod
    def from_process(cls, *, stdout: str, stderr: str, returncode: int,
                     elapsed_ms: int,
                     metadata: Mapping[str, Any] | None = None) -> InvocationOutcome:
        if returncode == 0:
            state = InvocationState.COMPLETE
            evidence = CompletionEvidence.NORMAL_EXIT
        else:
            state = InvocationState.PROCESS_FAILED
            evidence = None
        return cls(stdout, stderr, returncode, elapsed_ms, state, evidence,
                   metadata={} if metadata is None else metadata)

    @classmethod
    def from_timeout(cls, *, stdout: str, stderr: str, elapsed_ms: int,
                     metadata: Mapping[str, Any] | None = None) -> InvocationOutcome:
        return cls(
            stdout, stderr, 124, elapsed_ms, InvocationState.TIMED_OUT,
            metadata={} if metadata is None else metadata,
        )

    @classmethod
    def spawn_failed(cls, *, stderr: str, elapsed_ms: int,
                     stdout: str = "",
                     metadata: Mapping[str, Any] | None = None) -> InvocationOutcome:
        return cls(
            stdout, stderr, 127, elapsed_ms, InvocationState.SPAWN_FAILED,
            metadata={} if metadata is None else metadata,
        )

    @classmethod
    def harness_failed(cls, message: str, *,
                       metadata: Mapping[str, Any] | None = None) -> InvocationOutcome:
        if not isinstance(message, str) or not message.strip():
            raise ValueError("harness failure requires a non-empty message")
        return cls("", message, None, None, InvocationState.HARNESS_FAILED,
                   metadata={} if metadata is None else metadata)

    @classmethod
    def from_legacy_dict(cls, agent: str, raw: Mapping[str, Any], *,
                         allow_nonzero_complete: bool = False) -> InvocationOutcome:
        """Strict compatibility parser for third-party/test adapters.

        Presence-only validation is deliberately insufficient: boolean strings,
        timeout/code contradictions, and incomplete zero exits are rejected at
        this one trust boundary.
        """
        if not isinstance(raw, Mapping):
            raise TypeError(f"{agent}.invoke must return InvocationOutcome or a mapping, got {type(raw).__name__}")
        required = {"stdout", "stderr", "returncode", "timed_out", "elapsed_ms", "observation_complete"}
        missing = sorted(required - set(raw))
        if missing:
            raise KeyError(f"{agent}.invoke missing required result key(s): {', '.join(missing)}")
        stdout, stderr = raw["stdout"], raw["stderr"]
        if not isinstance(stdout, str) or not isinstance(stderr, str):
            raise TypeError(f"{agent}.invoke stdout and stderr must be strings")
        returncode = raw["returncode"]
        elapsed_ms = raw["elapsed_ms"]
        timed_out = raw["timed_out"]
        complete = raw["observation_complete"]
        if isinstance(returncode, bool) or not isinstance(returncode, int):
            raise TypeError(f"{agent}.invoke returncode must be an integer")
        if isinstance(elapsed_ms, bool) or not isinstance(elapsed_ms, int) or elapsed_ms < 0:
            raise ValueError(f"{agent}.invoke elapsed_ms must be a non-negative integer")
        if not isinstance(timed_out, bool) or not isinstance(complete, bool):
            raise TypeError(f"{agent}.invoke timed_out and observation_complete must be booleans")

        provider_error = raw.get("provider_error")
        raw_invocation_state = raw.get("invocation_state")
        try:
            invocation_state = (
                InvocationState(raw_invocation_state)
                if raw_invocation_state is not None else None
            )
        except ValueError as exc:
            raise ValueError(f"{agent}.invoke has invalid invocation_state") from exc
        raw_completion_evidence = raw.get("completion_evidence")
        try:
            completion_evidence = (
                CompletionEvidence(raw_completion_evidence)
                if raw_completion_evidence is not None else None
            )
        except ValueError as exc:
            raise ValueError(f"{agent}.invoke has invalid completion_evidence") from exc
        metadata = {
            key: value for key, value in raw.items()
            if key not in required | {
                "provider_error", "completion_evidence", "invocation_state"}
        }
        if provider_error is not None:
            if not isinstance(provider_error, str) or not provider_error.strip():
                raise ValueError(f"{agent}.invoke provider_error must be a non-empty string")
            if completion_evidence is not None:
                raise ValueError(f"{agent}.invoke provider failure cannot carry completion evidence")
            if complete:
                raise ValueError(f"{agent}.invoke cannot be complete and provider-failed")
            if timed_out:
                raise ValueError(f"{agent}.invoke cannot be both timed out and provider-failed")
            provider_state = (
                InvocationState.PROVIDER_FAILED
                if returncode == 0 else InvocationState.PROCESS_FAILED
            )
            if invocation_state not in {None, provider_state}:
                raise ValueError(f"{agent}.invoke provider failure contradicts invocation_state")
            return cls(stdout, stderr, returncode, elapsed_ms, provider_state,
                       provider_error=provider_error, metadata=metadata)
        if timed_out:
            if complete or returncode != 124:
                raise ValueError(f"{agent}.invoke timeout requires returncode 124 and an incomplete observation")
            if invocation_state not in {None, InvocationState.TIMED_OUT}:
                raise ValueError(f"{agent}.invoke timeout contradicts invocation_state")
            return cls.from_timeout(
                stdout=stdout, stderr=stderr, elapsed_ms=elapsed_ms,
                metadata=metadata)
        if complete:
            if invocation_state not in {None, InvocationState.COMPLETE}:
                raise ValueError(f"{agent}.invoke completion contradicts invocation_state")
            if returncode == 0:
                if completion_evidence not in {None, CompletionEvidence.NORMAL_EXIT}:
                    raise ValueError(f"{agent}.invoke zero-exit completion has invalid evidence")
                return cls.from_process(stdout=stdout, stderr=stderr, returncode=returncode,
                                        elapsed_ms=elapsed_ms).with_metadata(metadata)
            if (not allow_nonzero_complete
                    or completion_evidence is not CompletionEvidence.AGENT_WINDOW_EXHAUSTED):
                raise ValueError(f"{agent}.invoke non-zero completion needs explicit agent-window evidence")
            return cls(stdout, stderr, returncode, elapsed_ms, InvocationState.COMPLETE,
                       completion_evidence, metadata=metadata)
        if returncode == 0:
            raise ValueError(f"{agent}.invoke zero exit cannot be incomplete without a provider error")
        if invocation_state is InvocationState.SPAWN_FAILED:
            if returncode != 127:
                raise ValueError(f"{agent}.invoke spawn failure requires returncode 127")
            return cls.spawn_failed(
                stdout=stdout, stderr=stderr, elapsed_ms=elapsed_ms,
                metadata=metadata)
        if invocation_state not in {None, InvocationState.PROCESS_FAILED}:
            raise ValueError(f"{agent}.invoke process failure contradicts invocation_state")
        return cls.from_process(stdout=stdout, stderr=stderr, returncode=returncode,
                                elapsed_ms=elapsed_ms).with_metadata(metadata)

    def with_metadata(self, values: Mapping[str, Any] | None = None, **extra: Any) -> InvocationOutcome:
        if values is not None and not isinstance(values, Mapping):
            raise TypeError("invocation metadata must be a mapping or None")
        merged = {
            **dict(self.metadata),
            **dict({} if values is None else values),
            **extra,
        }
        return replace(self, metadata=merged)

    def with_provider_payload(self, payload: Any) -> InvocationOutcome:
        return replace(self, provider_payload=payload)

    def with_wire_text(self, *, stdout: str, stderr: str,
                       provider_error: str | None = None) -> InvocationOutcome:
        if self.state is InvocationState.PROVIDER_FAILED and provider_error is None:
            raise ValueError("redacted provider failure must retain a provider error")
        return replace(self, stdout=stdout, stderr=stderr, provider_error=provider_error)

    def with_provider_error(self, error: str | None, *, payload: Any = None) -> InvocationOutcome:
        if not error or self.state in {InvocationState.TIMED_OUT, InvocationState.SPAWN_FAILED, InvocationState.HARNESS_FAILED}:
            return replace(self, provider_payload=payload if payload is not None else self.provider_payload)
        state = (
            InvocationState.PROVIDER_FAILED
            if self.returncode == 0 else InvocationState.PROCESS_FAILED
        )
        return replace(self, state=state,
                       completion_evidence=None, provider_error=error.strip(),
                       provider_payload=payload if payload is not None else self.provider_payload)

    def as_agent_window_complete(self) -> InvocationOutcome:
        if self.state is not InvocationState.PROCESS_FAILED:
            raise ValueError("only a non-zero process failure can become an agent-window completion")
        return replace(self, state=InvocationState.COMPLETE,
                       completion_evidence=CompletionEvidence.AGENT_WINDOW_EXHAUSTED)

    def as_legacy_dict(self) -> dict[str, Any]:
        data = {
            **dict(self.metadata),
            "stdout": self.stdout,
            "stderr": self.stderr,
            "returncode": self.returncode,
            "timed_out": self.timed_out,
            "elapsed_ms": self.elapsed_ms,
            "observation_complete": self.observation_complete,
            "invocation_state": self.state.value,
            "completion_evidence": self.completion_evidence.value if self.completion_evidence else None,
        }
        if self.provider_error is not None:
            data["provider_error"] = self.provider_error
        return data


class TriggerExpectation(str, Enum):
    TRIGGER = "TRIGGER"
    DO_NOT_TRIGGER = "DO_NOT_TRIGGER"

    @classmethod
    def from_bool(cls, value: bool) -> TriggerExpectation:
        if not isinstance(value, bool):
            raise TypeError("trigger expectation must be a boolean at the wire boundary")
        return cls.TRIGGER if value else cls.DO_NOT_TRIGGER

    @property
    def should_trigger(self) -> bool:
        return self is TriggerExpectation.TRIGGER


class TriggerEvidenceKind(str, Enum):
    MOUNTED_PATH = "mounted_path"
    SKILL_TOOL = "skill_tool"
    VIBE_SKILL_TOOL = "vibe_skill_tool"


class TraceEventKind(str, Enum):
    EVENT = "event"
    SKILL_LOAD = "skill_load"
    ERROR = "error"
    MESSAGE = "message"
    COMMAND = "command"
    TOOL_CALL = "tool_call"
    FILE_WRITE = "file_write"
    FILE_READ = "file_read"
    METRIC = "metric"


@dataclass(frozen=True)
class TriggerEvidence:
    kind: TriggerEvidenceKind
    text: str

    def __post_init__(self) -> None:
        if not isinstance(self.kind, TriggerEvidenceKind):
            raise TypeError("trigger evidence kind must be TriggerEvidenceKind")
        if not isinstance(self.text, str) or not self.text.strip():
            raise ValueError("trigger evidence text must be non-empty")
        validate_json_text(self.text, "trigger evidence text")


@dataclass(frozen=True)
class TriggerDetection:
    evidence: tuple[TriggerEvidence, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.evidence, tuple) or not all(isinstance(item, TriggerEvidence) for item in self.evidence):
            raise TypeError("trigger evidence must be a tuple of TriggerEvidence")

    @property
    def triggered(self) -> bool:
        return bool(self.evidence)

    @property
    def legacy_evidence(self) -> list[str]:
        return [item.text for item in self.evidence]

    @classmethod
    def absent(cls) -> TriggerDetection:
        return cls()

    @classmethod
    def from_texts(cls, kind: TriggerEvidenceKind, texts: list[str] | tuple[str, ...]) -> TriggerDetection:
        return cls(tuple(TriggerEvidence(kind, text) for text in texts if isinstance(text, str) and text.strip()))


_USAGE_SOURCES = {"provider_reported", "trace_normalized", "estimated", "missing", "not_applicable"}
_COST_SOURCES = {"provider_reported", "trace_normalized", "price_table_estimated", "estimated", "missing", "not_applicable"}
_TRIGGER_RESERVED_METADATA = {
    "population", "agent", "provider", "model", "query", "should_trigger",
    "triggered", "pass", "observation_complete", "returncode", "timed_out",
    "elapsed_ms", "completion_evidence", "invocation_state", "evidence", "evidence_typed",
    "usage_normalized", "cost_normalized", "stderr", "provider_error",
    "query_id", "run_number", "invocation_metadata", "observation_metadata",
    "measurement_status", "trigger_evidence_observed",
}
_TRIGGER_EXPERIMENT_METADATA = {
    "measurement", "ablation", "skill_tree_hash", "protocol_sha256",
    "protocol_observation", "trace_dir", "trace_error", "telemetry_error",
    "error",
}


def validated_trigger_protocol_limits(
    *, timeout_seconds: Any, runs_per_query: Any, workers: Any,
) -> tuple[int, int, int]:
    """Return protocol concurrency limits only when all are positive integers."""
    values = {
        "timeout_seconds": timeout_seconds,
        "runs_per_query": runs_per_query,
        "workers": workers,
    }
    for label, value in values.items():
        if type(value) is not int or value <= 0:
            raise ValueError(f"{label} must be a positive integer")
    return timeout_seconds, runs_per_query, workers


def validated_trigger_model(value: Any, label: str = "model") -> str | None:
    """Return a protocol model only when its persisted identity is unambiguous."""
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be None or a non-empty string")
    validate_json_text(value, label)
    return value


def _usage_block(block: Mapping[str, Any]) -> Mapping[str, Any]:
    if not isinstance(block, Mapping):
        raise TypeError("usage telemetry must be a mapping")
    source = block.get("source")
    if source not in _USAGE_SOURCES:
        raise ValueError(f"usage telemetry has invalid source {source!r}")
    if source in {"missing", "not_applicable"}:
        if set(block) != {"source"}:
            raise ValueError(f"{source} usage telemetry cannot carry numeric evidence")
        return MappingProxyType(dict(block))
    token_keys = {
        "input_tokens", "output_tokens", "total_tokens", "cache_read_tokens",
        "cache_write_tokens", "reasoning_tokens",
    }
    unknown = set(block) - token_keys - {"source"}
    if unknown:
        raise ValueError(f"usage telemetry has unknown field(s): {', '.join(sorted(unknown))}")
    observed = []
    for key in token_keys:
        if key not in block:
            continue
        measurement = telemetry_domain.measurement_from_usage_block(block, key)
        if measurement.availability != telemetry_domain.AVAILABLE:
            raise ValueError(f"usage telemetry {key} must be a finite non-negative integer")
        observed.append(key)
    if not observed:
        raise ValueError("available usage telemetry requires at least one token measurement")
    return MappingProxyType(dict(block))


def _cost_block(block: Mapping[str, Any]) -> Mapping[str, Any]:
    if not isinstance(block, Mapping):
        raise TypeError("cost telemetry must be a mapping")
    source = block.get("source")
    if source not in _COST_SOURCES:
        raise ValueError(f"cost telemetry has invalid source {source!r}")
    if source in {"missing", "not_applicable"}:
        if set(block) != {"source"}:
            raise ValueError(f"{source} cost telemetry cannot carry numeric evidence")
        return freeze_json_mapping(block, "cost telemetry")
    numeric_keys = {
        "total_cost", "input_cost", "output_cost", "cache_read_cost",
        "cache_write_cost", "reasoning_cost",
    }
    allowed = numeric_keys | {"source", "currency", "pricing_model", "pricing_table_version", "pricing_notes"}
    unknown = set(block) - allowed
    if unknown:
        raise ValueError(f"cost telemetry has unknown field(s): {', '.join(sorted(unknown))}")
    measurement = telemetry_domain.measurement_from_cost_block(block)
    if measurement.availability != telemetry_domain.AVAILABLE:
        raise ValueError("available cost telemetry requires finite non-negative total_cost and ISO currency")
    for key in numeric_keys:
        if key not in block:
            continue
        value = block[key]
        try:
            finite = math.isfinite(float(value))
        except (OverflowError, TypeError, ValueError):
            finite = False
        if (isinstance(value, bool) or not isinstance(value, (int, float))
                or not finite or value < 0):
            raise ValueError(f"cost telemetry {key} must be finite and non-negative")
    currency = block.get("currency", "USD")
    if not isinstance(currency, str) or not re.fullmatch(r"[A-Z]{3}", currency):
        raise ValueError("cost telemetry currency must be a three-letter uppercase ISO code")
    for key in ("pricing_model", "pricing_table_version"):
        if key in block and (not isinstance(block[key], str) or not block[key]):
            raise ValueError(f"cost telemetry {key} must be a non-empty string")
    notes = block.get("pricing_notes")
    if notes is not None and (not isinstance(notes, list) or not all(isinstance(item, str) for item in notes)):
        raise ValueError("cost telemetry pricing_notes must be a list of strings")
    return freeze_json_mapping(block, "cost telemetry")


@dataclass(frozen=True)
class CompleteTriggerResult:
    """A quality result backed by a complete observation window."""

    expectation: TriggerExpectation
    triggered: bool

    def __post_init__(self) -> None:
        if not isinstance(self.expectation, TriggerExpectation):
            raise TypeError("complete trigger results require a typed expectation")
        if not isinstance(self.triggered, bool):
            raise TypeError("complete trigger results require a boolean triggered value")

    @property
    def passed(self) -> bool:
        return self.triggered == self.expectation.should_trigger


@dataclass(frozen=True)
class IncompleteTriggerResult:
    """An invocation outcome that cannot inhabit the quality-result state."""

    state: InvocationState

    def __post_init__(self) -> None:
        if not isinstance(self.state, InvocationState) or self.state is InvocationState.COMPLETE:
            raise ValueError("incomplete trigger results require a non-complete invocation state")


TriggerResult: TypeAlias = CompleteTriggerResult | IncompleteTriggerResult


@dataclass(frozen=True, order=True)
class TriggerRepetitionIdentity:
    """Stable identity for one persisted trigger-matrix repetition.

    Trigger rates do not assume matched randomness across arms, but causal
    comparison still needs to prove that no repetition was duplicated, lost,
    or silently replaced before rates are aggregated.
    """

    query_id: str
    run_number: int

    def __post_init__(self) -> None:
        if not isinstance(self.query_id, str) or not self.query_id.strip():
            raise ValueError("trigger query_id must be a non-empty string")
        validate_json_text(self.query_id, "trigger query_id")
        if (isinstance(self.run_number, bool) or not isinstance(self.run_number, int)
                or self.run_number < 1):
            raise ValueError("trigger run_number must be a positive integer")

    def as_dict(self) -> dict[str, Any]:
        return {"query_id": self.query_id, "run_number": self.run_number}

    @classmethod
    def from_row(cls, raw: Mapping[str, Any]) -> TriggerRepetitionIdentity | None:
        has_query_id = "query_id" in raw
        has_run_number = "run_number" in raw
        if has_query_id != has_run_number:
            raise ValueError("trigger repetition identity requires both query_id and run_number")
        if not has_query_id:
            return None
        return cls(raw["query_id"], raw["run_number"])


@dataclass(frozen=True)
class TriggerObservation:
    agent: str
    model: str | None
    query: str
    expectation: TriggerExpectation
    invocation: InvocationOutcome
    detection: TriggerDetection
    usage: Mapping[str, Any]
    cost: Mapping[str, Any]
    metadata: Mapping[str, Any] = field(default_factory=dict, compare=False)
    identity: TriggerRepetitionIdentity | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.agent, str) or not self.agent.strip():
            raise ValueError("trigger observation agent must be non-empty")
        validate_json_text(self.agent, "trigger observation agent")
        if self.model is not None and (not isinstance(self.model, str) or not self.model.strip()):
            raise ValueError("trigger observation model must be None or non-empty")
        if self.model is not None:
            validate_json_text(self.model, "trigger observation model")
        if not isinstance(self.query, str) or not self.query.strip():
            raise ValueError("trigger observation query must be non-empty")
        validate_json_text(self.query, "trigger observation query")
        if not isinstance(self.expectation, TriggerExpectation):
            raise TypeError("trigger observation expectation must be TriggerExpectation")
        if not isinstance(self.invocation, InvocationOutcome):
            raise TypeError("trigger observation invocation must be InvocationOutcome")
        if not isinstance(self.detection, TriggerDetection):
            raise TypeError("trigger observation detection must be TriggerDetection")
        if self.identity is not None and not isinstance(self.identity, TriggerRepetitionIdentity):
            raise TypeError("trigger observation identity must be TriggerRepetitionIdentity or None")
        usage = _usage_block(self.usage)
        cost = _cost_block(self.cost)
        if not self.invocation.observation_complete and (
            usage.get("source") != "missing" or cost.get("source") != "missing"
        ):
            raise ValueError("incomplete trigger observations must carry missing usage and cost")
        metadata = freeze_json_mapping(self.metadata, "trigger observation metadata")
        collisions = sorted(_TRIGGER_RESERVED_METADATA & set(metadata))
        if collisions:
            raise ValueError(f"trigger metadata collides with derived field(s): {', '.join(collisions)}")
        invocation_collisions = sorted(
            set(self.invocation.metadata)
            & (_TRIGGER_RESERVED_METADATA | _TRIGGER_EXPERIMENT_METADATA | set(metadata)))
        if invocation_collisions:
            raise ValueError(
                "invocation metadata collides with trigger-owned field(s): "
                + ", ".join(invocation_collisions))
        object.__setattr__(self, "usage", usage)
        object.__setattr__(self, "cost", cost)
        object.__setattr__(self, "metadata", metadata)

    @property
    def result(self) -> TriggerResult:
        """Classify evidence before exposing a quality verdict."""
        if self.invocation.observation_complete:
            return CompleteTriggerResult(self.expectation, self.detection.triggered)
        return IncompleteTriggerResult(self.invocation.state)

    @property
    def passed(self) -> bool | None:
        """Compatibility projection; incomplete evidence has no pass value."""
        result = self.result
        return result.passed if isinstance(result, CompleteTriggerResult) else None

    def with_metadata(self, values: Mapping[str, Any]) -> TriggerObservation:
        return replace(self, metadata={**dict(self.metadata), **dict(values)})

    def as_row(self) -> dict[str, Any]:
        result = self.result
        complete = isinstance(result, CompleteTriggerResult)
        row: dict[str, Any] = {
            "population": "trigger",
            "agent": self.agent,
            "model": self.model,
            "query": self.query,
            "should_trigger": self.expectation.should_trigger,
            "trigger_evidence_observed": self.detection.triggered,
            "triggered": result.triggered if complete else None,
            "pass": result.passed if complete else None,
            "measurement_status": "complete" if complete else "incomplete",
            "observation_complete": self.invocation.observation_complete,
            "returncode": self.invocation.returncode,
            "timed_out": self.invocation.timed_out,
            "elapsed_ms": self.invocation.elapsed_ms,
            "completion_evidence": (
                self.invocation.completion_evidence.value
                if self.invocation.completion_evidence else None
            ),
            "invocation_state": self.invocation.state.value,
            "evidence": self.detection.legacy_evidence,
            "evidence_typed": [
                {"kind": item.kind.value, "text": item.text}
                for item in self.detection.evidence
            ],
            "usage_normalized": dict(self.usage),
            "cost_normalized": dict(self.cost),
            "stderr": self.invocation.stderr[-1000:],
            "invocation_metadata": dict(self.invocation.metadata),
            "observation_metadata": dict(self.metadata),
        }
        for key, value in self.invocation.metadata.items():
            row[key] = value
        if self.invocation.provider_error is not None:
            row["provider_error"] = self.invocation.provider_error
        if self.identity is not None:
            row.update(self.identity.as_dict())
        for key, value in self.metadata.items():
            row[key] = value
        return row

    @classmethod
    def from_row(cls, raw: Mapping[str, Any], *, default_agent: str | None = None) -> TriggerObservation:
        """Re-erect the typed contract when reading a persisted trigger row."""
        if not isinstance(raw, Mapping):
            raise TypeError("trigger observation row must be a mapping")
        if raw.get("population") != "trigger":
            raise ValueError("trigger observation row must declare population 'trigger'")
        agent = raw.get("agent", default_agent)
        model = raw.get("model")
        query = raw.get("query")
        should_trigger = raw.get("should_trigger")
        usage = raw.get("usage_normalized")
        cost = raw.get("cost_normalized")
        if not isinstance(agent, str) or not agent.strip():
            raise ValueError("trigger observation agent must be non-empty")
        if model is not None and not isinstance(model, str):
            raise TypeError("trigger observation model must be a string or null")
        if not isinstance(query, str) or not query.strip():
            raise ValueError("trigger observation query must be non-empty")
        if not isinstance(should_trigger, bool):
            raise TypeError("trigger observation should_trigger must be boolean")
        if not isinstance(usage, Mapping) or not isinstance(cost, Mapping):
            raise TypeError("trigger observation usage and cost must be mappings")
        has_invocation_namespace = "invocation_metadata" in raw
        has_observation_namespace = "observation_metadata" in raw
        if has_invocation_namespace != has_observation_namespace:
            raise ValueError(
                "trigger observation metadata requires both explicit namespaces")
        if has_invocation_namespace:
            raw_invocation_metadata = raw["invocation_metadata"]
            raw_observation_metadata = raw["observation_metadata"]
            if (not isinstance(raw_invocation_metadata, Mapping)
                    or not isinstance(raw_observation_metadata, Mapping)):
                raise TypeError("trigger observation metadata namespaces must be mappings")
            invocation_metadata = freeze_json_mapping(
                raw_invocation_metadata, "persisted invocation metadata")
            observation_metadata = freeze_json_mapping(
                raw_observation_metadata, "persisted observation metadata")
            declared_flat_keys = set(invocation_metadata) | set(observation_metadata)
            undeclared_flat_keys = (
                set(raw) - _TRIGGER_RESERVED_METADATA - declared_flat_keys)
            if undeclared_flat_keys:
                raise ValueError(
                    "persisted trigger row has undeclared flattened metadata: "
                    + ", ".join(sorted(str(key) for key in undeclared_flat_keys)))
            for namespace, values in (
                ("invocation", invocation_metadata),
                ("observation", observation_metadata),
            ):
                for key, value in values.items():
                    if key not in raw or not strict_json_equal(raw[key], value):
                        raise ValueError(
                            f"persisted {namespace} metadata disagrees with flattened field {key!r}")
        else:
            # Legacy rows flattened both mappings. Known experiment-owned keys
            # remain observation metadata; other non-reserved keys came from the
            # invocation compatibility mapping.
            observation_metadata = {
                key: raw[key] for key in _TRIGGER_EXPERIMENT_METADATA if key in raw
            }
            invocation_metadata = {
                key: value for key, value in raw.items()
                if key not in _TRIGGER_RESERVED_METADATA
                and key not in _TRIGGER_EXPERIMENT_METADATA
            }
        expectation = TriggerExpectation.from_bool(should_trigger)
        legacy_invocation = {
            **invocation_metadata,
            "stdout": "",
            "stderr": raw.get("stderr", ""),
            "returncode": raw.get("returncode"),
            "timed_out": raw.get("timed_out"),
            "elapsed_ms": raw.get("elapsed_ms"),
            "observation_complete": raw.get("observation_complete"),
            "completion_evidence": raw.get("completion_evidence"),
            "invocation_state": raw.get("invocation_state"),
            **({"provider_error": raw["provider_error"]} if "provider_error" in raw else {}),
        }
        if (raw.get("returncode") is None and raw.get("elapsed_ms") is None
                and raw.get("observation_complete") is False
                and raw.get("timed_out") is False
                and raw.get("completion_evidence") is None
                and "provider_error" not in raw
                and isinstance(raw.get("error"), str) and raw.get("error", "").strip()):
            invocation = InvocationOutcome.harness_failed(
                raw["error"], metadata=invocation_metadata)
        else:
            allow_nonzero = (
                raw.get("completion_evidence") == CompletionEvidence.AGENT_WINDOW_EXHAUSTED.value
            )
            invocation = InvocationOutcome.from_legacy_dict(
                str(agent or "trigger"), legacy_invocation,
                allow_nonzero_complete=allow_nonzero,
            )

        legacy_evidence = raw.get("evidence", [])
        if not isinstance(legacy_evidence, list) or not all(isinstance(item, str) for item in legacy_evidence):
            raise TypeError("trigger evidence must be a list of strings")
        typed_wire = raw.get("evidence_typed")
        evidence_items: list[TriggerEvidence] = []
        if "evidence_typed" in raw:
            if not isinstance(typed_wire, list):
                raise TypeError("evidence_typed must be a list")
            for item in typed_wire:
                if not isinstance(item, Mapping):
                    raise TypeError("typed trigger evidence entries must be mappings")
                evidence_items.append(TriggerEvidence(
                    TriggerEvidenceKind(item.get("kind")), item.get("text"),
                ))
            if [item.text for item in evidence_items] != legacy_evidence:
                raise ValueError("typed and legacy trigger evidence disagree")
        else:
            evidence_items = [
                TriggerEvidence(TriggerEvidenceKind.MOUNTED_PATH, item)
                for item in legacy_evidence
            ]
        detection = TriggerDetection(tuple(evidence_items))
        observation = cls(
            agent=agent, model=model, query=query, expectation=expectation,
            invocation=invocation, detection=detection,
            usage=usage, cost=cost,
            metadata=observation_metadata,
            identity=TriggerRepetitionIdentity.from_row(raw),
        )
        result = observation.result
        expected_status = (
            "complete" if isinstance(result, CompleteTriggerResult) else "incomplete"
        )
        stored_status = raw.get("measurement_status")
        if "measurement_status" in raw and stored_status != expected_status:
            raise ValueError("persisted measurement_status disagrees with the typed observation")
        stored_evidence = raw.get("trigger_evidence_observed")
        if "trigger_evidence_observed" in raw and (
            not isinstance(stored_evidence, bool) or stored_evidence != detection.triggered
        ):
            raise ValueError("persisted trigger evidence flag disagrees with trigger evidence")
        stored_triggered = raw.get("triggered")
        stored_pass = raw.get("pass")
        if isinstance(result, CompleteTriggerResult):
            if not isinstance(stored_triggered, bool) or stored_triggered != result.triggered:
                raise ValueError("persisted triggered flag disagrees with the typed observation")
            if not isinstance(stored_pass, bool) or stored_pass != result.passed:
                raise ValueError("persisted pass flag disagrees with the typed observation")
        else:
            # Historical rows encoded incomplete evidence as false. Accept those
            # rows at the wire boundary, but never produce that lossy projection.
            if stored_triggered is not None and (
                not isinstance(stored_triggered, bool) or stored_triggered != detection.triggered
            ):
                raise ValueError(
                    "persisted triggered flag disagrees with incomplete trigger evidence")
            if stored_pass is not None and (
                not isinstance(stored_pass, bool) or stored_pass
            ):
                raise ValueError(
                    "an incomplete trigger observation cannot carry a passing verdict")
        return observation

    @classmethod
    def harness_failure(cls, *, agent: str, model: str | None, query: str,
                        expectation: TriggerExpectation, error: BaseException,
                        metadata: Mapping[str, Any] | None = None,
                        identity: TriggerRepetitionIdentity | None = None) -> TriggerObservation:
        message = f"{type(error).__name__}: {error}"
        if metadata is not None and not isinstance(metadata, Mapping):
            raise TypeError("trigger failure metadata must be a mapping or None")
        invocation = InvocationOutcome.harness_failed(message)
        return cls(agent, model, query, expectation, invocation, TriggerDetection.absent(),
                   {"source": "missing"}, {"source": "missing"},
                   {"error": message, **dict({} if metadata is None else metadata)},
                   identity)
