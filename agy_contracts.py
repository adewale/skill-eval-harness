"""Typed, fail-closed contracts for Google Antigravity (``agy``) CLI output.

``agy``'s ``stream-json`` format is a provider wire protocol, not the harness's
internal evidence model.  This leaf module validates that protocol once and
constructs only complete observations or explicit failures.  Callers never infer
success from a process exit, never reconstruct an answer from arbitrary trace
bytes, and never receive a zero-valued measurement in place of a missing one.

Three properties are load-bearing and each closes a defect the earlier adapter
shipped:

* **Search is not a read.**  ``AGY_FILE_READ_TOOLS`` and ``AGY_SEARCH_TOOLS`` are
  disjoint, and only the former can produce path evidence.  A ``grep_search``
  scoped at a mounted ``SKILL.md`` used to normalize to the same record as a
  ``view_file`` of it, so searching for a skill could be recorded as activating
  one.
* **Absent telemetry is not zero.**  Usage is a three-state value.  ``agy``
  emits a full block of zeroed counters when authentication fails, which is a
  run that never reached a model rather than a run that spent nothing.
* **A nonzero exit does not erase a diagnosis.**  The provider error is taken
  from the parsed stream regardless of return code.
"""
from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from json_contracts import (
    freeze_json_mapping,
    validate_json_text,
    validate_json_value,
)

# ---------------------------------------------------------------------------
# Closed vocabularies, observed across real agy 1.1.8 and 1.1.9 runs.
#
# Each is deliberately closed: a value a future release adds fails loudly on the
# next run rather than being dropped and mis-scored, and the fix is to add the
# name here once its payload is understood.
# ---------------------------------------------------------------------------

AGY_EVENT_TYPES = frozenset({"init", "step_update", "result"})
AGY_STEP_STATES = frozenset({"ACTIVE", "DONE"})
AGY_STEP_TYPES = frozenset({"tool", "user_input", "agent_response", "checkpoint",
                            "system_message", "unknown"})
AGY_SUCCESS_STATUSES = frozenset({"SUCCESS", "COMPLETED", "OK"})
AGY_DONE_STATE = "DONE"

# Tools that execute a shell command; their CommandLine is what `command_ran`
# assertions match against once normalized.
AGY_SHELL_TOOLS = ("run_command",)

# Tools that open one specific file.  This partition alone can carry path
# evidence, and it alone can establish that a skill was read.
AGY_FILE_READ_TOOLS = ("view_file",)

# Discovery tools.  A search states an interest in a path; it does not establish
# that anything was opened, so these carry no path and are never a file read.
# `skill_search` sits here rather than in a category of its own until a real
# capture settles whether it carries stronger signal than a generic grep.
AGY_SEARCH_TOOLS = ("grep_search", "find_by_name", "list_dir", "code_search",
                    "skill_search")

# Tools that modify files.  Without these a run that wrote files publishes
# `file_writes: 0` alongside complete trace evidence, which reads as a model
# that changed nothing.  `write_to_file` is not hypothetical: a sandbox-escape
# probe on agy 1.1.8 used it in preference to the shell.
AGY_WRITE_TOOLS = ("write_to_file", "replace_file_content",
                   "multi_replace_file_content", "notebook_edit")

# Genuinely neither shell, read, search, nor write.  Listed explicitly so a tool
# agy advertises cannot default into this bucket without someone deciding it
# belongs here -- a write landing here silently is how `file_writes: 0` was
# reported for runs that wrote files.
#
# `sed_file` sits here under protest: agy advertises it, but whether it edits in
# place or only prints could not be established, and an unverified read
# classification would both inflate `file_reads` and hide a write.
AGY_GENERIC_TOOLS = (
    "sed_file",
    # Reads a URL, not a file.  Filed as a `file_read` it inflated `file_reads`
    # and published the URL as an OTel `file.path`, so telemetry claimed a
    # filesystem access that never happened.
    "read_url_content",
    "ask_permission", "ask_question", "call_mcp_tool", "command_status",
    "define_subagent", "delete_knowledge", "finish", "generate_image",
    "invoke_subagent", "list_permissions", "list_resources", "manage_inbox",
    "manage_subagents", "manage_task", "moma_search", "notebook_execution",
    "read_resource", "schedule", "search_web", "send_command_input",
    "send_message", "wait", "wait_5_seconds",
    "browser_click_element", "browser_drag_pixel_to_pixel", "browser_get_dom",
    "browser_get_network_request", "browser_input",
    "browser_list_network_requests", "browser_mouse_down", "browser_mouse_up",
    "browser_move_mouse", "browser_press_key", "browser_refresh_page",
    "browser_resize_window", "browser_scroll", "browser_scroll_dom",
    "browser_select_option", "browser_subagent",
    "capture_browser_console_logs", "capture_browser_screenshot",
    "click_browser_pixel", "execute_browser_javascript", "list_browser_pages",
    "open_browser_url", "read_browser_page",
)

