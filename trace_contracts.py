"""Closed lifecycle states for normalized trace events."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any


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


_COMPLETED = {"completed", "complete", "succeeded", "success", "done", "finished"}
_IN_PROGRESS = {"in_progress", "started", "running", "pending", "queued"}
_FAILED = {"failed", "failure", "error", "errored", "cancelled", "canceled", "timed_out", "timeout"}
_TERMINAL_KINDS = {
    "result", "response.completed", "item.completed", "message_end", "turn_end",
    "agent_end", "tool_execution_end", "command", "exec", "exec_command",
    "tool_use", "tool_call", "file_read", "file_write", "usage", "metric",
}
_START_KINDS = {"item.started", "agent_start", "tool_execution_start", "command_start"}


def parse_event_state(raw: Any, *, raw_type: str = "", legacy: bool = False) -> ParsedEventState:
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
    kind = raw_type.strip().casefold()
    if kind in _TERMINAL_KINDS or kind.endswith(".completed") or kind.endswith("_end"):
        return ParsedEventState(EventState.COMPLETED, EventStateSource.PROVIDER_EVENT_KIND)
    if kind in _START_KINDS or kind.endswith(".started") or kind.endswith("_start"):
        return ParsedEventState(EventState.IN_PROGRESS, EventStateSource.PROVIDER_EVENT_KIND)
    if legacy:
        return ParsedEventState(EventState.COMPLETED, EventStateSource.LEGACY_ASSUMED_COMPLETED)
    return ParsedEventState(EventState.UNKNOWN, EventStateSource.UNKNOWN)


def event_is_completed(event: dict[str, Any], *, legacy: bool = False) -> bool:
    parsed = parse_event_state(event.get("status"), legacy=legacy)
    return parsed.state is EventState.COMPLETED
