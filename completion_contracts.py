"""How a run ended, which model served it, and what effort it ran at.

Three facts decide whether a graded answer measures the model at all, and the
harness used to record none of them:

* **Stop reason.** An answer cut off at an output-token limit exits 0 and
  reads like a wrong answer. A refusal reads like a capability miss. Both are
  now recorded as a closed ``StopClass`` next to the provider's raw value.
* **Served model.** A provider can answer with a different model than the one
  requested (a fallback, a capacity reroute). A score from the wrong model
  measures nothing, so ``served_model_check`` compares the two and a clear
  mismatch makes the run unscorable.
* **Effort.** Default effort differs by model and by CLI, so two tiers
  compared "at defaults" may run at different effort. ``EffortSetting``
  records what was requested and how it was applied, and a with/without pair
  whose arms ran at different effort is blocked instead of compared.

A backend that exposes none of these writes ``unobserved`` rather than a
guess: missing evidence is recorded as missing, never as a default value.
"""
from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Any

from json_contracts import validate_json_text


class StopClass(str, Enum):
    """Closed vocabulary for why the model stopped producing its answer."""

    COMPLETED = "completed"
    TRUNCATED = "truncated"
    TURN_LIMIT = "turn_limit"
    REFUSED = "refused"
    OTHER = "other"
    UNOBSERVED = "unobserved"


# A truncated answer or a run stopped by the eval's own step budget is cut off
# by a limit the eval chose. Grading it as a wrong answer blames the model for
# the harness's configuration, so these classes are not scorable.
UNSCORABLE_STOP_CLASSES = frozenset({StopClass.TRUNCATED, StopClass.TURN_LIMIT})

# Values from the Messages API `stop_reason`. Claude Code 2.1.269 copies the
# final one onto its terminal result event (recorded 2026-09-23 in PR #85's
# plugin-eval fixture), alongside `terminal_reason` and `subtype`.
_MESSAGES_API_STOP = {
    "end_turn": StopClass.COMPLETED,
    "stop_sequence": StopClass.COMPLETED,
    "max_tokens": StopClass.TRUNCATED,
    "model_context_window_exceeded": StopClass.TRUNCATED,
    "refusal": StopClass.REFUSED,
}
# OpenAI-style `finish_reason` (Jetty chat completions, some CLI streams).
_FINISH_REASON_STOP = {
    "stop": StopClass.COMPLETED,
    "length": StopClass.TRUNCATED,
    "content_filter": StopClass.REFUSED,
}


@dataclass(frozen=True)
class StopObservation:
    """One run's stop reason, normalized, with the provider's own words kept."""

    stop_class: StopClass
    raw: str | None
    source: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "stop_class", StopClass(self.stop_class))
        if self.raw is not None:
            if not isinstance(self.raw, str) or not self.raw.strip():
                raise ValueError("raw stop reason must be a non-empty string or None")
            validate_json_text(self.raw, "raw stop reason")
        if not isinstance(self.source, str) or not self.source.strip():
            raise ValueError("stop observation requires a source")
        if self.stop_class is StopClass.UNOBSERVED and self.raw is not None:
            raise ValueError("an unobserved stop cannot carry a raw reason")

    @classmethod
    def unobserved(cls, source: str) -> StopObservation:
        return cls(StopClass.UNOBSERVED, None, source)

    @property
    def scorable(self) -> bool:
        return self.stop_class not in UNSCORABLE_STOP_CLASSES

    def as_metadata(self) -> dict[str, Any]:
        return {
            "stop_class": self.stop_class.value,
            "stop_reason": self.raw,
            "stop_source": self.source,
        }


def stop_from_messages_api(value: object, *, source: str) -> StopObservation:
    """Normalize a Messages-API-style ``stop_reason`` string."""
    if not isinstance(value, str) or not value.strip():
        return StopObservation.unobserved(source)
    return StopObservation(_MESSAGES_API_STOP.get(value, StopClass.OTHER), value, source)


def stop_from_finish_reason(value: object, *, source: str) -> StopObservation:
    """Normalize an OpenAI-style ``finish_reason`` string."""
    if not isinstance(value, str) or not value.strip():
        return StopObservation.unobserved(source)
    return StopObservation(_FINISH_REASON_STOP.get(value, StopClass.OTHER), value, source)


def claude_result_stop(result_event: Mapping[str, Any] | None) -> StopObservation:
    """Stop reason from Claude Code's terminal ``type: result`` event.

    Claude Code 2.1.x writes ``stop_reason`` (Messages API vocabulary) and a
    ``subtype`` on that event. Neither field is in the public stream-json
    reference, so both are read as optional observations. A max-turns subtype
    outranks the last message's stop reason: the run was stopped by the step
    budget, whatever the final turn said.
    """
    source = "claude-result-event"
    if not isinstance(result_event, Mapping):
        return StopObservation.unobserved("claude stream has no result event")
    subtype = result_event.get("subtype")
    if isinstance(subtype, str) and subtype == "error_max_turns":
        return StopObservation(StopClass.TURN_LIMIT, f"subtype={subtype}", source)
    return stop_from_messages_api(result_event.get("stop_reason"), source=source)


class ServedModelCheck(str, Enum):
    """Whether the model that answered is the model that was asked for."""

    MATCH = "match"
    MISMATCH = "mismatch"
    UNVERIFIABLE = "unverifiable"
    UNOBSERVED = "unobserved"
    NOT_REQUESTED = "not-requested"


