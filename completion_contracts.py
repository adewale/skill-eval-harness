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

A backend that exposes none of these writes ``unavailable`` (the canonical
spelling in ``observation_contracts``) rather than a guess: missing evidence is
recorded as missing, never as a default value.
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
    UNAVAILABLE = "unavailable"


# A truncated answer or a run stopped by the eval's own step budget is cut off
# by a limit the eval chose. Grading it as a wrong answer blames the model for
# the harness's configuration, so these classes are not scorable.
UNSCORABLE_STOP_CLASSES = frozenset({StopClass.TRUNCATED, StopClass.TURN_LIMIT})

# Values from the Messages API `stop_reason`. Claude Code 2.1.269 copies the
# final one onto its terminal result event (recorded 2026-09-23 in
# tests/fixtures/claude/stream-json.plugin-skill.jsonl), alongside
# `terminal_reason` and `subtype`.
_MESSAGES_API_STOP = {
    "end_turn": StopClass.COMPLETED,
    "stop_sequence": StopClass.COMPLETED,
    "max_tokens": StopClass.TRUNCATED,
    "model_context_window_exceeded": StopClass.TRUNCATED,
    "refusal": StopClass.REFUSED,
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
        if self.stop_class is StopClass.UNAVAILABLE and self.raw is not None:
            raise ValueError("an unavailable stop cannot carry a raw reason")

    @classmethod
    def unavailable(cls, source: str) -> StopObservation:
        return cls(StopClass.UNAVAILABLE, None, source)

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
        return StopObservation.unavailable(source)
    return StopObservation(_MESSAGES_API_STOP.get(value, StopClass.OTHER), value, source)


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
        return StopObservation.unavailable("claude stream has no result event")
    subtype = result_event.get("subtype")
    if isinstance(subtype, str) and subtype == "error_max_turns":
        return StopObservation(StopClass.TURN_LIMIT, f"subtype={subtype}", source)
    return stop_from_messages_api(result_event.get("stop_reason"), source=source)


class ServedModelCheck(str, Enum):
    """Whether the model that answered is the model that was asked for."""

    MATCH = "match"
    MISMATCH = "mismatch"
    # Several distinct models answered and one of them is the requested model:
    # no single model can be credited with the answer.
    MIXED = "mixed"
    UNVERIFIABLE = "unverifiable"
    UNAVAILABLE = "unavailable"
    NOT_REQUESTED = "not_requested"


_FAMILY_ALIASES = frozenset({"haiku", "sonnet", "opus", "fable", "mythos"})
_SNAPSHOT_SUFFIX = re.compile(r"[-@]\d{8}")
# Spellings that name the same model as the bare id: Claude Code's context
# window suffix (``sonnet[1m]``), Bedrock's region and vendor prefix and
# version suffix (``us.anthropic.claude-x-20250929-v1:0``) and Vertex's
# snapshot separator (``claude-x@20250805``). A ``-latest`` alias names
# whichever snapshot the provider serves today, so it is kept on the tail and
# read by ``served_model_check``.
_CONTEXT_SUFFIX = re.compile(r"\[[^\]]*\]$")
_BEDROCK_PREFIX = re.compile(r"^(?:[a-z]+(?:-[a-z]+)*\.)?anthropic\.")
_BEDROCK_VERSION = re.compile(r"-v\d+:\d+$")
_VERTEX_SNAPSHOT = re.compile(r"@(\d{8})$")
# A Claude model id, in the current (``claude-sonnet-4-5``) or the Claude 3
# (``claude-3-5-haiku``) order, with an optional dated snapshot.
_CLAUDE_ID = re.compile(
    r"claude-(?:(?P<family>[a-z]+)-(?P<version>\d{1,2}(?:-\d{1,2})?)"
    r"|(?P<version3>\d{1,2}(?:-\d{1,2})?)-(?P<family3>[a-z]+))(?:-(?P<snapshot>\d{8}))?")


def _model_tail(model: str) -> str:
    """The bare model id behind provider spellings: routing prefixes
    (``anthropic/x``, ``models/x``, ``us.anthropic.x``), a context-window
    suffix, a Bedrock version suffix, and a Vertex ``@date`` snapshot."""
    tail = _CONTEXT_SUFFIX.sub("", model.strip().casefold()).rsplit("/", 1)[-1]
    tail = _BEDROCK_VERSION.sub("", _BEDROCK_PREFIX.sub("", tail))
    return _VERTEX_SNAPSHOT.sub(r"-\1", tail)


def _claude_id(model: str) -> tuple[str, tuple[int, ...], str | None] | None:
    """(family, version, snapshot) of a Claude id; ``4-0`` is version 4."""
    match = _CLAUDE_ID.fullmatch(model)
    if match is None:
        return None
    family = match["family"] or match["family3"]
    version = [int(part) for part in (match["version"] or match["version3"]).split("-")]
    while len(version) > 1 and version[-1] == 0:
        version.pop()
    return family, tuple(version), match["snapshot"]


def served_model_check(requested: str | None, served: str | None) -> ServedModelCheck:
    """Compare a requested model id with the one the provider reported.

    Both ids are first reduced to the bare model id (``_model_tail``). Two
    Claude ids match when family and version agree (``-4-0`` is version 4) and
    the request either names no snapshot or the served one; a request for a
    snapshot answered by an undated id is ``unverifiable``. A ``-latest`` id
    on either side is ``unverifiable`` when the rest agrees, because the
    harness cannot know which snapshot it resolved to. A bare family alias
    (``sonnet``) matches any served id of that family. Other ids match
    themselves or themselves plus a dated snapshot suffix. An alias or id the
    harness cannot resolve is ``unverifiable``, which does not block scoring;
    only a clear mismatch does.
    """
    if served is None or not served.strip():
        return ServedModelCheck.UNAVAILABLE
    if requested is None or not requested.strip():
        return ServedModelCheck.NOT_REQUESTED
    want = _model_tail(requested)
    got = _model_tail(served)
    if want == got:
        return ServedModelCheck.MATCH
    latest = want.endswith("-latest") or got.endswith("-latest")
    want, got = want.removesuffix("-latest"), got.removesuffix("-latest")
    if want == got:
        return ServedModelCheck.UNVERIFIABLE
    want_id, got_id = _claude_id(want), _claude_id(got)
    if want in _FAMILY_ALIASES:
        if got_id is not None:
            return ServedModelCheck.MATCH if got_id[0] == want else ServedModelCheck.MISMATCH
        tokens = set(re.split(r"[-_.@]", got))
        return ServedModelCheck.MATCH if want in tokens else ServedModelCheck.UNVERIFIABLE
    if want_id is not None and got_id is not None:
        if want_id[:2] != got_id[:2]:
            return ServedModelCheck.MISMATCH
        if latest:
            return ServedModelCheck.UNVERIFIABLE
        if want_id[2] is None or want_id[2] == got_id[2]:
            return ServedModelCheck.MATCH
        return (ServedModelCheck.UNVERIFIABLE if got_id[2] is None
                else ServedModelCheck.MISMATCH)
    if want_id is not None or got_id is not None:
        return ServedModelCheck.UNVERIFIABLE
    if got.startswith(want) and _SNAPSHOT_SUFFIX.fullmatch(got[len(want):]):
        return ServedModelCheck.UNVERIFIABLE if latest else ServedModelCheck.MATCH
    if not any(char.isdigit() for char in want):
        return ServedModelCheck.UNVERIFIABLE
    return ServedModelCheck.MISMATCH


@dataclass(frozen=True)
class ServedModel:
    """The model(s) a run reported, and how they compare with the request.

    One rule for every backend: with exactly one reported model, that model is
    credited with the answer and checked against the request. With several,
    no single model can be credited (``served`` is None); the run is
    ``mixed`` when the requested model is among them and a ``mismatch`` when it
    is not. Claude subagent turns are excluded before this point, because a
    subagent may run on another model by design.
    """

    requested: str | None
    reported: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.requested is not None:
            if not isinstance(self.requested, str) or not self.requested.strip():
                raise ValueError("requested model must be a non-empty string or None")
            validate_json_text(self.requested, "requested model")
        if not isinstance(self.reported, tuple) or not all(
                isinstance(item, str) and item.strip() for item in self.reported):
            raise ValueError("reported models must be a tuple of non-empty strings")
        for item in self.reported:
            validate_json_text(item, "served model")
        if len(set(self.reported)) != len(self.reported):
            raise ValueError("reported models must be distinct")

    @classmethod
    def observe(cls, requested: str | None, reported_in_order: Iterable[object]) -> ServedModel:
        """Keep each distinct non-empty reported model, in first-seen order."""
        reported = [item.strip() for item in reported_in_order
                    if isinstance(item, str) and item.strip()]
        return cls(requested, tuple(dict.fromkeys(reported)))

    @property
    def served(self) -> str | None:
        return self.reported[0] if len(self.reported) == 1 else None

    @property
    def check(self) -> ServedModelCheck:
        if len(self.reported) <= 1:
            return served_model_check(self.requested, self.served)
        if self.requested is None or not self.requested.strip():
            return ServedModelCheck.NOT_REQUESTED
        checks = {served_model_check(self.requested, item) for item in self.reported}
        if ServedModelCheck.MATCH in checks or ServedModelCheck.UNVERIFIABLE in checks:
            return ServedModelCheck.MIXED
        return ServedModelCheck.MISMATCH

    def as_metadata(self) -> dict[str, Any]:
        return {
            "requested_model": self.requested,
            "served_model": self.served,
            "served_models": list(self.reported),
            "served_model_check": self.check.value,
        }


# Every effort level some answer backend accepts: Claude Code's `--effort`
# takes `low` through `max`, and Codex's `model_reasoning_effort` also takes
# `minimal`. Each backend's `effort_levels` names its own subset, and a level
# outside it is refused before any run.
EFFORT_LEVELS = ("minimal", "low", "medium", "high", "xhigh", "max")
BACKEND_DEFAULT = "backend_default"


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