AGY_CLASSIFIED_TOOLS = frozenset((
    *AGY_SHELL_TOOLS, *AGY_FILE_READ_TOOLS, *AGY_SEARCH_TOOLS,
    *AGY_WRITE_TOOLS, *AGY_GENERIC_TOOLS,
))


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
    validate_json_value(value, "agy JSON")
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


# ---------------------------------------------------------------------------
# Telemetry as an explicit three-state value.
# ---------------------------------------------------------------------------

AGY_USAGE_COUNTERS = ("input_tokens", "output_tokens", "thinking_tokens",
                      "cache_read_tokens", "total_tokens")


@dataclass(frozen=True)
class AgyUsagePresent:
    """Token counters the provider reported for a run that reached a model."""

    counters: Mapping[str, int]

    def __post_init__(self) -> None:
        if not isinstance(self.counters, Mapping) or not self.counters:
            raise ValueError("present usage needs at least one counter")
        for key, value in self.counters.items():
            _nonempty_string(key, "agy usage counter name")
            _nonnegative_int(value, f"agy usage {key}")
        if not any(self.counters.get(key) for key in AGY_USAGE_COUNTERS):
            raise ValueError("all-zero usage is absent telemetry, not a measurement")
        object.__setattr__(self, "counters", freeze_json_mapping(
            self.counters, "agy usage"))


@dataclass(frozen=True)
class AgyUsageAbsent:
    """No usable token accounting.  Never a zero-valued measurement."""

    reason: str

    def __post_init__(self) -> None:
        _nonempty_string(self.reason, "agy absent-usage reason")


@dataclass(frozen=True)
class AgyUsageInvalid:
    """Token accounting that contradicts itself, so it cannot be trusted."""

    reason: str

    def __post_init__(self) -> None:
        _nonempty_string(self.reason, "agy invalid-usage reason")


AgyUsage = AgyUsagePresent | AgyUsageAbsent | AgyUsageInvalid


def parse_agy_usage(raw: Any) -> AgyUsage:
    """Classify one ``usage`` block into present, absent, or invalid.

    An all-zero block is *absent*: agy emits exactly that when authentication
    fails, and a run that never reached a model has no token measurement to
    report.  Publishing it as ``provider_reported`` zero would record a
    measurement that was never taken.
    """
    if raw is None:
        return AgyUsageAbsent("no usage block in result event")
    if not isinstance(raw, Mapping):
        return AgyUsageInvalid("usage block is not an object")
    counters: dict[str, int] = {}
    for key, value in raw.items():
        if not isinstance(key, str) or not key.strip():
            return AgyUsageInvalid("usage counter name is not a string")
        if isinstance(value, bool) or not isinstance(value, int):
            return AgyUsageInvalid(f"usage {key} is not an integer")
        if value < 0:
            return AgyUsageInvalid(f"usage {key} is negative")
        counters[key] = value
    if not counters:
        return AgyUsageAbsent("usage block is empty")
    if not any(counters.get(key) for key in AGY_USAGE_COUNTERS):
        return AgyUsageAbsent(
            "every reported counter is zero, so no model work was measured")
    total = counters.get("total_tokens")
    served = counters.get("input_tokens")
    produced = counters.get("output_tokens")
    if total is not None and (served is not None or produced is not None):
        parts = (served or 0) + (produced or 0)
        if total < parts:
            return AgyUsageInvalid(
                "total_tokens is less than input_tokens + output_tokens")
    return AgyUsagePresent(counters=counters)


# ---------------------------------------------------------------------------
# Model identity kept as three separate facts.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AgyModelIdentity:
    """What was asked for, what the launcher implied, and what agy reported.

    These are three different claims and collapsing them invents certainty.  A
    run that reported no model must not be labelled with the model the harness
    requested, and a run that reported two must not silently become the first.
    """

    requested: str | None = None
    configured: str | None = None
    reported: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for label, value in (("requested", self.requested),
                             ("configured", self.configured)):
            if value is not None:
                _nonempty_string(value, f"agy {label} model")
        if not isinstance(self.reported, tuple):
            raise TypeError("agy reported models must be a tuple")
        for name in self.reported:
            _nonempty_string(name, "agy reported model")

    @property
    def resolved(self) -> str | None:
        """The single reported model, or None for zero or several."""
        return self.reported[0] if len(self.reported) == 1 else None

    @property
    def ambiguous(self) -> bool:
        return len(self.reported) > 1


