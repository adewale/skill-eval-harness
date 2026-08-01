"""Closed lifecycle states for normalized trace events."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Literal, TypeAlias

from json_contracts import freeze_json_mapping


class EventState(str, Enum):
    COMPLETED = "completed"
    IN_PROGRESS = "in_progress"
    FAILED = "failed"
    UNKNOWN = "unknown"


class EventStateSource(str, Enum):
    PROVIDER_STATUS = "provider_status"
    PROVIDER_EVENT_KIND = "provider_event_kind"
    LEGACY_ASSUMED_COMPLETED = "legacy_assumed_completed"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class ParsedEventState:
    state: EventState
    source: EventStateSource
    raw: str | None = None


class EventLogState(str, Enum):
    MISSING = "missing"
    INVALID = "invalid"
    LOADED = "loaded"


@dataclass(frozen=True)
class MissingEventLog:
    reason: str = "missing events.json"
    state: Literal[EventLogState.MISSING] = field(
        default=EventLogState.MISSING, init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.reason, str) or not self.reason.strip():
            raise ValueError("missing event log reason must be non-empty")


@dataclass(frozen=True)
class InvalidEventLog:
    reason: str
    state: Literal[EventLogState.INVALID] = field(
        default=EventLogState.INVALID, init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.reason, str) or not self.reason.strip():
            raise ValueError("invalid event log reason must be non-empty")


@dataclass(frozen=True)
class LoadedEventLog:
    events: tuple[Mapping[str, Any], ...]
    schema_version: Literal[1, 2]
    state: Literal[EventLogState.LOADED] = field(
        default=EventLogState.LOADED, init=False)

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version not in {1, 2}:
            raise ValueError("loaded event log schema_version must be exactly 1 or 2")
        if not isinstance(self.events, tuple) or not all(
            isinstance(event, Mapping) for event in self.events
        ):
            raise TypeError("loaded event log events must be a tuple of mappings")
        object.__setattr__(self, "events", tuple(
            freeze_json_mapping(event, f"events[{index}]")
            for index, event in enumerate(self.events)
        ))


EventLogObservation: TypeAlias = MissingEventLog | InvalidEventLog | LoadedEventLog


def parse_event_log(raw: Any) -> InvalidEventLog | LoadedEventLog:
    """Parse a loaded JSON value into one normalized event-log observation."""
    version_present = isinstance(raw, dict) and "schema_version" in raw
    version = raw["schema_version"] if version_present else None
    if version_present and (type(version) is not int or version not in {1, 2}):
        return InvalidEventLog(f"unsupported events.json schema_version {version!r}")
    events = raw.get("events") if isinstance(raw, dict) else raw
    if not isinstance(events, list) or not all(isinstance(event, dict) for event in events):
        return InvalidEventLog("events.json must contain an events list")
    resolved_version: Literal[1, 2] = 2 if type(version) is int and version == 2 else 1
    if resolved_version == 1:
        events = [
            ({
                **event,
                "status": EventState.COMPLETED.value,
                "state_source": EventStateSource.LEGACY_ASSUMED_COMPLETED.value,
            } if event.get("status") is None else event)
            for event in events
        ]
    return LoadedEventLog(tuple(events), resolved_version)


_COMPLETED = {"completed", "complete", "succeeded", "success", "done", "finished"}
_IN_PROGRESS = {"in_progress", "started", "running", "pending", "queued"}
_FAILED = {"failed", "failure", "error", "errored", "cancelled", "canceled", "timed_out", "timeout"}
_TERMINAL_KINDS = {
    "result", "response.completed", "item.completed", "message_end", "turn_end",
    "agent_end", "tool_execution_end", "command_end", "usage", "metric",
}
_START_KINDS = {"item.started", "agent_start", "tool_execution_start", "command_start"}


def parse_event_state(raw: Any, *, raw_type: str = "", legacy: bool = False,
                      status_present: bool = False) -> ParsedEventState:
    """Parse status once; unknown/missing is never silently completed.

    Event kinds whose emission is intrinsically terminal may establish completion.
    Schema-v1 compatibility must be requested explicitly with ``legacy=True``.
    """
    if isinstance(raw, str) and raw.strip():
        normalized = raw.strip().casefold()
        if normalized in _COMPLETED:
            return ParsedEventState(EventState.COMPLETED, EventStateSource.PROVIDER_STATUS, raw)
        if normalized in _IN_PROGRESS:
            return ParsedEventState(EventState.IN_PROGRESS, EventStateSource.PROVIDER_STATUS, raw)
        if normalized in _FAILED:
            return ParsedEventState(EventState.FAILED, EventStateSource.PROVIDER_STATUS, raw)
        return ParsedEventState(EventState.UNKNOWN, EventStateSource.UNKNOWN, raw)
    if status_present:
        # Present-but-null/wrong-typed status is malformed, not equivalent to an
        # absent field whose provider event kind may prove lifecycle state.
        return ParsedEventState(EventState.UNKNOWN, EventStateSource.UNKNOWN,
                                None if raw is None else repr(raw))
    kind = raw_type.strip().casefold()
    if kind in _TERMINAL_KINDS:
        return ParsedEventState(EventState.COMPLETED, EventStateSource.PROVIDER_EVENT_KIND)
    if kind in _START_KINDS:
        return ParsedEventState(EventState.IN_PROGRESS, EventStateSource.PROVIDER_EVENT_KIND)
    if legacy:
        return ParsedEventState(EventState.COMPLETED, EventStateSource.LEGACY_ASSUMED_COMPLETED)
    return ParsedEventState(EventState.UNKNOWN, EventStateSource.UNKNOWN)


def event_is_completed(event: dict[str, Any], *, legacy: bool = False) -> bool:
    parsed = parse_event_state(event.get("status"), legacy=legacy,
                               status_present="status" in event)
    return parsed.state is EventState.COMPLETED
