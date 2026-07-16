"""Typed contracts for autonomous-trigger subprocesses and observations.

Wire formats remain dictionaries at CLI/artifact boundaries. Internally, completion,
provider failure, trigger detection, and pass/fail are derived from closed states so
contradictory booleans cannot be assembled independently.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import Enum
import math
import re
from types import MappingProxyType
from typing import Any, Mapping

import telemetry as telemetry_domain


class InvocationState(str, Enum):
    COMPLETE = "complete"
    TIMED_OUT = "timed_out"
    SPAWN_FAILED = "spawn_failed"
    PROCESS_FAILED = "process_failed"
    PROVIDER_FAILED = "provider_failed"
    HARNESS_FAILED = "harness_failed"


class CompletionEvidence(str, Enum):
    NORMAL_EXIT = "normal_exit"
    AGENT_WINDOW_EXHAUSTED = "agent_window_exhausted"


_INVOCATION_RESERVED_METADATA = {
    "stdout", "stderr", "returncode", "timed_out", "elapsed_ms",
    "observation_complete", "provider_error", "completion_evidence",
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
        if self.elapsed_ms is not None and (
            isinstance(self.elapsed_ms, bool) or not isinstance(self.elapsed_ms, int) or self.elapsed_ms < 0
        ):
            raise ValueError("invocation elapsed_ms must be a non-negative integer or None")
        if self.returncode is not None and (isinstance(self.returncode, bool) or not isinstance(self.returncode, int)):
            raise TypeError("invocation returncode must be an integer or None")
        if not isinstance(self.state, InvocationState):
            raise TypeError("invocation state must be InvocationState")
        if self.completion_evidence is not None and not isinstance(self.completion_evidence, CompletionEvidence):
            raise TypeError("completion_evidence must be CompletionEvidence or None")
        if self.provider_error is not None and (not isinstance(self.provider_error, str) or not self.provider_error.strip()):
            raise ValueError("provider_error must be a non-empty string")

        if self.state is InvocationState.COMPLETE:
            if self.returncode == 0:
                if self.completion_evidence not in {None, CompletionEvidence.NORMAL_EXIT}:
                    raise ValueError("zero-exit completion cannot claim an exhausted agent window")
            elif (self.returncode in {None, 124, 127}
                  or self.completion_evidence is not CompletionEvidence.AGENT_WINDOW_EXHAUSTED):
                raise ValueError("non-zero completion requires a spawned non-timeout code and explicit agent-window evidence")
            if self.provider_error is not None:
                raise ValueError("a complete invocation cannot carry a provider error")
        elif self.completion_evidence is not None:
            raise ValueError("only complete invocations can carry completion evidence")

        if self.state is InvocationState.TIMED_OUT and self.returncode != 124:
            raise ValueError("timed-out invocation must use returncode 124")
        if self.state is InvocationState.SPAWN_FAILED and self.returncode != 127:
            raise ValueError("spawn failure must use returncode 127")
        if self.state is InvocationState.PROCESS_FAILED:
            if self.returncode in {None, 0, 124, 127}:
                raise ValueError("process failure requires a non-zero non-timeout returncode")
        if self.state is InvocationState.PROVIDER_FAILED:
            if self.returncode is None or self.returncode in {124, 127} or self.provider_error is None:
                raise ValueError("provider failure requires a non-timeout spawned process and provider_error")
        elif self.provider_error is not None:
            raise ValueError("provider_error requires PROVIDER_FAILED state")
        if self.state is InvocationState.HARNESS_FAILED:
            if self.returncode is not None or self.elapsed_ms is not None:
                raise ValueError("harness failure has no process returncode or measured elapsed time")
        elif self.returncode is None or self.elapsed_ms is None:
            raise ValueError("process invocation states require returncode and elapsed_ms")

        metadata = dict(self.metadata)
        collisions = sorted(_INVOCATION_RESERVED_METADATA & set(metadata))
        if collisions:
            raise ValueError(f"invocation metadata collides with derived field(s): {', '.join(collisions)}")
        object.__setattr__(self, "metadata", MappingProxyType(metadata))

    @property
    def observation_complete(self) -> bool:
        return self.state is InvocationState.COMPLETE

    @property
    def timed_out(self) -> bool:
        return self.state is InvocationState.TIMED_OUT

    @classmethod
    def from_process(cls, *, stdout: str, stderr: str, returncode: int,
                     elapsed_ms: int,
                     metadata: Mapping[str, Any] | None = None) -> "InvocationOutcome":
        if returncode == 0:
            state = InvocationState.COMPLETE
            evidence = CompletionEvidence.NORMAL_EXIT
        elif returncode == 124:
            state = InvocationState.TIMED_OUT
            evidence = None
        elif returncode == 127:
            state = InvocationState.SPAWN_FAILED
            evidence = None
        else:
            state = InvocationState.PROCESS_FAILED
            evidence = None
        return cls(stdout, stderr, returncode, elapsed_ms, state, evidence,
                   metadata=metadata or {})

    @classmethod
    def harness_failed(cls, message: str, *,
                       metadata: Mapping[str, Any] | None = None) -> "InvocationOutcome":
        if not isinstance(message, str) or not message.strip():
            raise ValueError("harness failure requires a non-empty message")
        return cls("", message, None, None, InvocationState.HARNESS_FAILED,
                   metadata=metadata or {})

    @classmethod
    def from_legacy_dict(cls, agent: str, raw: Mapping[str, Any], *,
                         allow_nonzero_complete: bool = False) -> "InvocationOutcome":
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
            if key not in required | {"provider_error", "completion_evidence"}
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
            return cls(stdout, stderr, returncode, elapsed_ms, InvocationState.PROVIDER_FAILED,
                       provider_error=provider_error, metadata=metadata)
        if timed_out:
            if complete or returncode != 124:
                raise ValueError(f"{agent}.invoke timeout requires returncode 124 and an incomplete observation")
            return cls.from_process(stdout=stdout, stderr=stderr, returncode=returncode,
                                    elapsed_ms=elapsed_ms).with_metadata(metadata)
        if complete:
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
        if returncode == 124:
            raise ValueError(f"{agent}.invoke returncode 124 must set timed_out true")
        if returncode == 0:
            raise ValueError(f"{agent}.invoke zero exit cannot be incomplete without a provider error")
        return cls.from_process(stdout=stdout, stderr=stderr, returncode=returncode,
                                elapsed_ms=elapsed_ms).with_metadata(metadata)

    def with_metadata(self, values: Mapping[str, Any] | None = None, **extra: Any) -> "InvocationOutcome":
        merged = {**dict(self.metadata), **dict(values or {}), **extra}
        return replace(self, metadata=merged)

    def with_provider_payload(self, payload: Any) -> "InvocationOutcome":
        return replace(self, provider_payload=payload)

    def with_wire_text(self, *, stdout: str, stderr: str,
                       provider_error: str | None = None) -> "InvocationOutcome":
        if self.state is InvocationState.PROVIDER_FAILED and provider_error is None:
            raise ValueError("redacted provider failure must retain a provider error")
        return replace(self, stdout=stdout, stderr=stderr, provider_error=provider_error)

    def with_provider_error(self, error: str | None, *, payload: Any = None) -> "InvocationOutcome":
        if not error or self.state in {InvocationState.TIMED_OUT, InvocationState.SPAWN_FAILED, InvocationState.HARNESS_FAILED}:
            return replace(self, provider_payload=payload if payload is not None else self.provider_payload)
        return replace(self, state=InvocationState.PROVIDER_FAILED,
                       completion_evidence=None, provider_error=error.strip(),
                       provider_payload=payload if payload is not None else self.provider_payload)

    def as_agent_window_complete(self) -> "InvocationOutcome":
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
            "completion_evidence": self.completion_evidence.value if self.completion_evidence else None,
        }
        if self.provider_error is not None:
            data["provider_error"] = self.provider_error
        return data


class TriggerExpectation(str, Enum):
    TRIGGER = "TRIGGER"
    DO_NOT_TRIGGER = "DO_NOT_TRIGGER"

    @classmethod
    def from_bool(cls, value: bool) -> "TriggerExpectation":
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
    def absent(cls) -> "TriggerDetection":
        return cls()

    @classmethod
    def from_texts(cls, kind: TriggerEvidenceKind, texts: list[str] | tuple[str, ...]) -> "TriggerDetection":
        return cls(tuple(TriggerEvidence(kind, text) for text in texts if isinstance(text, str) and text.strip()))


_USAGE_SOURCES = {"provider_reported", "trace_normalized", "estimated", "missing", "not_applicable"}
_COST_SOURCES = {"provider_reported", "trace_normalized", "price_table_estimated", "estimated", "missing", "not_applicable"}
_TRIGGER_RESERVED_METADATA = {
    "population", "agent", "provider", "model", "query", "should_trigger",
    "triggered", "pass", "observation_complete", "returncode", "timed_out",
    "elapsed_ms", "completion_evidence", "evidence", "evidence_typed",
    "usage_normalized", "cost_normalized", "stderr", "provider_error",
}


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
        return MappingProxyType(dict(block))
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
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)) or value < 0:
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
    return MappingProxyType(dict(block))


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

    def __post_init__(self) -> None:
        if not isinstance(self.agent, str) or not self.agent.strip():
            raise ValueError("trigger observation agent must be non-empty")
        if self.model is not None and (not isinstance(self.model, str) or not self.model.strip()):
            raise ValueError("trigger observation model must be None or non-empty")
        if not isinstance(self.query, str) or not self.query.strip():
            raise ValueError("trigger observation query must be non-empty")
        if not isinstance(self.expectation, TriggerExpectation):
            raise TypeError("trigger observation expectation must be TriggerExpectation")
        if not isinstance(self.invocation, InvocationOutcome):
            raise TypeError("trigger observation invocation must be InvocationOutcome")
        if not isinstance(self.detection, TriggerDetection):
            raise TypeError("trigger observation detection must be TriggerDetection")
        usage = _usage_block(self.usage)
        cost = _cost_block(self.cost)
        if not self.invocation.observation_complete:
            if usage.get("source") != "missing" or cost.get("source") != "missing":
                raise ValueError("incomplete trigger observations must carry missing usage and cost")
        metadata = dict(self.metadata)
        collisions = sorted(_TRIGGER_RESERVED_METADATA & set(metadata))
        if collisions:
            raise ValueError(f"trigger metadata collides with derived field(s): {', '.join(collisions)}")
        object.__setattr__(self, "usage", usage)
        object.__setattr__(self, "cost", cost)
        object.__setattr__(self, "metadata", MappingProxyType(metadata))

    @property
    def passed(self) -> bool:
        return self.invocation.observation_complete and self.detection.triggered == self.expectation.should_trigger

    def as_row(self) -> dict[str, Any]:
        row: dict[str, Any] = {
            "population": "trigger",
            "agent": self.agent,
            "model": self.model,
            "query": self.query,
            "should_trigger": self.expectation.should_trigger,
            "triggered": self.detection.triggered,
            "pass": self.passed,
            "observation_complete": self.invocation.observation_complete,
            "returncode": self.invocation.returncode,
            "timed_out": self.invocation.timed_out,
            "elapsed_ms": self.invocation.elapsed_ms,
            "completion_evidence": (
                self.invocation.completion_evidence.value
                if self.invocation.completion_evidence else None
            ),
            "evidence": self.detection.legacy_evidence,
            "evidence_typed": [
                {"kind": item.kind.value, "text": item.text}
                for item in self.detection.evidence
            ],
            "usage_normalized": dict(self.usage),
            "cost_normalized": dict(self.cost),
            "stderr": self.invocation.stderr[-1000:],
        }
        for key, value in self.invocation.metadata.items():
            if key not in row:
                row[key] = value
        if self.invocation.provider_error is not None:
            row["provider_error"] = self.invocation.provider_error
        for key, value in self.metadata.items():
            if key not in row:
                row[key] = value
        return row

    @classmethod
    def from_row(cls, raw: Mapping[str, Any], *, default_agent: str | None = None) -> "TriggerObservation":
        """Re-erect the typed contract when reading a persisted trigger row."""
        if not isinstance(raw, Mapping):
            raise TypeError("trigger observation row must be a mapping")
        if raw.get("population") != "trigger":
            raise ValueError("trigger observation row must declare population 'trigger'")
        agent = raw.get("agent", default_agent)
        model = raw.get("model")
        query = raw.get("query")
        expectation = TriggerExpectation.from_bool(raw.get("should_trigger"))
        legacy_invocation = {
            "stdout": "",
            "stderr": raw.get("stderr", ""),
            "returncode": raw.get("returncode"),
            "timed_out": raw.get("timed_out"),
            "elapsed_ms": raw.get("elapsed_ms"),
            "observation_complete": raw.get("observation_complete"),
            "completion_evidence": raw.get("completion_evidence"),
            **({"provider_error": raw["provider_error"]} if "provider_error" in raw else {}),
        }
        if (raw.get("returncode") is None and raw.get("elapsed_ms") is None
                and raw.get("observation_complete") is False
                and raw.get("timed_out") is False
                and raw.get("completion_evidence") is None
                and "provider_error" not in raw
                and isinstance(raw.get("error"), str) and raw.get("error", "").strip()):
            invocation = InvocationOutcome.harness_failed(raw["error"])
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
        if not isinstance(raw.get("triggered"), bool) or raw["triggered"] != detection.triggered:
            raise ValueError("persisted triggered flag disagrees with trigger evidence")
        observation = cls(
            agent=agent, model=model, query=query, expectation=expectation,
            invocation=invocation, detection=detection,
            usage=raw.get("usage_normalized"), cost=raw.get("cost_normalized"),
        )
        if not isinstance(raw.get("pass"), bool) or raw["pass"] != observation.passed:
            raise ValueError("persisted pass flag disagrees with the typed observation")
        return observation

    @classmethod
    def harness_failure(cls, *, agent: str, model: str | None, query: str,
                        expectation: TriggerExpectation, error: BaseException,
                        metadata: Mapping[str, Any] | None = None) -> "TriggerObservation":
        message = f"{type(error).__name__}: {error}"
        invocation = InvocationOutcome.harness_failed(message)
        return cls(agent, model, query, expectation, invocation, TriggerDetection.absent(),
                   {"source": "missing"}, {"source": "missing"},
                   {"error": message, **dict(metadata or {})})