# ---------------------------------------------------------------------------
# Tool evidence.  Search is not a read.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AgyFileRead:
    """A completed open of one specific file."""

    path: str

    def __post_init__(self) -> None:
        _nonempty_string(self.path, "agy file read path")


@dataclass(frozen=True)
class AgySearch:
    """A completed discovery operation.

    Carries the tool name only.  A search states an interest in a location; it
    is not evidence that anything there was opened, so it deliberately has no
    path field for a caller to mistake for one.
    """

    tool: str

    def __post_init__(self) -> None:
        _nonempty_string(self.tool, "agy search tool name")


@dataclass(frozen=True)
class AgyShellCommand:
    command: str

    def __post_init__(self) -> None:
        _nonempty_string(self.command, "agy shell command")


@dataclass(frozen=True)
class AgyFileWrite:
    path: str

    def __post_init__(self) -> None:
        _nonempty_string(self.path, "agy file write path")


@dataclass(frozen=True)
class AgyGenericCall:
    tool: str

    def __post_init__(self) -> None:
        _nonempty_string(self.tool, "agy tool name")


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

    def __post_init__(self) -> None:
        _nonempty_string(self.path, "agy activated skill path")


@dataclass(frozen=True)
class AgySkillNotActivated:
    """A complete observation in which the skill was demonstrably not read."""


@dataclass(frozen=True)
class AgySkillObservationUnavailable:
    """Neither activation nor a clean negative could be established.

    This is the state that keeps the trigger matrix honest.  A run that searched
    the skill directory without opening it, a truncated stream, a failed read,
    or a denied consent prompt are all cases where the harness does not know
    what happened -- and "do not know" must not be scored as "did not trigger".
    """

    reason: str

    def __post_init__(self) -> None:
        _nonempty_string(self.reason, "agy unavailable-observation reason")


AgySkillObservation = (
    AgySkillActivated | AgySkillNotActivated | AgySkillObservationUnavailable
)


# ---------------------------------------------------------------------------
# Stream parsing.
# ---------------------------------------------------------------------------

_PATH_PARAMETERS = ("AbsolutePath", "TargetFile", "Path", "path", "file",
                    "FilePath")


def _read_path(params: Mapping[str, Any]) -> str:
    """The file path a read or write tool acted on.

    agy spells these ``AbsolutePath``, ``TargetFile`` and similar per tool, so
    fall back to any ``*Path``/``*File`` parameter rather than enumerating a
    list that the next tool breaks.
    """
    for key in _PATH_PARAMETERS:
        value = params.get(key)
        if isinstance(value, str) and value.strip():
            return value
    for key, value in params.items():
        if (isinstance(value, str) and value.strip()
                and key.endswith(("Path", "File", "path", "file"))):
            return value
    return ""


