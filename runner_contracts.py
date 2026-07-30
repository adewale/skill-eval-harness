"""Closed answer-runner outcomes.

Provider wire dictionaries are classified once into one of four mutually exclusive
states. Artifact writers consume the union exhaustively and never repair booleans.
"""
from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any, TypeAlias

from json_contracts import freeze_json_mapping


class Provider(str, Enum):
    CODEX = "codex"
    CLAUDE = "claude"
    VIBE = "vibe"
    SUBAGENT = "subagent"
    JETTY = "jetty"


def _finite_nonnegative(value: Any, label: str, *, integer: bool = False) -> int | float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)) or value < 0:
        raise ValueError(f"{label} must be finite and non-negative")
    if integer and (not isinstance(value, int) or isinstance(value, bool)):
        raise ValueError(f"{label} must be a non-negative integer")
    return value


_RESERVED_EVIDENCE_KEYS = frozenset({
    "schema_version", "source", "tool_calls", "commands", "file_reads",
    "file_writes", "errors", "retries", "repeated_command_max",
    "skill_invoked", "skill_invocation_evidence", "parse_errors",
    "input_tokens", "output_tokens", "total_tokens", "cache_read_tokens",
    "cache_write_tokens", "cache_creation_tokens", "cost_usd",
    "usage_normalized", "cost_normalized", "otel",
    "returncode", "timed_out", "elapsed_ms",
    "observation_complete", "process_observation_complete",
    "provider_response_complete", "trace_observation_complete",
    "operation_observation_complete", "artifact_set_complete",
    "observation_evidence", "telemetry", "telemetry_schema_version",
})


def _validate_usage(value: Any, label: str) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            _validate_usage(item, f"{label}.{key}")
        return
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must contain only numeric measurements")
    _finite_nonnegative(value, label, integer=label.casefold().endswith("tokens"))


@dataclass(frozen=True)
class OutcomeContext:
    provider: Provider
    model: str | None = None
    elapsed_ms: int | None = None
    stderr: str = ""
    trace_text: str = ""
    usage: Mapping[str, Any] | None = None
    cost_usd: float | None = None
    metadata_extra: Mapping[str, Any] = field(default_factory=dict)
    metrics_extra: Mapping[str, Any] = field(default_factory=dict)
    environment: Mapping[str, Any] | None = None
    diagnose_returncode: bool = True

    def __post_init__(self) -> None:
        try:
            object.__setattr__(self, "provider", Provider(self.provider))
        except ValueError as exc:
            raise ValueError(f"unknown runner provider {self.provider!r}") from exc
        if self.model is not None and (not isinstance(self.model, str) or not self.model.strip()):
            raise ValueError("runner model must be None or a non-empty string")
        if self.elapsed_ms is not None:
            _finite_nonnegative(self.elapsed_ms, "elapsed_ms", integer=True)
        if not isinstance(self.stderr, str) or not isinstance(self.trace_text, str):
            raise TypeError("stderr and trace_text must be strings")
        if self.cost_usd is not None:
            _finite_nonnegative(self.cost_usd, "cost_usd")
        if self.usage is not None:
            if not isinstance(self.usage, Mapping):
                raise TypeError("usage must be a mapping or None")
            _validate_usage(self.usage, "usage")
            object.__setattr__(self, "usage", freeze_json_mapping(self.usage, "usage"))
        for label, values in (("metadata_extra", self.metadata_extra),
                              ("metrics_extra", self.metrics_extra)):
            collisions = _RESERVED_EVIDENCE_KEYS & set(values)
            if collisions:
                raise ValueError(f"{label} cannot override derived evidence: {', '.join(sorted(collisions))}")
        object.__setattr__(self, "metadata_extra", freeze_json_mapping(self.metadata_extra, "metadata_extra"))
        object.__setattr__(self, "metrics_extra", freeze_json_mapping(self.metrics_extra, "metrics_extra"))
        if self.environment is not None:
            object.__setattr__(self, "environment", freeze_json_mapping(self.environment, "environment"))
        if not isinstance(self.diagnose_returncode, bool):
            raise TypeError("diagnose_returncode must be boolean")

    def enriched(self, *, metadata: Mapping[str, Any] | None = None,
                 environment: Mapping[str, Any] | None = None) -> OutcomeContext:
        if metadata is not None and not isinstance(metadata, Mapping):
            raise TypeError("metadata must be a mapping or None")
        if environment is not None and not isinstance(environment, Mapping):
            raise TypeError("environment must be a mapping or None")
        return replace(
            self,
            metadata_extra={
                **dict(self.metadata_extra),
                **dict({} if metadata is None else metadata),
            },
            environment={
                **dict({} if self.environment is None else self.environment),
                **dict({} if environment is None else environment),
            },
        )


@dataclass(frozen=True)
class Completed:
    context: OutcomeContext
    answer: str
    returncode: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.context, OutcomeContext):
            raise TypeError("Completed context must be OutcomeContext")
        if type(self.returncode) is not int:
            raise TypeError("Completed returncode must be an integer")
        if self.returncode != 0:
            raise ValueError("Completed returncode must be 0")
        if not isinstance(self.answer, str):
            raise TypeError("Completed answer must be a string")
        if not self.answer:
            raise ValueError("Completed answer cannot be empty")


