"""Typed, fail-closed contracts for official Gemini CLI output.

Gemini's JSON and stream-JSON formats are provider wire protocols, not the
harness's internal evidence model.  This leaf module validates those protocols
once and constructs only complete observations or explicit failures.  Callers
never infer success from a process exit or reconstruct an answer from arbitrary
trace bytes.
"""
from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from json_contracts import (
    freeze_json_mapping,
    validate_json_text,
    validate_json_value,
)


def _reject_constant(value: str) -> None:
    raise ValueError(f"non-finite numeric constant {value!r}")


def _object_without_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in pairs:
        if key in out:
            raise ValueError(f"duplicate object key {key!r}")
        out[key] = value
    return out


def _strict_json_loads(text: str) -> Any:
    value = json.loads(
        text,
        parse_constant=_reject_constant,
        object_pairs_hook=_object_without_duplicates,
    )
    validate_json_value(value, "Gemini JSON")
    return value


def _nonempty_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty string")
    validate_json_text(value, label)
    return value


def _nonnegative_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{label} must be a non-negative integer")
    return value


def _error_text(value: Any, label: str) -> str:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be an object")
    kind = _nonempty_string(value.get("type"), f"{label}.type")
    message = _nonempty_string(value.get("message"), f"{label}.message")
    return f"{kind}: {message}"


def _terminal_warning(value: str) -> bool:
    normalized = value.casefold()
    return (
        normalized in {
            "maximum session turns exceeded",
            "loop detected, stopping execution",
        }
        or normalized.startswith("agent execution stopped:")
    )


@dataclass(frozen=True)
class GeminiToolCall:
    """One schema-valid, completed Gemini tool lifecycle."""

    call_id: str
    name: str
    parameters: Mapping[str, Any]
    status: str
    output: str | None = None
    error: str | None = None

    def __post_init__(self) -> None:
        _nonempty_string(self.call_id, "Gemini tool call id")
        _nonempty_string(self.name, "Gemini tool name")
        if self.status not in {"success", "error"}:
            raise ValueError("Gemini tool status must be success or error")
        if self.output is not None and not isinstance(self.output, str):
            raise TypeError("Gemini tool output must be text or None")
        if self.output is not None:
            validate_json_text(self.output, "Gemini tool output")
        if self.error is not None and (
                not isinstance(self.error, str) or not self.error.strip()):
            raise ValueError("Gemini tool error must be non-empty text or None")
        if self.error is not None:
            validate_json_text(self.error, "Gemini tool error")
        # The official ToolResultEvent makes ``error`` optional even for an
        # error status, so absence is not an invalid state.  A success carrying
        # an error object is contradictory to the emitter's lifecycle, though.
        if self.status == "success" and self.error is not None:
            raise ValueError("Gemini tool success cannot carry an error")
        object.__setattr__(
            self, "parameters",
            freeze_json_mapping(self.parameters, "Gemini tool parameters"),
        )


@dataclass(frozen=True)
class _StreamStats:
    usage: Mapping[str, Any]
    duration_ms: int
    tool_calls: int
    models: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "usage", freeze_json_mapping(
            self.usage, "Gemini stream usage"))


def _stream_model_tokens(raw: Any, label: str) -> dict[str, int]:
    if not isinstance(raw, Mapping):
        raise ValueError(f"{label} must be an object")
    values = {
        key: _nonnegative_int(raw.get(key), f"{label}.{key}")
        for key in ("total_tokens", "input_tokens", "output_tokens", "cached", "input")
    }
    if values["total_tokens"] < values["input_tokens"] + values["output_tokens"]:
        raise ValueError(
            f"{label}.total_tokens cannot be less than input_tokens + output_tokens")
    if values["input_tokens"] != values["cached"] + values["input"]:
        raise ValueError(
            f"{label}.input_tokens must equal cached + input")
    return values