_FAMILY_ALIASES = frozenset({"haiku", "sonnet", "opus", "fable", "mythos"})
_SNAPSHOT_SUFFIX = re.compile(r"[-@]\d{8}")


def _model_tail(model: str) -> str:
    """Drop provider routing prefixes: ``anthropic/x``, ``models/x``, ``anthropic.x``."""
    tail = model.strip().casefold().rsplit("/", 1)[-1]
    return tail.removeprefix("anthropic.")


def served_model_check(requested: str | None, served: str | None) -> ServedModelCheck:
    """Compare a requested model id with the one the provider reported.

    A full id matches itself or itself plus a dated snapshot suffix
    (``claude-haiku-4-5`` -> ``claude-haiku-4-5-20251001``). A bare family
    alias (``sonnet``) matches any served id containing that family. Any other
    alias the harness cannot resolve is ``unverifiable``, which does not block
    scoring; only a clear mismatch does.
    """
    if served is None or not served.strip():
        return ServedModelCheck.UNOBSERVED
    if requested is None or not requested.strip():
        return ServedModelCheck.NOT_REQUESTED
    want = _model_tail(requested)
    got = _model_tail(served)
    if want == got:
        return ServedModelCheck.MATCH
    if got.startswith(want) and _SNAPSHOT_SUFFIX.fullmatch(got[len(want):]):
        return ServedModelCheck.MATCH
    if not any(char.isdigit() for char in want):
        if want in _FAMILY_ALIASES:
            tokens = set(re.split(r"[-_.@]", got))
            return (ServedModelCheck.MATCH if want in tokens
                    else ServedModelCheck.MISMATCH)
        return ServedModelCheck.UNVERIFIABLE
    return ServedModelCheck.MISMATCH


@dataclass(frozen=True)
class ServedModel:
    """The model(s) a run reported, and how they compare with the request."""

    requested: str | None
    served: str | None
    all_served: tuple[str, ...]

    def __post_init__(self) -> None:
        for label, value in (("requested", self.requested), ("served", self.served)):
            if value is not None:
                if not isinstance(value, str) or not value.strip():
                    raise ValueError(f"{label} model must be a non-empty string or None")
                validate_json_text(value, f"{label} model")
        if not isinstance(self.all_served, tuple) or not all(
                isinstance(item, str) and item.strip() for item in self.all_served):
            raise ValueError("all_served must be a tuple of non-empty strings")

    @classmethod
    def observe(cls, requested: str | None, served_in_order: Iterable[object]) -> ServedModel:
        """The last reported model produced the final answer; keep the rest."""
        reported = [item for item in served_in_order if isinstance(item, str) and item.strip()]
        return cls(requested, reported[-1] if reported else None,
                   tuple(dict.fromkeys(reported)))

    @property
    def check(self) -> ServedModelCheck:
        return served_model_check(self.requested, self.served)

    def as_metadata(self) -> dict[str, Any]:
        return {
            "requested_model": self.requested,
            "served_model": self.served,
            "served_models": list(self.all_served),
            "served_model_check": self.check.value,
        }


# Effort levels documented for Claude Code's `--effort`; Codex's
# `model_reasoning_effort` also accepts `minimal`.
EFFORT_LEVELS = ("minimal", "low", "medium", "high", "xhigh", "max")
BACKEND_DEFAULT = "backend-default"


@dataclass(frozen=True)
class EffortSetting:
    """What effort a run asked for and how the backend applied it.

    ``requested`` is None when the run used the backend's default, which is
    itself worth recording: defaults differ between models and CLI versions,
    so two runs "at default" are not known to share an effort level.
    """

    requested: str | None
    applied_by: str

    def __post_init__(self) -> None:
        if self.requested is not None and self.requested not in EFFORT_LEVELS:
            raise ValueError(
                f"effort must be one of {', '.join(EFFORT_LEVELS)}; got {self.requested!r}")
        if not isinstance(self.applied_by, str) or not self.applied_by.strip():
            raise ValueError("effort setting requires applied_by")
        if self.requested is None and self.applied_by != BACKEND_DEFAULT:
            raise ValueError("an unrequested effort must be applied by the backend default")

    @classmethod
    def default(cls) -> EffortSetting:
        return cls(None, BACKEND_DEFAULT)

    @property
    def identity(self) -> str:
        """The value pairs compare: the requested level or the default marker."""
        return self.requested if self.requested is not None else BACKEND_DEFAULT

    def as_metadata(self) -> dict[str, Any]:
        return {"effort": {"requested": self.requested, "applied_by": self.applied_by}}


def effort_identity(row: Mapping[str, Any]) -> str | None:
    """The effort a result row ran at, or None when the run predates recording."""
    effort = row.get("effort")
    if isinstance(effort, Mapping):
        requested = effort.get("requested")
        if requested is None:
            return BACKEND_DEFAULT
        if isinstance(requested, str):
            return requested
    return None


def completion_unscorable_reason(metadata: Mapping[str, Any]) -> str | None:
    """Why recorded completion evidence makes a run unscorable, if it does."""
    stop_class = metadata.get("stop_class")
    if stop_class in {item.value for item in UNSCORABLE_STOP_CLASSES}:
        return f"stopped:{stop_class}"
    if metadata.get("served_model_check") == ServedModelCheck.MISMATCH.value:
        return "served_model_mismatch"
    return None