@dataclass(frozen=True)
class AgyStream:
    """One completely classified ``agy --output-format stream-json`` observation."""

    records: tuple[Mapping[str, Any], ...] = ()
    conversation_id: str | None = None
    model: AgyModelIdentity = AgyModelIdentity()
    answer: str = ""
    usage: AgyUsage = AgyUsageAbsent("stream not parsed")
    tools: tuple[AgyToolEvidence, ...] = ()
    advertised_tools: tuple[str, ...] = ()
    incomplete_tools: tuple[str, ...] = ()
    status: str | None = None
    provider_error: str | None = None
    protocol_error: str | None = None

    def __post_init__(self) -> None:
        if self.conversation_id is not None and not isinstance(
                self.conversation_id, str):
            raise TypeError("agy conversation id must be text or None")
        if not isinstance(self.answer, str):
            raise TypeError("agy answer must be text")
        validate_json_text(self.answer, "agy answer")
        for label, value in (("provider error", self.provider_error),
                             ("protocol error", self.protocol_error)):
            if value is not None:
                _nonempty_string(value, f"agy {label}")
        object.__setattr__(self, "records", tuple(
            freeze_json_mapping(record, f"agy stream record {index}")
            for index, record in enumerate(self.records, 1)))

    @property
    def complete(self) -> bool:
        """A run that produced an answer and neither kind of error."""
        return (self.protocol_error is None
                and self.provider_error is None
                and bool(self.answer.strip()))

    @property
    def unclassified_tools_advertised(self) -> tuple[str, ...]:
        """Advertised tool names this module does not classify."""
        return tuple(sorted(
            name for name in self.advertised_tools
            if name not in AGY_CLASSIFIED_TOOLS))

    @classmethod
    def invalid(cls, message: str, *,
                records: tuple[Mapping[str, Any], ...] = (),
                conversation_id: str | None = None,
                provider_error: str | None = None) -> AgyStream:
        return cls(records=records, conversation_id=conversation_id,
                   usage=AgyUsageAbsent("stream did not parse"),
                   provider_error=provider_error, protocol_error=message)

    @classmethod
    def parse(cls, raw_text: str, *, returncode: int = 0,
              requested_model: str | None = None,
              configured_model: str | None = None) -> AgyStream:
        """Classify one stream.

        ``returncode`` is accepted but deliberately does **not** gate the
        provider error.  agy exits 1 on an authentication failure while still
        emitting a well-formed ``result`` event, and that error string is the
        only diagnosis the run produced.  It is equally true that a zero exit is
        not proof of completion, which is why a truncated stream still yields a
        protocol error.
        """
        if not isinstance(raw_text, str):
            raise TypeError("agy stream must be text")

        materialized: list[Mapping[str, Any]] = []
        truncated_at: int | None = None
        for line_number, line in enumerate(raw_text.splitlines(), 1):
            if not line.strip():
                continue
            try:
                value = _strict_json_loads(line)
            except (json.JSONDecodeError, TypeError, ValueError,
                    RecursionError):
                truncated_at = line_number
                continue
            if not isinstance(value, Mapping):
                return cls.invalid(f"non-object JSONL at line {line_number}",
                                   records=tuple(materialized))
            materialized.append(value)

        records = tuple(materialized)
        if not records:
            return cls.invalid("agy stream is empty")

        identity = AgyModelIdentity(requested=requested_model,
                                    configured=configured_model)
        reported: list[str] = []
        advertised: list[str] = []
        conversation_id: str | None = None
        answer = ""
        status: str | None = None
        provider_error: str | None = None
        raw_usage: Any = None
        tools: list[AgyToolEvidence] = []
        incomplete: list[str] = []
        saw_result = False

        try:
            for index, record in enumerate(records, 1):
                event = record.get("event")
                if not isinstance(event, str) or event not in AGY_EVENT_TYPES:
                    return cls.invalid(
                        f"unknown event {event!r} at record {index}",
                        records=records, conversation_id=conversation_id)
                raw_conversation = record.get("conversation_id")
                if isinstance(raw_conversation, str) and raw_conversation:
                    conversation_id = raw_conversation

                if event == "init":
                    init = record.get("init")
                    if not isinstance(init, Mapping):
                        raise ValueError(f"agy init {index} must carry an object")
                    name = init.get("model")
                    if name is not None:
                        reported.append(_nonempty_string(
                            name, f"agy init {index}.model"))
                    raw_tools = init.get("tools")
                    if raw_tools is not None:
                        if not isinstance(raw_tools, list):
                            raise ValueError(
                                f"agy init {index}.tools must be a list")
                        advertised.extend(
                            _nonempty_string(tool, f"agy init {index} tool name")
                            for tool in raw_tools)
                elif event == "step_update":
                    step = record.get("step_update")
                    if not isinstance(step, Mapping):
                        raise ValueError(
                            f"agy step_update {index} must carry an object")
                    step_type = step.get("step_type")
                    if (not isinstance(step_type, str)
                            or step_type not in AGY_STEP_TYPES):
                        raise ValueError(
                            f"agy step_update {index} has unknown step_type "
                            f"{step_type!r}")
                    state = step.get("state")
                    if not isinstance(state, str) or state not in AGY_STEP_STATES:
                        raise ValueError(
                            f"agy step_update {index} has unknown state {state!r}")
                    if step_type != "tool":
                        continue
                    name = _nonempty_string(
                        step.get("tool_name"), f"agy step_update {index}.tool_name")
                    if state != AGY_DONE_STATE:
                        # A tool that started is not a tool that finished.  It is
                        # recorded so an observation can be marked incomplete,
                        # never translated into evidence.
                        incomplete.append(name)
                        continue
                    info = step.get("tool_info")
                    if info is not None and not isinstance(info, Mapping):
                        raise ValueError(
                            f"agy step_update {index}.tool_info must be an object")
                    params = info.get("parameters") if isinstance(
                        info, Mapping) else None
                    if params is not None and not isinstance(params, Mapping):
                        raise ValueError(
                            f"agy step_update {index} parameters must be an object")
                    tools.append(_tool_evidence(
                        name, params if isinstance(params, Mapping) else {},
                        incomplete))
                else:
                    saw_result = True
                    result = record.get("result")
                    if not isinstance(result, Mapping):
                        raise ValueError(
                            f"agy result {index} must carry an object")
                    raw_status = result.get("status")
                    if raw_status is not None:
                        status = _nonempty_string(
                            raw_status, f"agy result {index}.status")
                    response = result.get("response")
                    if response is not None:
                        if not isinstance(response, str):
                            raise ValueError(
                                f"agy result {index}.response must be a string")
                        validate_json_text(response, "agy answer")
                        answer = response
                    error = result.get("error")
                    if error is not None:
                        provider_error = _nonempty_string(
                            error, f"agy result {index}.error")
                    raw_usage = result.get("usage")
        except (TypeError, ValueError) as exc:
            return cls.invalid(str(exc), records=records,
                               conversation_id=conversation_id,
                               provider_error=provider_error)

        identity = AgyModelIdentity(requested=requested_model,
                                    configured=configured_model,
                                    reported=tuple(reported))
        usage = parse_agy_usage(raw_usage)

        protocol_error: str | None = None
        if truncated_at is not None:
            protocol_error = f"malformed JSONL at line {truncated_at}"
        elif not saw_result:
            protocol_error = "missing terminal result event"
        # A non-success status is the provider's own verdict, not a parse
        # failure, so it becomes a provider error when one is not already
        # present rather than masquerading as a protocol error.
        elif (status is not None and provider_error is None
              and status.upper() not in AGY_SUCCESS_STATUSES):
            provider_error = f"agy reported status {status}"

        return cls(
            records=records,
            conversation_id=conversation_id,
            model=identity,
            answer=answer,
            usage=usage,
            tools=tuple(tools),
            advertised_tools=tuple(advertised),
            incomplete_tools=tuple(incomplete),
            status=status,
            provider_error=provider_error,
            protocol_error=protocol_error,
        )