def _parse_stream_stats(raw: Any) -> _StreamStats:
    if not isinstance(raw, Mapping):
        raise ValueError("Gemini result.stats must be an object")
    totals = _stream_model_tokens(raw, "Gemini result.stats")
    duration_ms = _nonnegative_int(
        raw.get("duration_ms"), "Gemini result.stats.duration_ms")
    tool_calls = _nonnegative_int(
        raw.get("tool_calls"), "Gemini result.stats.tool_calls")
    raw_models = raw.get("models")
    if not isinstance(raw_models, Mapping):
        raise ValueError("Gemini result.stats.models must be an object")
    models: list[str] = []
    model_totals = {key: 0 for key in totals}
    for name, payload in raw_models.items():
        model_name = _nonempty_string(name, "Gemini result.stats model name")
        values = _stream_model_tokens(
            payload, f"Gemini result.stats.models.{model_name}")
        models.append(model_name)
        for key, value in values.items():
            model_totals[key] += value
    if model_totals != totals:
        raise ValueError(
            "Gemini result.stats totals must equal the per-model token totals")
    return _StreamStats(
        usage={
            "input_tokens": totals["input_tokens"],
            "output_tokens": totals["output_tokens"],
            "total_tokens": totals["total_tokens"],
            "cache_read_tokens": totals["cached"],
        },
        duration_ms=duration_ms,
        tool_calls=tool_calls,
        models=tuple(models),
    )