@dataclass(frozen=True)
class TimedOut:
    context: OutcomeContext
    timeout_s: int | None = None
    reason: str | None = None
    returncode: int = 124

    def __post_init__(self) -> None:
        if not isinstance(self.context, OutcomeContext):
            raise TypeError("TimedOut context must be OutcomeContext")
        if type(self.returncode) is not int:
            raise TypeError("TimedOut returncode must be an integer")
        if self.returncode != 124:
            raise ValueError("TimedOut returncode must be 124")
        if self.timeout_s is not None and (isinstance(self.timeout_s, bool) or not isinstance(self.timeout_s, int) or self.timeout_s <= 0):
            raise ValueError("timeout_s must be a positive integer or None")
        if self.reason is not None and (not isinstance(self.reason, str) or not self.reason.strip()):
            raise ValueError("timeout reason must be non-empty or None")


@dataclass(frozen=True)
class SpawnFailed:
    context: OutcomeContext
    reason: str
    returncode: int = 127

    def __post_init__(self) -> None:
        if not isinstance(self.context, OutcomeContext):
            raise TypeError("SpawnFailed context must be OutcomeContext")
        if type(self.returncode) is not int:
            raise TypeError("SpawnFailed returncode must be an integer")
        if self.returncode != 127:
            raise ValueError("SpawnFailed returncode must be 127")
        if not isinstance(self.reason, str) or not self.reason.strip():
            raise ValueError("SpawnFailed requires a reason")


@dataclass(frozen=True)
class ProviderFailed:
    context: OutcomeContext
    returncode: int
    reason: str | None = None
    answer: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.context, OutcomeContext):
            raise TypeError("ProviderFailed context must be OutcomeContext")
        if type(self.returncode) is not int:
            raise TypeError("ProviderFailed returncode must be an integer")
        if self.returncode in {124, 127}:
            raise ValueError("ProviderFailed requires an actual exited-process returncode")
        if self.reason is not None and (not isinstance(self.reason, str) or not self.reason.strip()):
            raise ValueError("provider failure reason must be non-empty or None")
        if self.returncode == 0 and self.reason is None:
            raise ValueError("zero-exit provider failure requires a protocol reason")
        if not isinstance(self.answer, str):
            raise TypeError("provider failure answer must be a string")


AnswerOutcome: TypeAlias = Completed | TimedOut | SpawnFailed | ProviderFailed


def outcome_context(outcome: AnswerOutcome) -> OutcomeContext:
    return outcome.context


def process_observation_complete(outcome: AnswerOutcome) -> bool:
    """Whether the subprocess reached an actual exit (success or failure)."""
    return isinstance(outcome, (Completed, ProviderFailed))


def provider_response_complete(outcome: AnswerOutcome) -> bool:
    """Whether a validated final provider answer exists."""
    return isinstance(outcome, Completed)


def outcome_with_context(outcome: AnswerOutcome, context: OutcomeContext) -> AnswerOutcome:
    return replace(outcome, context=context)


def RunnerOutcome(*, provider: str, answer: str | None = None,
                  returncode: int | None = None, timed_out: bool = False,
                  elapsed_ms: int | None = None, stderr: str = "",
                  error: str | None = None, timeout_s: int | None = None,
                  trace_text: str | None = None, usage: Mapping[str, Any] | None = None,
                  cost_usd: float | None = None, model: str | None = None,
                  metadata_extra: Mapping[str, Any] | None = None,
                  metrics_extra: Mapping[str, Any] | None = None,
                  environment: Mapping[str, Any] | None = None,
                  diagnose_returncode: bool = True) -> AnswerOutcome:
    """Strict compatibility factory for the historical constructor spelling."""
    if not isinstance(timed_out, bool):
        raise TypeError("timed_out must be boolean")
    if returncode is not None and type(returncode) is not int:
        raise TypeError("returncode must be an integer or None")
    if answer is not None and not isinstance(answer, str):
        raise TypeError("answer must be a string or None")
    if error is not None and (not isinstance(error, str) or not error.strip()):
        raise ValueError("error must be a non-empty string or None")
    context = OutcomeContext(
        provider=Provider(provider), model=model, elapsed_ms=elapsed_ms, stderr=stderr,
        trace_text="" if trace_text is None else trace_text,
        usage=usage, cost_usd=cost_usd,
        metadata_extra={} if metadata_extra is None else metadata_extra,
        metrics_extra={} if metrics_extra is None else metrics_extra,
        environment=environment, diagnose_returncode=diagnose_returncode,
    )
    if timed_out:
        if returncode not in {None, 124}:
            raise ValueError("timed-out outcome cannot carry a non-timeout returncode")
        return TimedOut(context, timeout_s=timeout_s, reason=error)
    code = 0 if returncode is None else returncode
    if code == 124:
        raise ValueError("returncode 124 requires timed_out=True")
    if code == 127:
        return SpawnFailed(context, reason=error or stderr or "process spawn failed")
    if code != 0 or error:
        return ProviderFailed(
            context, returncode=code, reason=error,
            answer="" if answer is None else answer)
    if not answer:
        return ProviderFailed(context, returncode=0, reason="provider produced no final answer")
    return Completed(context, answer=answer)


def classify_runner_result(*, provider: str, answer: str | None, returncode: int,
                           timed_out: bool, elapsed_ms: int | None, stderr: str = "",
                           error: str | None = None, timeout_s: int | None = None,
                           **context: Any) -> AnswerOutcome:
    return RunnerOutcome(provider=provider, answer=answer, returncode=returncode,
                         timed_out=timed_out, elapsed_ms=elapsed_ms, stderr=stderr,
                         error=error, timeout_s=timeout_s, **context)