def _tool_evidence(name: str, params: Mapping[str, Any],
                   incomplete: list[str]) -> AgyToolEvidence:
    """One completed tool step as evidence of its own kind.

    A search never becomes a read, and a read whose path is missing becomes an
    unclassified call rather than a read of nowhere.
    """
    if name in AGY_SEARCH_TOOLS:
        return AgySearch(tool=name)
    if name in AGY_FILE_READ_TOOLS:
        path = _read_path(params)
        if not path:
            incomplete.append(name)
            return AgyGenericCall(tool=name)
        return AgyFileRead(path=path)
    if name in AGY_WRITE_TOOLS:
        path = _read_path(params)
        if not path:
            incomplete.append(name)
            return AgyGenericCall(tool=name)
        return AgyFileWrite(path=path)
    if name in AGY_SHELL_TOOLS:
        command = params.get("CommandLine")
        if not isinstance(command, str) or not command.strip():
            incomplete.append(name)
            return AgyGenericCall(tool=name)
        return AgyShellCommand(command=command)
    return AgyGenericCall(tool=name)


def observe_skill_activation(stream: AgyStream,
                             skill_path: str) -> AgySkillObservation:
    """Whether this run demonstrably read the mounted skill.

    Activation is a **completed read of the exact mounted path** and nothing
    else.  Anything that leaves the question open -- a truncated stream, a tool
    that started but did not finish, a search of the skill directory -- yields
    an explicitly unavailable observation, because scoring "unknown" as "did not
    trigger" is what silently deflates a trigger matrix, just as counting a
    search as a read inflates it.
    """
    _nonempty_string(skill_path, "mounted skill path")
    if stream.protocol_error is not None:
        return AgySkillObservationUnavailable(
            f"stream did not parse cleanly: {stream.protocol_error}")
    for evidence in stream.tools:
        if isinstance(evidence, AgyFileRead) and evidence.path == skill_path:
            return AgySkillActivated(path=skill_path)
    if stream.incomplete_tools:
        return AgySkillObservationUnavailable(
            "run contains tool steps that never completed: "
            + ", ".join(sorted(set(stream.incomplete_tools))))
    if any(isinstance(evidence, AgySearch) for evidence in stream.tools):
        return AgySkillObservationUnavailable(
            "run searched for the skill without opening it, so neither "
            "activation nor a clean negative is established")
    if stream.provider_error is not None:
        return AgySkillObservationUnavailable(
            f"provider reported an error: {stream.provider_error}")
    return AgySkillNotActivated()