@dataclass(frozen=True)
class GeminiStream:
    """One completely classified Gemini ``stream-json`` observation."""

    records: tuple[Mapping[str, Any], ...] = ()
    session_id: str | None = None
    configured_model: str | None = None
    models: tuple[str, ...] = ()
    answer: str = ""
    usage: Mapping[str, Any] | None = None
    duration_ms: int | None = None
    tool_calls: tuple[GeminiToolCall, ...] = ()
    reported_tool_calls: int | None = None
    warnings: tuple[str, ...] = ()
    provider_error: str | None = None
    protocol_error: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.models, (tuple, list)):
            raise TypeError("Gemini models must be a tuple or list")
        if self.session_id is not None:
            _nonempty_string(self.session_id, "Gemini session id")
        if self.configured_model is not None:
            _nonempty_string(self.configured_model, "Gemini configured model")
        frozen_models = tuple(self.models)
        if any(not isinstance(model, str) or not model.strip()
               for model in frozen_models):
            raise ValueError("Gemini model names must be non-empty strings")
        for model in frozen_models:
            validate_json_text(model, "Gemini model name")
        if not isinstance(self.answer, str):
            raise TypeError("Gemini answer must be text")
        validate_json_text(self.answer, "Gemini answer")
        if self.usage is not None:
            object.__setattr__(self, "usage", freeze_json_mapping(
                self.usage, "Gemini usage"))
        if self.duration_ms is not None:
            _nonnegative_int(self.duration_ms, "Gemini duration_ms")
        frozen_calls = tuple(self.tool_calls)
        if any(not isinstance(call, GeminiToolCall) for call in frozen_calls):
            raise TypeError("Gemini tool_calls must contain GeminiToolCall values")
        if self.provider_error is not None:
            _nonempty_string(self.provider_error, "Gemini provider error")
        if self.protocol_error is not None:
            _nonempty_string(self.protocol_error, "Gemini protocol error")
        frozen_records = tuple(
            freeze_json_mapping(record, f"Gemini stream record {index}")
            for index, record in enumerate(self.records, 1)
        )
        object.__setattr__(self, "records", frozen_records)
        object.__setattr__(self, "models", frozen_models)
        object.__setattr__(self, "tool_calls", frozen_calls)
        if self.reported_tool_calls is not None:
            _nonnegative_int(
                self.reported_tool_calls, "Gemini reported tool_calls")
        if not isinstance(self.warnings, (tuple, list)) or any(
                not isinstance(warning, str) or not warning.strip()
                for warning in self.warnings):
            raise ValueError("Gemini stream warnings must be non-empty strings")
        for warning in self.warnings:
            validate_json_text(warning, "Gemini stream warning")
        object.__setattr__(self, "warnings", tuple(self.warnings))

    @property
    def complete(self) -> bool:
        return (
            self.protocol_error is None
            and self.provider_error is None
            and bool(self.answer.strip())
        )

    @property
    def resolved_model(self) -> str | None:
        return self.models[0] if len(self.models) == 1 else None

    @classmethod
    def invalid(
        cls, message: str, *, records: tuple[Mapping[str, Any], ...] = (),
        session_id: str | None = None, configured_model: str | None = None,
        provider_error: str | None = None,
    ) -> GeminiStream:
        return cls(
            records=records,
            session_id=session_id,
            configured_model=configured_model,
            provider_error=provider_error,
            protocol_error=message,
        )

    @classmethod
    def parse(cls, raw_text: str) -> GeminiStream:
        if not isinstance(raw_text, str):
            raise TypeError("Gemini stream must be text")
        materialized: list[Mapping[str, Any]] = []
        for line_number, line in enumerate(raw_text.splitlines(), 1):
            if not line.strip():
                continue
            try:
                value = _strict_json_loads(line)
            except (json.JSONDecodeError, TypeError, ValueError, RecursionError) as exc:
                return cls.invalid(
                    f"malformed JSONL at line {line_number}: {exc}",
                    records=tuple(materialized),
                )
            if not isinstance(value, Mapping):
                return cls.invalid(
                    f"non-object JSONL at line {line_number}",
                    records=tuple(materialized),
                )
            materialized.append(value)
        records = tuple(materialized)
        if not records:
            return cls.invalid("Gemini stream is empty")
        if records[0].get("type") != "init":
            return cls.invalid("missing init event", records=records)
        init_positions = [index for index, record in enumerate(records)
                          if record.get("type") == "init"]
        if init_positions != [0]:
            return cls.invalid("Gemini stream must contain one initial init event",
                               records=records)
        result_positions = [index for index, record in enumerate(records)
                            if record.get("type") == "result"]
        if result_positions != [len(records) - 1]:
            return cls.invalid("missing terminal result event", records=records)

        try:
            session_id = _nonempty_string(
                records[0].get("session_id"), "Gemini init.session_id")
            configured_model = _nonempty_string(
                records[0].get("model"), "Gemini init.model")
        except (TypeError, ValueError) as exc:
            return cls.invalid(str(exc), records=records)

        open_calls: dict[str, tuple[str, Mapping[str, Any]]] = {}
        completed_calls: list[GeminiToolCall] = []
        seen_call_ids: set[str] = set()
        answer_parts: list[str] = []
        error_events: list[str] = []
        warning_events: list[str] = []
        provider_error: str | None = None
        stats: _StreamStats | None = None
        known = {"init", "message", "tool_use", "tool_result", "error", "result"}
        try:
            for index, record in enumerate(records, 1):
                kind = record.get("type")
                if kind not in known:
                    return cls.invalid(
                        f"unknown event {kind!r} at record {index}", records=records,
                        session_id=session_id, configured_model=configured_model)
                _nonempty_string(record.get("timestamp"),
                                 f"Gemini record {index}.timestamp")
                if kind == "init":
                    continue
                if kind == "message":
                    role = record.get("role")
                    if role not in {"user", "assistant"}:
                        raise ValueError(
                            f"Gemini message {index}.role must be user or assistant")
                    content = record.get("content")
                    if not isinstance(content, str):
                        raise ValueError(
                            f"Gemini message {index}.content must be a string")
                    if "delta" in record and not isinstance(record["delta"], bool):
                        raise ValueError(
                            f"Gemini message {index}.delta must be boolean")
                    if role == "user":
                        answer_parts.clear()
                    else:
                        answer_parts.append(content)
                elif kind == "tool_use":
                    name = _nonempty_string(
                        record.get("tool_name"), f"Gemini tool_use {index}.tool_name")
                    call_id = _nonempty_string(
                        record.get("tool_id"), f"Gemini tool_use {index}.tool_id")
                    parameters = record.get("parameters")
                    if not isinstance(parameters, Mapping):
                        raise ValueError(
                            f"Gemini tool_use {index}.parameters must be an object")
                    if call_id in seen_call_ids:
                        raise ValueError(
                            f"Gemini tool id {call_id!r} is duplicated or reused")
                    seen_call_ids.add(call_id)
                    open_calls[call_id] = (name, parameters)
                    answer_parts.clear()
                elif kind == "tool_result":
                    call_id = _nonempty_string(
                        record.get("tool_id"), f"Gemini tool_result {index}.tool_id")
                    matched = open_calls.pop(call_id, None)
                    if matched is None:
                        raise ValueError(
                            f"Gemini tool_result has no matching tool_use for {call_id!r}")
                    status = record.get("status")
                    if status not in {"success", "error"}:
                        raise ValueError(
                            f"Gemini tool_result {index}.status must be success or error")
                    output = record.get("output")
                    if output is not None and not isinstance(output, str):
                        raise ValueError(
                            f"Gemini tool_result {index}.output must be a string")
                    tool_error = None
                    if record.get("error") is not None:
                        tool_error = _error_text(
                            record["error"], f"Gemini tool_result {index}.error")
                    if status == "success" and tool_error is not None:
                        raise ValueError(
                            f"Gemini tool_result {index} success contradicts its error object")
                    name, parameters = matched
                    completed_calls.append(GeminiToolCall(
                        call_id=call_id, name=name, parameters=parameters,
                        status=status, output=output, error=tool_error,
                    ))
                    answer_parts.clear()
                elif kind == "error":
                    severity = record.get("severity")
                    if severity not in {"warning", "error"}:
                        raise ValueError(
                            f"Gemini error {index}.severity must be warning or error")
                    message = _nonempty_string(
                        record.get("message"), f"Gemini error {index}.message")
                    if severity == "error":
                        error_events.append(message)
                    else:
                        warning_events.append(message)
                else:
                    status = record.get("status")
                    if status not in {"success", "error"}:
                        raise ValueError(
                            "Gemini result.status must be success or error")
                    raw_error = record.get("error")
                    if status == "error":
                        provider_error = (
                            _error_text(raw_error, "Gemini result.error")
                            if raw_error is not None else
                            error_events[-1] if error_events else
                            "Gemini result reported error status"
                        )
                    elif raw_error is not None:
                        raise ValueError(
                            "Gemini result success contradicts its error object")
                    if record.get("stats") is not None:
                        stats = _parse_stream_stats(record["stats"])
        except (TypeError, ValueError) as exc:
            return cls.invalid(
                str(exc), records=records, session_id=session_id,
                configured_model=configured_model,
            )

        if open_calls:
            return cls.invalid(
                f"dangling tool call {next(iter(open_calls))!r}", records=records,
                session_id=session_id, configured_model=configured_model)
        if error_events and provider_error is None:
            # The official max-turn path can emit a severity:error event and
            # still close with result.status=success. That is a provider-level
            # failure observation, not malformed wire protocol.
            provider_error = error_events[-1]
        terminal_warning = next((
            warning for warning in reversed(warning_events)
            if _terminal_warning(warning)), None)
        if terminal_warning is not None and provider_error is None:
            provider_error = terminal_warning
        answer = "".join(answer_parts).strip()
        if provider_error is None and not answer:
            return cls.invalid(
                "missing final answer", records=records, session_id=session_id,
                configured_model=configured_model)
        return cls(
            records=records,
            session_id=session_id,
            configured_model=configured_model,
            models=stats.models if stats is not None else (),
            answer=answer,
            usage=dict(stats.usage) if stats is not None else None,
            duration_ms=stats.duration_ms if stats is not None else None,
            tool_calls=tuple(completed_calls),
            reported_tool_calls=(stats.tool_calls if stats is not None else None),
            warnings=tuple(warning_events),
            provider_error=provider_error,
        )


def _json_model_usage(raw: Any, label: str) -> dict[str, int]:
    if not isinstance(raw, Mapping):
        raise ValueError(f"{label} must be an object")
    tokens = raw.get("tokens")
    if not isinstance(tokens, Mapping):
        raise ValueError(f"{label}.tokens must be an object")
    values = {
        key: _nonnegative_int(tokens.get(key), f"{label}.tokens.{key}")
        for key in ("input", "prompt", "candidates", "total", "cached", "thoughts", "tool")
    }
    if values["prompt"] != values["input"] + values["cached"]:
        raise ValueError(f"{label}.tokens.prompt must equal input + cached")
    minimum_total = values["prompt"] + values["candidates"]
    if values["total"] < minimum_total:
        raise ValueError(
            f"{label}.tokens.total cannot be less than prompt + candidates")
    return {
        "input_tokens": values["prompt"],
        "output_tokens": values["candidates"],
        "total_tokens": values["total"],
        "cache_read_tokens": values["cached"],
    }


def _parse_json_usage(
    raw: Any,
) -> tuple[dict[str, int] | None, tuple[str, ...], int | None]:
    if raw is None:
        return None, (), None
    if not isinstance(raw, Mapping):
        raise ValueError("Gemini JSON stats must be an object")
    models = raw.get("models")
    if not isinstance(models, Mapping):
        raise ValueError("Gemini JSON stats.models must be an object")
    totals = {
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
        "cache_read_tokens": 0,
    }
    names: list[str] = []
    for name, payload in models.items():
        model_name = _nonempty_string(name, "Gemini JSON model name")
        usage = _json_model_usage(payload, f"Gemini JSON stats.models.{model_name}")
        names.append(model_name)
        for key, value in usage.items():
            totals[key] += value
    raw_tools = raw.get("tools")
    tool_calls: int | None = None
    if raw_tools is not None:
        if not isinstance(raw_tools, Mapping):
            raise ValueError("Gemini JSON stats.tools must be an object")
        tool_calls = _nonnegative_int(
            raw_tools.get("totalCalls"),
            "Gemini JSON stats.tools.totalCalls")
    return (totals if names else None), tuple(names), tool_calls


@dataclass(frozen=True)
class GeminiJsonResponse:
    """One completely classified Gemini ``--output-format json`` envelope."""

    response: str = ""
    session_id: str | None = None
    models: tuple[str, ...] = ()
    usage: Mapping[str, Any] | None = None
    tool_calls: int | None = None
    warnings: tuple[str, ...] = ()
    provider_error: str | None = None
    protocol_error: str | None = None
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.models, (tuple, list)):
            raise TypeError("Gemini JSON models must be a tuple or list")
        if not isinstance(self.response, str):
            raise TypeError("Gemini JSON response must be text")
        validate_json_text(self.response, "Gemini JSON response")
        if self.session_id is not None:
            _nonempty_string(self.session_id, "Gemini JSON session id")
        frozen_models = tuple(self.models)
        if any(not isinstance(model, str) or not model.strip()
               for model in frozen_models):
            raise ValueError("Gemini JSON model names must be non-empty strings")
        for model in frozen_models:
            validate_json_text(model, "Gemini JSON model name")
        if self.usage is not None:
            object.__setattr__(self, "usage", freeze_json_mapping(
                self.usage, "Gemini JSON usage"))
        if self.tool_calls is not None:
            _nonnegative_int(self.tool_calls, "Gemini JSON tool_calls")
        if not isinstance(self.warnings, (tuple, list)) or any(
                not isinstance(warning, str) or not warning.strip()
                for warning in self.warnings):
            raise ValueError("Gemini JSON warnings must be non-empty strings")
        for warning in self.warnings:
            validate_json_text(warning, "Gemini JSON warning")
        object.__setattr__(self, "warnings", tuple(self.warnings))
        if self.provider_error is not None:
            _nonempty_string(self.provider_error, "Gemini JSON provider error")
        if self.protocol_error is not None:
            _nonempty_string(self.protocol_error, "Gemini JSON protocol error")
        object.__setattr__(self, "raw", freeze_json_mapping(
            self.raw, "Gemini JSON envelope"))
        object.__setattr__(self, "models", frozen_models)

    @property
    def complete(self) -> bool:
        return (
            self.protocol_error is None
            and self.provider_error is None
            and bool(self.response.strip())
        )

    @property
    def resolved_model(self) -> str | None:
        return self.models[0] if len(self.models) == 1 else None

    @classmethod
    def parse(cls, raw_text: str) -> GeminiJsonResponse:
        if not isinstance(raw_text, str):
            raise TypeError("Gemini JSON output must be text")
        try:
            value = _strict_json_loads(raw_text)
        except (json.JSONDecodeError, TypeError, ValueError, RecursionError) as exc:
            return cls(protocol_error=f"malformed Gemini JSON: {exc}")
        if not isinstance(value, Mapping):
            return cls(protocol_error="Gemini JSON output must be an object")
        raw = dict(value)
        session_id = raw.get("session_id")
        try:
            if session_id is not None:
                session_id = _nonempty_string(
                    session_id, "Gemini JSON session_id")
            provider_error = (
                _error_text(raw["error"], "Gemini JSON error")
                if raw.get("error") is not None else None
            )
            raw_warnings = raw.get("warnings", [])
            if not isinstance(raw_warnings, list):
                raise ValueError("Gemini JSON warnings must be an array")
            warnings = tuple(
                _nonempty_string(warning, "Gemini JSON warning")
                for warning in raw_warnings)
            terminal_warning = next((
                warning for warning in warnings
                if _terminal_warning(warning)), None)
            if provider_error is None and terminal_warning is not None:
                provider_error = terminal_warning
            response = raw.get("response")
            if provider_error is None:
                response = _nonempty_string(
                    response, "Gemini JSON response")
            elif response is None:
                response = ""
            elif not isinstance(response, str):
                raise ValueError("Gemini JSON response must be a string when present")
            usage, models, tool_calls = _parse_json_usage(raw.get("stats"))
        except (TypeError, ValueError) as exc:
            return cls(
                session_id=session_id if isinstance(session_id, str) else None,
                protocol_error=str(exc), raw=raw,
            )
        return cls(
            response=response,
            session_id=session_id,
            models=models,
            usage=usage,
            tool_calls=tool_calls,
            warnings=warnings,
            provider_error=provider_error,
            raw=raw,
        )
