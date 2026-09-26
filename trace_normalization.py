"""Agent trace parsing and normalization.

Each provider stream is flattened by its `TraceDialect` into normalized events,
metrics, and trace artifacts. Trigger detection and the event counters that
metrics and trajectory diffs share live beside the dialects they depend on.
"""
from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass as _dataclass
from pathlib import Path
from typing import Any, Protocol

import telemetry as telemetry_domain
from agent_capabilities import trace_dialect_implementations
from gemini_contracts import GeminiStream
from harness_io import iter_json_objects, string_keyed_dict, write_json
from json_contracts import strict_json_loads
from telemetry_blocks import USAGE_ALIASES, _num, normalize_cost, normalize_usage
from trace_contracts import event_is_completed, parse_event_state
from trigger_contracts import TraceEventKind, TriggerDetection, TriggerEvidenceKind

GEMINI_READ_ONLY_TOOLS = (
    "glob", "grep_search", "list_directory", "read_file", "read_many_files",
)


def _jetty_trace_protocol_error(
    records: list[dict[str, Any]], pi_stream: PiStream | None,
) -> str | None:
    aggregate = next((record.get("total_tool_calls") for record in records
                      if record.get("type") == "usage"), None)
    if aggregate is None:
        if not any(str(record.get("type") or "").casefold() in {
                "command", "tool_call", "tool_use", "file_read", "file_write",
                "skill_load"} for record in records):
            return "Jetty trajectory has no event stream or explicit zero aggregate"
        return None
    detailed = sum(1 for record in records if str(record.get("type") or "").casefold()
                   in {"command", "tool_call", "tool_use", "file_read", "file_write", "skill_load"})
    if detailed != aggregate:
        return ("Jetty aggregate total_tool_calls has no complete matching "
                "event-level trajectory")
    return None


def raw_trace_record_for_ref(run_base: Path | None, ref: Any) -> dict[str, Any] | None:
    """Resolve one normalized raw-trace reference back to its provider record.

    Fails closed — returns None, never a guess — when the trace file, the cited
    physical line, or valid JSON is absent.
    """
    if run_base is None or not isinstance(ref, dict):
        return None
    line_no = ref.get("line")
    if (ref.get("file") != "trace.jsonl" or isinstance(line_no, bool)
            or not isinstance(line_no, int) or line_no < 1):
        return None
    path = run_base / "trace.jsonl"
    if not path.is_file():
        return None
    for i, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
        if i == line_no:
            try:
                record = strict_json_loads(line)
            except json.JSONDecodeError:
                return None
            return record if isinstance(record, dict) else None
    return None


def raw_trace_record_for_event(run_base: Path | None, event: dict[str, Any]) -> dict[str, Any] | None:
    """Resolve one normalized event's raw_ref back to the raw provider record in
    the run's trace.jsonl. The normalizer truncates input/output summaries, so
    this is the sanctioned path to full fidelity WITHOUT fattening events.json.
    """
    return raw_trace_record_for_ref(run_base, event.get("raw_ref"))


def command_text(event: dict[str, Any]) -> str:
    parts: list[str] = []
    for key in ["input_summary", "command", "cmd", "name"]:
        value = event.get(key)
        if isinstance(value, str):
            parts.append(value)
        elif isinstance(value, list):
            parts.append(" ".join(str(v) for v in value))
    details = event.get("details")
    if isinstance(details, dict):
        for key in ["command", "cmd", "input", "args"]:
            value = details.get(key)
            if isinstance(value, str):
                parts.append(value)
            elif isinstance(value, list):
                parts.append(" ".join(str(v) for v in value))
    return " ".join(p for p in parts if p).strip()


def command_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Commands proven completed; failed/unknown/start events never satisfy execution."""
    return [e for e in events if e.get("type") == "command" and event_is_completed(e)]


def event_mentions_skill_file(event: dict[str, Any]) -> bool:
    if event.get("type") not in {"skill_load", "file_read", "tool_call", "command"}:
        return False
    # Gemini's discovery/read-many stream results are flattened text without a
    # structured, attributable processed-file list. A matching name in their
    # query or display output proves discovery intent, not that SKILL.md was
    # opened. Only a direct read_file target remains positive tool evidence.
    if str(event.get("name") or "").casefold() in {
            "glob", "grep_search", "list_directory", "read_many_files"}:
        return False
    # Arbitrary tool/command output mentioning a path is not attributable
    # proof that the path was opened. Positive evidence comes from a typed
    # skill/file-read target or an operation input that itself names the file.
    hay = " ".join(str(event.get(key, ""))
                   for key in ["input_summary", "name"])
    return "SKILL.md" in hay or "/skills/" in hay or "\\skills\\" in hay


# The step-shaped trajectory events: the completed actions a per-step judge
# grades and the trajectory diff counts. Messages/metrics are context.
TRAJECTORY_STEP_TYPES = {"command", "tool_call", "file_read", "file_write", "skill_load"}


def trace_event_counts(events: list[dict[str, Any]]) -> dict[str, Any]:
    """Single owner of the completed-events-only counting rules. metrics.json
    (normalize_trace_records) and the report's trajectory diff both derive
    their counters here, so the two surfaces cannot drift: a diff delta is a
    delta of exactly the numbers metrics.json reports. Errors count over ALL
    events — a failed operation is an error precisely because it never
    completed."""
    completed = [e for e in events if event_is_completed(e)]
    commands = command_events(events)
    return {
        "steps": sum(1 for e in completed if e.get("type") in TRAJECTORY_STEP_TYPES),
        # File and skill operations are provider tool calls too.  Keeping their
        # finer taxonomy must not make them disappear from the aggregate call
        # count or from tool-call assertions.
        "tool_calls": sum(1 for e in completed
                          if e.get("type") in TRAJECTORY_STEP_TYPES),
        "commands": len(commands),
        "file_reads": sum(1 for e in completed if e.get("type") in {"file_read", "skill_load"}),
        "file_writes": sum(1 for e in completed if e.get("type") == "file_write"),
        "errors": sum(1 for e in events if e.get("type") == "error" or e.get("is_error") is True),
        "skill_events": [e for e in completed if e.get("type") == "skill_load" or event_mentions_skill_file(e)],
    }


EVENT_TEXT_KEYS = {"file_path", "path", "skill", "input", "input_summary", "partial_json", "command", "cmd", "args", "argv"}


def _flatten_event_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return " ".join(_flatten_event_text(item) for item in value).strip()
    return ""


def event_texts_for_tool_input(obj: Any) -> list[str]:
    """Recursively collect file-path-ish strings from a runner event (tool inputs),
    used to detect whether the model actually opened a mounted skill file."""
    out: list[str] = []
    if isinstance(obj, dict):
        for key, value in obj.items():
            if key in EVENT_TEXT_KEYS:
                text = _flatten_event_text(value)
                if text:
                    out.append(text)
            out.extend(event_texts_for_tool_input(value))
    elif isinstance(obj, list):
        for item in obj:
            out.extend(event_texts_for_tool_input(item))
    return out


def detect_trigger_records(records: Iterable[dict[str, Any]], copied_paths: list[Path],
                           *, source: str = "generic",
                           pi_stream: PiStream | None = None) -> TriggerDetection:
    """Derive mounted-path evidence from provider-aware completed operations."""
    needles = [str(p) for p in copied_paths] + [str(p.parent) for p in copied_paths]
    evidence: list[str] = []
    materialized = list(records)
    event_doc, _ = normalize_trace_records(
        materialized, source=source, pi_stream=pi_stream)
    for event in event_doc["events"]:
        if not event_is_completed(event):
            continue
        if event.get("type") not in {"skill_load", "file_read", "command"}:
            continue
        for text in event_texts_for_tool_input(event):
            if any(needle and needle in text for needle in needles):
                evidence.append(text[:500])
    return TriggerDetection.from_texts(TriggerEvidenceKind.MOUNTED_PATH, evidence[:5])


def detect_trigger_detection(stdout: str, copied_paths: list[Path],
                             *, source: str = "generic") -> TriggerDetection:
    """Typed skill-invocation detector for a raw JSON event stream."""
    records = [event for event in iter_json_objects(stdout) if isinstance(event, dict)]
    return detect_trigger_records(records, copied_paths, source=source)


def detect_trigger(stdout: str, copied_paths: list[Path]) -> tuple[bool, list[str]]:
    """Compatibility wire helper; internal trigger runners use TriggerDetection."""
    detection = detect_trigger_detection(stdout, copied_paths)
    return detection.triggered, detection.legacy_evidence


def safe_trace_label(text: str, fallback: str) -> str:
    label = re.sub(r"[^a-zA-Z0-9_.-]+", "-", text)[:80].strip("-")
    return label or fallback


def regex_hit(pattern: str, text: str, ci: bool = True) -> bool:
    flags = re.IGNORECASE if ci else 0
    return re.search(pattern, text, flags) is not None


def repeated_command_max(commands: list[str]) -> int:
    last = None
    current = 0
    best = 0
    for command in commands:
        normed = re.sub(r"\s+", " ", command.strip().casefold())
        if normed and normed == last:
            current += 1
        else:
            last = normed
            current = 1 if normed else 0
        best = max(best, current)
    return best


def raw_trace_value(record: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in record:
            return record[key]
    for container_key in ["message", "delta", "data", "item"]:
        nested = record.get(container_key)
        if isinstance(nested, dict):
            for key in keys:
                if key in nested:
                    return nested[key]
    return None


def raw_trace_input_value(record: dict[str, Any], *keys: str) -> Any:
    """Invocation arguments may supply paths/commands, never lifecycle facts."""
    value = raw_trace_value(record, *keys)
    if value is not None:
        return value
    for container_key in ("tool_input", "input", "details", "args"):
        nested = record.get(container_key)
        if isinstance(nested, dict):
            for key in keys:
                if key in nested:
                    return nested[key]
    return None


def raw_trace_has_key(record: dict[str, Any], *keys: str) -> bool:
    if any(key in record for key in keys):
        return True
    for container_key in ["message", "delta", "data", "item"]:
        nested = record.get(container_key)
        if isinstance(nested, dict) and any(key in nested for key in keys):
            return True
    return False


def stringify_trace_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return " ".join(stringify_trace_value(v) for v in value)
    if isinstance(value, dict):
        for key in ["command", "cmd", "content", "text", "path"]:
            if key in value:
                return stringify_trace_value(value[key])
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return str(value)


def parse_trace_jsonl_text_with_lines(
    text: str, *, strict_json_errors: bool = True,
) -> tuple[list[dict[str, Any]], list[str], list[int]]:
    """Parse JSONL while retaining each object's physical source line.

    Blank, malformed, and non-object lines do not become records, but they do
    occupy source lines. Keeping that mapping makes every emitted raw_ref
    resolvable against the original trace.jsonl rather than a filtered ordinal.
    """
    records: list[dict[str, Any]] = []
    errors: list[str] = []
    record_lines: list[int] = []
    for line_number, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        try:
            obj = strict_json_loads(line)
        except json.JSONDecodeError as exc:
            if ("duplicate object key" in exc.msg
                    or "non-finite numeric constant" in exc.msg
                    ) and strict_json_errors:
                raise ValueError(exc.msg) from exc
            errors.append(f"line {line_number}: {exc}")
            continue
        except (TypeError, ValueError, RecursionError) as exc:
            if strict_json_errors:
                raise ValueError(f"line {line_number}: {exc}") from exc
            errors.append(f"line {line_number}: {type(exc).__name__}: {exc}")
            continue
        if isinstance(obj, dict):
            records.append(obj)
            record_lines.append(line_number)
        else:
            errors.append(f"line {line_number}: JSON value is not an object")
    return records, errors, record_lines


def parse_trace_jsonl_text(text: str) -> tuple[list[dict[str, Any]], list[str]]:
    records, errors, _ = parse_trace_jsonl_text_with_lines(text)
    return records, errors


def load_trace_jsonl(path: Path) -> tuple[list[dict[str, Any]], list[str]]:
    return parse_trace_jsonl_text(path.read_text(encoding="utf-8", errors="replace"))


def nested_item_type(record: dict[str, Any]) -> str:
    item = record.get("item")
    if isinstance(item, dict):
        value = item.get("type") or item.get("kind") or item.get("name")
        return str(value or "")
    return ""


def usage_number(usage: dict[str, Any], *keys: str) -> int | None:
    normalized = normalize_usage(usage, source="trace_normalized")
    for key in keys:
        value = normalized.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    return None


def normalize_trace_record(record: dict[str, Any], *, source: str, index: int, line: int) -> dict[str, Any]:
    top_type = str(raw_trace_value(record, "type", "event", "name", "kind") or "")
    item_type = nested_item_type(record)
    raw_type = f"{top_type} {item_type}".casefold()
    path = stringify_trace_value(raw_trace_input_value(record, "path", "file", "file_path"))
    command = stringify_trace_value(raw_trace_input_value(record, "command", "cmd", "args"))
    content = stringify_trace_value(raw_trace_value(record, "content", "text", "message"))
    raw_status = raw_trace_value(record, "status", "state")
    parsed_state = parse_event_state(
        raw_status, raw_type=top_type or item_type,
        status_present=raw_trace_has_key(record, "status", "state"))
    status = parsed_state.state.value
    # Unknown session/lifecycle events are not tool calls. Defaulting them to
    # tool_call inflated Pi's process telemetry even when its trace had no
    # model tool use at all.
    event_type = TraceEventKind.EVENT
    name = stringify_trace_value(raw_trace_value(
        record, "tool", "tool_name", "toolName", "name"))
    tool_name = name.casefold()
    is_write = ("file_write" in raw_type or "write" in raw_type or "edit" in raw_type
                or tool_name in {"write", "edit", "multiedit", "notebookedit", "write_file"})
    is_read = ("file_read" in raw_type or "read" in raw_type
               or tool_name in {"read", "read_file"})
    skill_path = path.endswith("SKILL.md") or "/SKILL.md" in path or "\\SKILL.md" in path
    explicit_skill_load = "skill" in raw_type and ("load" in raw_type or "read" in raw_type)
    if is_write:
        event_type = TraceEventKind.FILE_WRITE
    elif explicit_skill_load or (is_read and skill_path):
        event_type = TraceEventKind.SKILL_LOAD
    elif "command" in raw_type or "exec" in raw_type or command:
        event_type = TraceEventKind.COMMAND
        name = name or "bash"
    elif is_read:
        event_type = TraceEventKind.FILE_READ
    elif "tool" in raw_type or raw_trace_value(
            record, "tool", "tool_name", "toolName", "tool_call_id") is not None:
        event_type = TraceEventKind.TOOL_CALL
    elif "error" in raw_type or str(status).casefold() in {"failed", "error", "errored"}:
        event_type = TraceEventKind.ERROR
    elif raw_trace_value(record, "role") or content or "agent_message" in raw_type:
        event_type = TraceEventKind.MESSAGE
    elif "usage" in raw_type or "metric" in raw_type or raw_trace_value(record, "usage", "tokens"):
        event_type = TraceEventKind.METRIC
    input_summary = command or path or content[:500]
    output_summary = stringify_trace_value(raw_trace_value(record, "output", "stdout", "stderr", "result"))[:1000]
    raw_call_line = record.get("_raw_call_line")
    raw_line = raw_call_line if isinstance(raw_call_line, int) and not isinstance(raw_call_line, bool) and raw_call_line > 0 else line
    event = {
        "index": index,
        "type": event_type.value,
        "status": status,
        "state_source": parsed_state.source.value,
        "raw_ref": {"file": "trace.jsonl", "line": raw_line},
    }
    raw_result_line = record.get("_raw_result_line")
    if isinstance(raw_result_line, int) and not isinstance(raw_result_line, bool) and raw_result_line > 0:
        event["raw_result_ref"] = {"file": "trace.jsonl", "line": raw_result_line}
    if record.get("is_error") is True:
        event["is_error"] = True
    if isinstance(raw_status, str) and raw_status.casefold() != status:
        event["raw_status"] = raw_status
    role = raw_trace_value(record, "role")
    if not role and "agent_message" in raw_type:
        role = "assistant"
    if role:
        event["role"] = str(role)
    if name:
        event["name"] = name
    if input_summary:
        event["input_summary"] = input_summary[:1000]
    if output_summary:
        event["output_summary"] = output_summary
    timestamp = raw_trace_value(record, "timestamp", "time", "created_at")
    if timestamp:
        event["timestamp"] = str(timestamp)
    exit_code = raw_trace_value(record, "exit_code", "returncode")
    if isinstance(exit_code, int):
        event["exit_code"] = exit_code
    duration = _num(raw_trace_value(record, "duration_ms", "elapsed_ms"))
    if duration is not None:
        event["duration_ms"] = duration
    usage = record.get("usage") if isinstance(record.get("usage"), dict) else record.get("tokens")
    if isinstance(usage, dict):
        token_doc = {k: v for k, raw in usage.items() if (v := _num(raw)) is not None}
        normalized_total = usage_number(usage, "total_tokens")
        normalized_input = usage_number(usage, "input_tokens")
        normalized_output = usage_number(usage, "output_tokens")
        if normalized_total is None and normalized_input is not None and normalized_output is not None:
            normalized_total = normalized_input + normalized_output
        if normalized_input is not None:
            token_doc["input_tokens"] = int(normalized_input)
        if normalized_output is not None:
            token_doc["output_tokens"] = int(normalized_output)
        if normalized_total is not None:
            token_doc["total_tokens"] = int(normalized_total)
        event["tokens"] = token_doc
    event["source"] = source
    event["otel"] = otel_attributes_for_event(event)
    return event


def otel_attributes_for_event(event: dict[str, Any]) -> dict[str, Any]:
    """OTel GenAI semantic-convention attributes for one normalized event
    (roadmap 2.4). Additive: the harness's own keys stay authoritative for
    grading; these make the trace boundary a standard target instead of a
    bespoke schema. Events schema_version 2 carries them; version-1 files
    still grade unchanged."""
    attrs: dict[str, Any] = {}
    event_type = event.get("type")
    if event_type in {"command", "tool_call"}:
        attrs["gen_ai.operation.name"] = "execute_tool"
        attrs["gen_ai.tool.name"] = str(event.get("name") or "bash")
        if event.get("input_summary"):
            attrs["gen_ai.tool.call.arguments"] = event["input_summary"]
        if event.get("output_summary"):
            attrs["gen_ai.tool.call.result"] = event["output_summary"]
    elif event_type == "message":
        attrs["gen_ai.operation.name"] = "chat"
        if event.get("role"):
            attrs["gen_ai.message.role"] = event["role"]
    elif event_type == "error":
        attrs["error.type"] = str(event.get("name") or event.get("input_summary") or "error")[:120]
    elif event_type in {"file_read", "file_write", "skill_load"}:
        if event.get("input_summary"):
            attrs["file.path"] = event["input_summary"][:500]
    tokens = event.get("tokens")
    if isinstance(tokens, dict):
        if (input_tokens := _num(tokens.get("input_tokens"))) is not None:
            attrs["gen_ai.usage.input_tokens"] = int(input_tokens)
        if (output_tokens := _num(tokens.get("output_tokens"))) is not None:
            attrs["gen_ai.usage.output_tokens"] = int(output_tokens)
    if isinstance(event.get("exit_code"), int):
        attrs["process.exit_code"] = event["exit_code"]
    if event.get("is_error") is True:
        attrs["error.type"] = str(event.get("name") or "tool_result_error")[:120]
    return attrs


def _claude_tool_flat_record(name: str, tool_input: Any) -> dict[str, Any]:
    """One Claude tool_use block as a normalizer-native record. Bash is a shell
    command; Read/Write/Edit are file operations (a SKILL.md path classifies as
    skill_load in normalize_trace_record); the Skill tool is a skill load; any
    other tool stays a generic tool call with its input preserved."""
    inp = tool_input if isinstance(tool_input, dict) else {}
    if name == "Bash":
        return {"type": "command", "tool": name, "command": str(inp.get("command") or "")}
    if name == "Read":
        return {"type": "file_read", "name": name, "path": str(inp.get("file_path") or "")}
    if name in {"Write", "Edit", "MultiEdit", "NotebookEdit"}:
        return {"type": "file_write", "name": name, "path": str(inp.get("file_path") or inp.get("notebook_path") or "")}
    if name == "Skill":
        return {"type": "skill_load", "name": name, "path": str(inp.get("skill") or "")}
    return {"type": "tool_use", "tool": name, "input": inp}


def _claude_protocol_error(message: str) -> dict[str, Any]:
    return {
        "type": "error", "status": "failed", "message": message,
        "_trace_protocol_invalid": True,
    }


def claude_stream_flat_records(records: list[dict[str, Any]], *,
                               record_lines: list[int] | None = None) -> list[tuple[int, dict[str, Any]]]:
    """Flatten `claude -p --output-format stream-json` events into (line, record)
    pairs the generic normalizer understands. A message wraps several content
    blocks, so one raw line can yield several records — each keeps the RAW line
    for its raw_ref. Lifecycle is paired: a tool_use OPENS a call (in progress)
    and the matching tool_result COMPLETES it, so an orphaned call (crash
    mid-tool) counts zero under the completed-events-only metrics contract.
    Usage rides ONLY the terminal result event — per-assistant-message usage is
    API-request-level and would double count the cumulative total."""
    flat: list[tuple[int, dict[str, Any]]] = []
    if record_lines is not None and len(record_lines) != len(records):
        raise ValueError("record_lines must have one physical line per trace record")
    open_calls: dict[str, tuple[int, dict[str, Any]]] = {}
    seen_call_ids: set[str] = set()
    for ordinal, record in enumerate(records, 1):
        line = record_lines[ordinal - 1] if record_lines is not None else ordinal
        rtype = str(record.get("type") or "")
        if rtype not in {"assistant", "user"}:
            # system/init, result, and unknown lifecycle events pass through:
            # `result` is an intrinsically terminal kind carrying usage/duration.
            flat.append((line, record))
            continue
        if not isinstance(record.get("message"), dict):
            flat.append((line, _claude_protocol_error(
                f"Claude {rtype} record has no message object")))
            continue
        message = record["message"]
        role = str(message.get("role") or rtype)
        content = message.get("content")
        if isinstance(content, str):
            if content.strip():
                flat.append((line, {"type": "message", "role": role, "text": content}))
            continue
        if not isinstance(content, list):
            flat.append((line, _claude_protocol_error(
                "Claude message content must be a string or list")))
            continue
        for block in content:
            if not isinstance(block, dict):
                flat.append((line, _claude_protocol_error(
                    "Claude message content blocks must be objects")))
                continue
            btype = block.get("type")
            if btype == "text":
                text = block.get("text")
                if not isinstance(text, str):
                    flat.append((line, _claude_protocol_error(
                        "Claude text block text must be a string")))
                elif text.strip():
                    flat.append((line, {"type": "message", "role": role, "text": text}))
            elif btype == "thinking":
                if not isinstance(block.get("thinking"), str):
                    flat.append((line, _claude_protocol_error(
                        "Claude thinking block must contain thinking text")))
            elif btype == "redacted_thinking":
                if not isinstance(block.get("data"), str):
                    flat.append((line, _claude_protocol_error(
                        "Claude redacted_thinking block must contain data")))
            elif btype == "tool_use":
                name, raw_input, call_id = block.get("name"), block.get("input"), block.get("id")
                if not isinstance(name, str) or not name.strip():
                    flat.append((line, _claude_protocol_error(
                        "Claude tool_use name must be a non-empty string")))
                    continue
                if not isinstance(raw_input, dict):
                    flat.append((line, _claude_protocol_error(
                        "Claude tool_use input must be an object")))
                    continue
                if not isinstance(call_id, str) or not call_id.strip():
                    flat.append((line, _claude_protocol_error(
                        "Claude tool_use id must be a non-empty string")))
                    continue
                spec = _claude_tool_flat_record(name, raw_input)
                if call_id in seen_call_ids:
                    lifecycle = "open " if call_id in open_calls else "reused "
                    flat.append((line, _claude_protocol_error(
                        f"duplicate {lifecycle}Claude tool_use call id {call_id!r}")))
                    continue
                flat.append((line, {**spec, "status": "in_progress"}))
                seen_call_ids.add(call_id)
                open_calls[call_id] = (line, spec)
            elif btype == "tool_result":
                call_id = block.get("tool_use_id")
                if not isinstance(call_id, str) or not call_id.strip():
                    flat.append((line, _claude_protocol_error(
                        "Claude tool_result tool_use_id must be a non-empty string")))
                    continue
                if "is_error" in block and not isinstance(block["is_error"], bool):
                    flat.append((line, _claude_protocol_error(
                        "Claude tool_result is_error must be boolean")))
                    continue
                if not isinstance(block.get("content"), (str, list)):
                    flat.append((line, _claude_protocol_error(
                        "Claude tool_result content must be a string or list")))
                    continue
                matched_call = open_calls.pop(call_id, None)
                if matched_call is None:
                    flat.append((line, {
                        **_claude_protocol_error(
                            f"unmatched Claude tool_result for call id {call_id!r}"),
                        "result": stringify_trace_value(block.get("content"))[:1000],
                        "_raw_result_line": line,
                    }))
                    continue
                call_line, spec = matched_call
                completion = {**spec, "status": "completed",
                              "output": stringify_trace_value(block.get("content"))[:1000],
                              "_raw_call_line": call_line, "_raw_result_line": line}
                if block.get("is_error"):
                    # the call completed WITH an error result (e.g. nonzero
                    # exit); lifecycle-wise it still ran, so it stays completed.
                    completion["is_error"] = True
                flat.append((line, completion))
            else:
                flat.append((line, _claude_protocol_error(
                    f"unsupported Claude content block type {btype!r}")))
    for call_id, (call_line, _) in sorted(open_calls.items()):
        flat.append((call_line, _claude_protocol_error(
            f"Claude tool_use call id {call_id!r} has no matching tool_result")))
    return flat


def identity_flat_records(records: list[dict[str, Any]], *,
                          record_lines: list[int] | None = None) -> list[tuple[int, dict[str, Any]]]:
    """One raw record per physical line, unchanged — the default flatten."""
    if record_lines is not None and len(record_lines) != len(records):
        raise ValueError("record_lines must have one physical line per trace record")
    return [((record_lines[i - 1] if record_lines is not None else i), record)
            for i, record in enumerate(records, 1)]


def vibe_stream_flat_records(records: list[dict[str, Any]], *,
                             record_lines: list[int] | None = None) -> list[tuple[int, dict[str, Any]]]:
    """Flatten Vibe's OpenAI-style message/tool lifecycle without dropping it."""
    if record_lines is not None and len(record_lines) != len(records):
        raise ValueError("record_lines must have one physical line per trace record")
    flat: list[tuple[int, dict[str, Any]]] = []
    open_calls: dict[str, tuple[int, dict[str, Any]]] = {}
    seen: set[str] = set()

    def invalid(line: int, message: str) -> None:
        flat.append((line, _claude_protocol_error(f"Vibe {message}")))

    for ordinal, record in enumerate(records, 1):
        line = record_lines[ordinal - 1] if record_lines is not None else ordinal
        role = record.get("role")
        tool_calls = record.get("tool_calls")
        if tool_calls is not None:
            if role != "assistant" or not isinstance(tool_calls, list):
                invalid(line, "tool_calls must be a list on an assistant message")
                continue
            for call in tool_calls:
                if not isinstance(call, dict):
                    invalid(line, "tool_calls entries must be objects")
                    continue
                call_id = call.get("id")
                function = call.get("function")
                if not isinstance(call_id, str) or not call_id.strip():
                    invalid(line, "tool call id must be a non-empty string")
                    continue
                if call_id in seen:
                    invalid(line, f"tool call id {call_id!r} is duplicated or reused")
                    continue
                if not isinstance(function, dict):
                    invalid(line, "tool call function must be an object")
                    continue
                name, arguments = function.get("name"), function.get("arguments")
                if not isinstance(name, str) or not name.strip():
                    invalid(line, "tool call function name must be a non-empty string")
                    continue
                if isinstance(arguments, str):
                    try:
                        arguments = strict_json_loads(arguments)
                    except json.JSONDecodeError:
                        invalid(line, "tool call arguments must be a JSON object")
                        continue
                if not isinstance(arguments, dict):
                    invalid(line, "tool call arguments must be an object")
                    continue
                if name == "skill":
                    spec = {"type": "skill_load", "name": name,
                            "path": str(arguments.get("name") or arguments.get("skill") or "")}
                elif name == "read_file":
                    spec = {"type": "file_read", "name": name,
                            "path": str(arguments.get("path") or arguments.get("file_path") or "")}
                elif name == "grep":
                    spec = {"type": "tool_use", "tool": name, "input": arguments}
                else:
                    invalid(line, f"tool call function {name!r} is unsupported")
                    continue
                flat.append((line, {**spec, "status": "in_progress"}))
                open_calls[call_id] = (line, spec)
                seen.add(call_id)
        content = record.get("content")
        if role == "tool":
            call_id = record.get("tool_call_id")
            if not isinstance(call_id, str) or not call_id.strip():
                invalid(line, "tool result tool_call_id must be a non-empty string")
                continue
            matched = open_calls.pop(call_id, None)
            if matched is None:
                invalid(line, f"tool result for unknown call id {call_id!r}")
                continue
            call_line, spec = matched
            flat.append((line, {**spec, "status": "completed",
                                "output": stringify_trace_value(content)[:1000],
                                "_raw_call_line": call_line,
                                "_raw_result_line": line}))
        elif role in {"assistant", "user", "system"}:
            if content is not None and not isinstance(content, str):
                invalid(line, "message content must be a string")
            elif isinstance(content, str) and content.strip():
                flat.append((line, {"type": "message", "role": role,
                                    "text": content,
                                    **({"usage": record["usage"]}
                                       if isinstance(record.get("usage"), dict) else {})}))
        elif role is not None:
            invalid(line, f"message role {role!r} is unsupported")
        elif tool_calls is None:
            invalid(line, "record must have a supported role")
    for call_id, (line, _) in sorted(open_calls.items()):
        invalid(line, f"tool call id {call_id!r} has no matching tool result")
    return flat


def _gemini_tool_flat_record(name: str, parameters: Mapping[str, Any]) -> dict[str, Any]:
    """Map one official Gemini tool name into the harness trace vocabulary."""
    values = dict(parameters)
    common = {"parameters": values}
    if name == "run_shell_command":
        return {"type": "command", "tool": name,
                "command": str(values.get("command") or ""), **common}
    if name == "activate_skill":
        return {"type": "skill_load", "name": name,
                "path": str(values.get("name") or ""), **common}
    if name in {"write_file", "replace"}:
        return {"type": "file_write", "name": name,
                "path": str(values.get("file_path") or ""), **common}
    if name == "read_file":
        path = str(values.get("file_path") or "")
    elif name in {"list_directory", "grep_search", "glob"}:
        # Search patterns are queries, not evidence that a matching path was
        # opened.  Only the searched directory belongs in the path channel.
        path = str(values.get("dir_path") or "")
    elif name == "read_many_files":
        # stream-json omits this tool's structured returnDisplay, including its
        # processed-files list.  `include` contains paths/globs the model asked
        # to read, not proof that any match was opened, so fail closed on path
        # evidence while preserving the original parameters.
        path = ""
    else:
        path = ""
    if name in GEMINI_READ_ONLY_TOOLS:
        return {"type": "file_read", "name": name, "path": path, **common}
    return {"type": "tool_use", "tool": name, "input": values}


def gemini_stream_flat_records(
    records: list[dict[str, Any]], *, record_lines: list[int] | None = None,
) -> list[tuple[int, dict[str, Any]]]:
    """Flatten Gemini's paired tool events without discarding raw line refs."""
    if record_lines is not None and len(record_lines) != len(records):
        raise ValueError("record_lines must have one physical line per trace record")
    flat: list[tuple[int, dict[str, Any]]] = []
    open_calls: dict[str, tuple[int, dict[str, Any]]] = {}
    for ordinal, record in enumerate(records, 1):
        line = record_lines[ordinal - 1] if record_lines is not None else ordinal
        kind = record.get("type")
        if kind == "tool_use":
            call_id = record.get("tool_id")
            name = record.get("tool_name")
            parameters = record.get("parameters")
            if (not isinstance(call_id, str) or not call_id.strip()
                    or not isinstance(name, str) or not name.strip()
                    or not isinstance(parameters, Mapping)):
                flat.append((line, _claude_protocol_error(
                    "Gemini tool_use has an invalid id, name, or parameters")))
                continue
            spec = _gemini_tool_flat_record(name, parameters)
            flat.append((line, {**spec, "status": "in_progress"}))
            open_calls[call_id] = (line, spec)
            continue
        if kind == "tool_result":
            call_id = record.get("tool_id")
            matched = open_calls.pop(call_id, None) if isinstance(call_id, str) else None
            if matched is None:
                flat.append((line, _claude_protocol_error(
                    "Gemini tool_result has no matching tool_use")))
                continue
            call_line, spec = matched
            flat.append((line, {
                **spec,
                "status": (
                    "failed" if record.get("status") == "error"
                    else "completed"
                ),
                "output": stringify_trace_value(
                    record.get("output") or record.get("error"))[:1000],
                "is_error": record.get("status") == "error",
                "_raw_call_line": call_line,
                "_raw_result_line": line,
            }))
            continue
        if kind == "error" and record.get("severity") == "warning":
            flat.append((line, {
                "type": "warning",
                "severity": "warning",
                "message": record.get("message"),
                "timestamp": record.get("timestamp"),
            }))
            continue
        if kind == "message":
            flat.append((line, {
                "type": "message",
                "role": record.get("role"),
                "text": record.get("content"),
                "timestamp": record.get("timestamp"),
            }))
            continue
        flat.append((line, record))
    for call_id, (line, _) in sorted(open_calls.items()):
        flat.append((line, _claude_protocol_error(
            f"Gemini tool_use call id {call_id!r} has no matching tool_result")))
    return flat


def _gemini_records_text(records: Iterable[Mapping[str, Any]]) -> str:
    return "\n".join(json.dumps(dict(record), ensure_ascii=False)
                     for record in records) + ("\n" if records else "")


def _gemini_stream_semantics(
    records: list[dict[str, Any]], pi_stream: PiStream | None,
) -> tuple[dict[str, Any] | None, str | None]:
    _ = pi_stream
    parsed = GeminiStream.parse(_gemini_records_text(records))
    failure = parsed.protocol_error or parsed.provider_error
    return (dict(parsed.usage) if parsed.usage is not None and failure is None
            else None), failure


def _gemini_usage_and_cost_blocks(
    raw_text: str, pi_stream: PiStream | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    _ = pi_stream
    parsed = GeminiStream.parse(raw_text)
    if not parsed.complete or parsed.usage is None:
        return {"source": "missing"}, {"source": "missing"}
    return (
        normalize_usage(dict(parsed.usage), source="trace_normalized"),
        {"source": "missing"},
    )


def _no_stream_semantics(records: list[dict[str, Any]], pi_stream: PiStream | None) -> tuple[dict[str, Any] | None, str | None]:
    """No terminal cumulative usage and no stream-level failure: per-record
    token accumulation applies."""
    return None, None


def _no_retry_observation(records: list[dict[str, Any]], pi_stream: PiStream | None) -> int | None:
    """The provider's stream protocol has no retry marker, so retries are not
    observed — the metric is omitted and reads as unavailable, never zero."""
    return None


def _pi_retries(records: list[dict[str, Any]], pi_stream: PiStream | None) -> int | None:
    return (pi_stream or PiStream.from_records(records)).retries


def _pi_stream_semantics(records: list[dict[str, Any]], pi_stream: PiStream | None) -> tuple[dict[str, Any] | None, str | None]:
    """Pi repeats final cumulative usage on message_end, turn_end, and
    agent_end — one response, not several token deltas — and a terminal
    failure invalidates the stream's usage entirely. An already-parsed
    PiStream may be passed to avoid re-parsing."""
    parsed = pi_stream or PiStream.from_records(records)
    error = parsed.failure_error
    return (parsed.terminal_usage if not error else None), error


def _generic_usage_and_cost_blocks(raw_text: str, pi_stream: PiStream | None) -> tuple[dict[str, Any], dict[str, Any]]:
    records = [obj for obj in iter_json_objects(raw_text) if isinstance(obj, dict)]
    return _generic_stream_usage_and_cost(records)


def _pi_usage_and_cost_blocks(raw_text: str, pi_stream: PiStream | None) -> tuple[dict[str, Any], dict[str, Any]]:
    parsed = pi_stream or PiStream.parse(raw_text)
    return dict(parsed.usage_normalized), dict(parsed.cost_normalized)


def _generic_trace_protocol_error(
    records: list[dict[str, Any]], pi_stream: PiStream | None,
) -> str | None:
    return None


def _codex_trace_protocol_error(
    records: list[dict[str, Any]], pi_stream: PiStream | None,
) -> str | None:
    terminals = [i for i, record in enumerate(records)
                 if str(record.get("type") or "").casefold() == "turn.completed"]
    if terminals != [len(records) - 1]:
        return "Codex trace must contain exactly one final turn.completed event"
    return None


def _claude_trace_protocol_error(
    records: list[dict[str, Any]], pi_stream: PiStream | None,
) -> str | None:
    terminals = [i for i, record in enumerate(records) if record.get("type") == "result"]
    if terminals != [len(records) - 1]:
        return "Claude trace must contain exactly one final result event"
    return None


def _vibe_trace_protocol_error(
    records: list[dict[str, Any]], pi_stream: PiStream | None,
) -> str | None:
    terminal_answer = (records and records[-1].get("role") == "assistant"
                       and isinstance(records[-1].get("content"), str)
                       and bool(records[-1]["content"].strip()))
    if not terminal_answer:
        return "Vibe trace must end with one non-empty assistant response"
    return None


def _gemini_trace_protocol_error(
    records: list[dict[str, Any]], pi_stream: PiStream | None,
) -> str | None:
    _ = pi_stream
    return GeminiStream.parse(_gemini_records_text(records)).protocol_error


def _pi_trace_protocol_error(
    records: list[dict[str, Any]], pi_stream: PiStream | None,
) -> str | None:
    parsed = pi_stream or PiStream.from_records(records)
    return parsed.protocol_error


class TraceFlattener(Protocol):
    def __call__(self, records: list[dict[str, Any]], *,
                 record_lines: list[int] | None = None) -> list[tuple[int, dict[str, Any]]]: ...


@_dataclass(frozen=True)
class TraceDialect:
    """Per-provider trace semantics: how raw records flatten into
    normalizer-native (line, record) pairs — claude's block-structured stream
    expands to several records per raw line — how a stream's terminal
    usage/failure resolve — Pi's cumulative repeats must not be summed — and
    how a raw stream normalizes into usage/cost blocks. ONE registry instead
    of per-source branches scattered through the normalizer and
    stream_usage_and_cost. Generic-semantics providers are registered
    explicitly; misspelled or unsupported sources are rejected before a
    provider-specific stream can be silently normalized with the wrong rules."""

    flatten: TraceFlattener = identity_flat_records
    stream_semantics: Callable[[list[dict[str, Any]], PiStream | None], tuple[dict[str, Any] | None, str | None]] = _no_stream_semantics
    usage_and_cost: Callable[[str, PiStream | None], tuple[dict[str, Any], dict[str, Any]]] = _generic_usage_and_cost_blocks
    protocol_error: Callable[[list[dict[str, Any]], PiStream | None], str | None] = _generic_trace_protocol_error
    retries: Callable[[list[dict[str, Any]], PiStream | None], int | None] = _no_retry_observation


GENERIC_TRACE_DIALECT = TraceDialect()
CODEX_TRACE_DIALECT = TraceDialect(protocol_error=_codex_trace_protocol_error)
JETTY_TRACE_DIALECT = TraceDialect(protocol_error=_jetty_trace_protocol_error)
VIBE_TRACE_DIALECT = TraceDialect(
    flatten=vibe_stream_flat_records,
    protocol_error=_vibe_trace_protocol_error,
)
CLAUDE_TRACE_DIALECT = TraceDialect(
    flatten=claude_stream_flat_records,
    protocol_error=_claude_trace_protocol_error,
)
PI_TRACE_DIALECT = TraceDialect(
    stream_semantics=_pi_stream_semantics,
    usage_and_cost=_pi_usage_and_cost_blocks,
    protocol_error=_pi_trace_protocol_error,
    retries=_pi_retries,
)
GEMINI_TRACE_DIALECT = TraceDialect(
    flatten=gemini_stream_flat_records,
    stream_semantics=_gemini_stream_semantics,
    usage_and_cost=_gemini_usage_and_cost_blocks,
    protocol_error=_gemini_trace_protocol_error,
)

# ``generic`` is the explicit source for unowned imported traces. Every backend
# key is projected from its declarative row instead of being registered here a
# second time.
TRACE_DIALECTS: dict[str, TraceDialect] = {
    "generic": GENERIC_TRACE_DIALECT,
    **trace_dialect_implementations(),
}


def trace_dialect_for(source: str) -> TraceDialect:
    if not isinstance(source, str) or not source:
        raise ValueError("trace source must be a non-empty string")
    key = source.casefold()
    try:
        return TRACE_DIALECTS[key]
    except KeyError as exc:
        raise ValueError(
            f"unsupported trace source {source!r}; known: {sorted(TRACE_DIALECTS)}") from exc


def normalize_trace_records(records: list[dict[str, Any]], *, source: str = "generic",
                            pi_stream: PiStream | None = None,
                            record_lines: list[int] | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
    dialect = trace_dialect_for(source)
    flat = dialect.flatten(records, record_lines=record_lines)
    events = [normalize_trace_record(record, source=source, index=i, line=line)
              for i, (line, record) in enumerate(flat, 1)]
    commands = [command_text(e) for e in command_events(events)]
    token_totals: dict[str, int | None] = {
        "input_tokens": None, "output_tokens": None, "total_tokens": None,
    }
    elapsed_ms = 0.0
    terminal_usage, stream_error = dialect.stream_semantics(records, pi_stream)
    if terminal_usage is not None:
        for key in token_totals:
            value = usage_number(terminal_usage, key)
            if value is not None:
                token_totals[key] = value
    elif not stream_error:
        for (_, record), event in zip(flat, events):
            usage = record.get("usage") if isinstance(record.get("usage"), dict) else record.get("tokens")
            if isinstance(usage, dict):
                input_tokens = usage_number(usage, "input_tokens")
                output_tokens = usage_number(usage, "output_tokens")
                total_tokens = usage_number(usage, "total_tokens")
                if total_tokens is None and input_tokens is not None and output_tokens is not None:
                    total_tokens = input_tokens + output_tokens
                for key, value in [("input_tokens", input_tokens), ("output_tokens", output_tokens), ("total_tokens", total_tokens)]:
                    if value is not None:
                        token_totals[key] = (token_totals[key] or 0) + value
            duration = _num(raw_trace_value(record, "duration_ms", "elapsed_ms"))
            if duration is not None:
                elapsed_ms += duration
            tokens = event.get("tokens")
            if isinstance(tokens, dict) and not isinstance(usage, dict):
                for key in ("input_tokens", "output_tokens", "total_tokens"):
                    value = usage_number(tokens, key)
                    if value is not None:
                        token_totals[key] = (token_totals[key] or 0) + value
    if (token_totals["total_tokens"] is None
            and token_totals["input_tokens"] is not None
            and token_totals["output_tokens"] is not None):
        token_totals["total_tokens"] = (
            token_totals["input_tokens"] + token_totals["output_tokens"])
    counts = trace_event_counts(events)
    skill_events = counts["skill_events"]
    metrics: dict[str, Any] = {
        "schema_version": 2,
        "source": source,
        "tool_calls": counts["tool_calls"],
        "commands": counts["commands"],
        "file_reads": counts["file_reads"],
        "file_writes": counts["file_writes"],
        "errors": counts["errors"],
        "repeated_command_max": repeated_command_max(commands),
        "skill_invoked": bool(skill_events),
        "skill_invocation_evidence": [command_text(e) or e.get("input_summary", "") for e in skill_events[:10]],
    }
    retries = dialect.retries(records, pi_stream)
    if retries is not None:
        metrics["retries"] = retries
    protocol_errors = [
        str(record.get("message") or "trace protocol error")
        for _, record in flat if record.get("_trace_protocol_invalid") is True
    ]
    if protocol_errors:
        metrics["trace_protocol_errors"] = protocol_errors[:20]
    if elapsed_ms:
        metrics["elapsed_ms"] = int(elapsed_ms)
    for key, value in token_totals.items():
        if value is not None:
            metrics[key] = value
    observed_tokens = {key: value for key, value in token_totals.items()
                       if value is not None}
    if observed_tokens:
        # Trace-derived tokens get the normalized block (issue #21). Never a
        # missing marker here — a provider-reported block in run metadata must
        # not be shadowed by an empty trace.
        metrics["usage_normalized"] = normalize_usage(
            observed_tokens, source="trace_normalized")
    otel_usage = {}
    if token_totals["input_tokens"] is not None:
        otel_usage["gen_ai.usage.input_tokens"] = token_totals["input_tokens"]
    if token_totals["output_tokens"] is not None:
        otel_usage["gen_ai.usage.output_tokens"] = token_totals["output_tokens"]
    if otel_usage:
        metrics["otel"] = otel_usage
    event_doc = {"schema_version": 2, "source": source, "events": events}
    return event_doc, metrics


def _pi_final_message(record: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(record, dict):
        return None
    message = record.get("message")
    if isinstance(message, dict):
        return string_keyed_dict(message, "Pi final message")
    messages = record.get("messages")
    if isinstance(messages, list):
        for candidate in reversed(messages):
            if isinstance(candidate, dict) and candidate.get("role") == "assistant":
                return string_keyed_dict(candidate, "Pi assistant message")
        for candidate in reversed(messages):
            if isinstance(candidate, dict):
                return string_keyed_dict(candidate, "Pi fallback message")
    return None


def _pi_final_agent_end(records: list[dict[str, Any]]) -> dict[str, Any] | None:
    return next((record for record in reversed(records) if str(record.get("type") or "") == "agent_end"), None)


def _pi_terminal_error(records: list[dict[str, Any]]) -> str | None:
    """Read only Pi's final retry attempt, never a historical failed message."""
    last_agent_end = _pi_final_agent_end(records)
    fallback = next((record for record in reversed(records) if str(record.get("type") or "") == "turn_end"), None)
    candidate = _pi_final_message(last_agent_end if last_agent_end is not None else fallback)
    if candidate is None:
        return None
    error = candidate.get("errorMessage") or candidate.get("error")
    if isinstance(error, str) and error.strip():
        return error.strip()
    if str(candidate.get("stopReason") or "").casefold() == "error":
        return "Pi provider ended the turn with stopReason:error"
    return None


def pi_stream_terminal_error(raw_text: str) -> str | None:
    """Compatibility boundary backed by the typed, single-pass Pi parser."""
    return PiStream.parse(raw_text).failure_error


def _pi_terminal_usage(records: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Return cumulative usage from Pi's final retry attempt only."""
    last_agent_end = _pi_final_agent_end(records)
    fallback = next((record for record in reversed(records)
                     if str(record.get("type") or "") in {"turn_end", "message_end"}), None)
    message = _pi_final_message(last_agent_end if last_agent_end is not None else fallback)
    return message.get("usage") if isinstance(message, dict) and isinstance(message.get("usage"), dict) else None


def _stream_usage_doc(record: dict[str, Any]) -> dict[str, Any] | None:
    usage = raw_trace_value(record, "usage", "tokens")
    return usage if isinstance(usage, dict) else None


def _is_cumulative_usage_record(record: dict[str, Any]) -> bool:
    top_type = str(raw_trace_value(record, "type", "event", "name", "kind") or "").casefold()
    item_type = nested_item_type(record).casefold()
    return top_type in {"result", "response.completed"} or item_type in {"result", "response.completed"}


def _sum_stream_usage(records: list[dict[str, Any]]) -> dict[str, Any]:
    totals: dict[str, int] = {}
    for record in records:
        usage = _stream_usage_doc(record)
        if not usage:
            continue
        normalized = normalize_usage(usage, source="trace_normalized")
        for key in USAGE_ALIASES:
            value = normalized.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                totals[key] = totals.get(key, 0) + int(value)
    return normalize_usage(totals if totals else None, source="trace_normalized")


def _cost_value_from_record(record: dict[str, Any]) -> Any:
    """Return a validated raw cost value without discarding object currency."""
    raw = raw_trace_value(record, "cost", "cost_usd", "total_cost_usd")
    if raw is None:
        usage_obj = _stream_usage_doc(record)
        if isinstance(usage_obj, dict):
            raw = usage_obj.get("cost", usage_obj.get("cost_usd"))
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        return raw if _num(raw) is not None else None
    if isinstance(raw, dict):
        return raw if normalize_cost(raw).get("source") != "missing" else None
    return None


def _generic_stream_usage_and_cost(records: list[dict[str, Any]]) -> tuple[dict[str, Any], dict[str, Any]]:
    cumulative_usage: list[dict[str, Any]] = []
    for record in records:
        if not _is_cumulative_usage_record(record):
            continue
        usage_doc = _stream_usage_doc(record)
        if not usage_doc:
            continue
        normalized = normalize_usage(usage_doc, source="trace_normalized")
        if normalized.get("source") != "missing":
            cumulative_usage.append(normalized)
    usage = cumulative_usage[-1] if cumulative_usage else _sum_stream_usage(records)

    cumulative_cost: list[dict[str, Any]] = []
    for record in records:
        if not _is_cumulative_usage_record(record):
            continue
        value = _cost_value_from_record(record)
        if value is None:
            continue
        normalized = normalize_cost(value, source="trace_normalized")
        if normalized.get("source") != "missing":
            cumulative_cost.append(normalized)
    if cumulative_cost:
        return usage, cumulative_cost[-1]
    cost_values = [_cost_value_from_record(record) for record in records]
    cost_blocks = [normalize_cost(value, source="trace_normalized") for value in cost_values if value is not None]
    cost_blocks = [block for block in cost_blocks if block.get("source") != "missing"]
    currencies = {str(block.get("currency")) for block in cost_blocks}
    if len(currencies) == 1:
        currency = next(iter(currencies))
        cost_total = sum(float(block["total_cost"]) for block in cost_blocks)
        cost = normalize_cost({"amount": round(cost_total, 6), "currency": currency}, source="trace_normalized")
    else:
        cost = {"source": "missing"}
    return usage, cost


@_dataclass(frozen=True)
class PiStream:
    """One parsed Pi JSON stream with intrinsic terminal semantics.

    Provider status and cumulative telemetry are derived together once. Callers
    pass this object to detection, telemetry, and trace normalization rather than
    reinterpreting the same wire text with independent policies.
    """

    records: tuple[dict[str, Any], ...]
    parse_errors: tuple[str, ...]
    terminal_error: str | None
    protocol_error: str | None
    terminal_usage: dict[str, Any] | None
    usage_normalized: dict[str, Any]
    cost_normalized: dict[str, Any]
    # Attempts Pi retried: every retried attempt ends in an agent_end marked
    # willRetry:true. Known only once the stream reaches its final agent_end —
    # a truncated stream may be missing later attempts.
    retries: int | None = None

    def __post_init__(self) -> None:
        if self.failure_error and (
            self.usage_normalized != {"source": "missing"}
            or self.cost_normalized != {"source": "missing"}
        ):
            raise ValueError("failed Pi streams cannot carry measured usage or cost")
        if self.retries is not None and (
            isinstance(self.retries, bool) or not isinstance(self.retries, int) or self.retries < 0
        ):
            raise ValueError("Pi retry count must be a non-negative integer or None")
        if self.protocol_error and self.retries is not None:
            raise ValueError("protocol-invalid Pi streams cannot carry a retry count")

    @property
    def failure_error(self) -> str | None:
        return self.terminal_error or self.protocol_error

    @classmethod
    def from_records(cls, records: Iterable[dict[str, Any]],
                     parse_errors: Iterable[str] = ()) -> PiStream:
        materialized = [dict(record) for record in records]
        terminal_error = _pi_terminal_error(materialized)
        final_agent_end = _pi_final_agent_end(materialized)
        terminal_seen = final_agent_end is not None and final_agent_end.get("willRetry") is not True
        errors = tuple(parse_errors)
        protocol_error = (
            f"Pi JSON stream parse error: {errors[0]}" if errors
            else None if terminal_seen
            else "Pi JSON stream ended without a final agent_end event"
        )
        terminal_usage = _pi_terminal_usage(materialized)
        if terminal_error or protocol_error:
            usage, cost = {"source": "missing"}, {"source": "missing"}
        elif terminal_usage is not None:
            usage = normalize_usage(terminal_usage, source="trace_normalized")
            cost = normalize_cost(terminal_usage.get("cost"), source="trace_normalized")
        else:
            usage, cost = _generic_stream_usage_and_cost(materialized)
        retries = None if protocol_error else sum(
            1 for record in materialized
            if record.get("type") == "agent_end" and record.get("willRetry") is True)
        return cls(tuple(materialized), errors, terminal_error, protocol_error,
                   dict(terminal_usage) if terminal_usage is not None else None,
                   usage, cost, retries)

    @classmethod
    def parse(cls, raw_text: str) -> PiStream:
        if not isinstance(raw_text, str):
            raise TypeError("Pi stream must be text")
        records, errors = parse_trace_jsonl_text(raw_text)
        return cls.from_records(records, errors)


def stream_usage_and_cost(raw_text: str, *, source: str | None = None,
                          pi_stream: PiStream | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
    """Normalize one runner stream without confusing Pi cumulative events for
    deltas — resolved by the source's registered trace dialect."""
    resolved_source = "generic" if source is None else source
    return trace_dialect_for(resolved_source).usage_and_cost(raw_text, pi_stream)


def write_trace_artifacts(
    run_dir: Path,
    trace_text: str,
    *,
    source: str,
    metadata: dict[str, Any] | None = None,
    extra_metrics: dict[str, Any] | None = None,
    environment: dict[str, Any] | None = None,
    write_metadata: bool = True,
    out_events: Path | None = None,
    out_metrics: Path | None = None,
    write_raw_trace: bool = True,
    pi_stream: PiStream | None = None,
    process_observation_complete: bool | None = None,
    provider_response_complete: bool | None = None,
    artifact_set_complete: bool | None = None,
    retain_invalid_provider_trace: bool = False,
    trace_utf8_valid: bool = True,
) -> tuple[dict[str, Any], dict[str, Any]]:
    if not isinstance(retain_invalid_provider_trace, bool):
        raise TypeError("retain_invalid_provider_trace must be boolean")
    if not isinstance(trace_utf8_valid, bool):
        raise TypeError("trace_utf8_valid must be boolean")
    for label, value in (("process_observation_complete", process_observation_complete),
                         ("provider_response_complete", provider_response_complete),
                         ("artifact_set_complete", artifact_set_complete)):
        if value is not None and not isinstance(value, bool):
            raise TypeError(f"{label} must be boolean or None")
    run_dir.mkdir(parents=True, exist_ok=True)
    if write_raw_trace:
        (run_dir / "trace.jsonl").write_text(trace_text, encoding="utf-8")
    parsed_records, parsed_errors, record_lines = parse_trace_jsonl_text_with_lines(
        trace_text, strict_json_errors=not retain_invalid_provider_trace)
    if source.casefold() == "pi" and pi_stream is not None:
        records, parse_errors = list(pi_stream.records), list(pi_stream.parse_errors)
        if len(record_lines) != len(records):
            record_lines = list(range(1, len(records) + 1))
    else:
        records, parse_errors = parsed_records, parsed_errors
    events, metrics = normalize_trace_records(
        records, source=source, pi_stream=pi_stream, record_lines=record_lines)
    semantic_protocol_error = trace_dialect_for(source).protocol_error(records, pi_stream)
    if semantic_protocol_error:
        existing_protocol_errors = metrics.get("trace_protocol_errors")
        protocol_errors = (list(existing_protocol_errors)
                           if isinstance(existing_protocol_errors, list) else [])
        protocol_errors.append(semantic_protocol_error)
        metrics["trace_protocol_errors"] = protocol_errors[:20]
    if not trace_utf8_valid:
        protocol_errors = list(metrics.get("trace_protocol_errors") or [])
        protocol_errors.append("trace transport is not valid UTF-8")
        metrics["trace_protocol_errors"] = protocol_errors[:20]
    if parse_errors:
        metrics["parse_errors"] = parse_errors[:20]
        metrics["errors"] = int(metrics.get("errors", 0) or 0) + len(parse_errors)
    # A trace-derived count is observed only when at least one valid event was
    # captured and parsing completed. Completion is derived here and reserved:
    # arbitrary caller metrics cannot promote an absent trace.
    trace_observation_complete = (
        bool(records) and not parse_errors and not metrics.get("trace_protocol_errors"))
    reserved = set(metrics) | {
        "observation_complete", "trace_observation_complete", "process_observation_complete",
        "provider_response_complete", "operation_observation_complete",
        "artifact_set_complete", "observation_evidence", "telemetry",
        "telemetry_schema_version", "usage_normalized", "cost_normalized",
        "input_tokens", "output_tokens", "total_tokens", "cache_read_tokens",
        "cache_write_tokens", "cache_creation_tokens", "cost_usd", "otel",
        "parse_errors", "trace_protocol_errors",
        "skill_invoked", "skill_invocation_evidence", "retries",
        "repeated_command_max", "commands", "tool_calls", "file_reads",
        "file_writes", "errors", "schema_version", "source",
    }
    # Process-level duration/exit facts are authoritative named observations;
    # every trace-owned counter and schema field is non-overridable.
    process_metric_keys = {"returncode", "timed_out", "elapsed_ms"}
    collisions = (reserved - process_metric_keys) & set(extra_metrics or {})
    if collisions:
        raise ValueError(f"extra_metrics cannot override derived evidence: {', '.join(sorted(collisions))}")
    if extra_metrics:
        metrics.update(extra_metrics)
    generic_complete = metrics.get("observation_complete")
    if not isinstance(generic_complete, bool):
        generic_complete = (metadata or {}).get("observation_complete")
    if process_observation_complete is None:
        explicit = (metadata or {}).get("process_observation_complete")
        process_observation_complete = explicit if isinstance(explicit, bool) else generic_complete if isinstance(generic_complete, bool) else None
    if provider_response_complete is None:
        explicit = (metadata or {}).get("provider_response_complete")
        provider_response_complete = explicit if isinstance(explicit, bool) else generic_complete if isinstance(generic_complete, bool) else None
    evidence = telemetry_domain.ObservationEvidence(
        telemetry_domain.ObservationEvidence.state(process_observation_complete),
        telemetry_domain.ObservationEvidence.state(provider_response_complete),
        telemetry_domain.ObservationEvidence.state(trace_observation_complete),
        telemetry_domain.ObservationEvidence.state(artifact_set_complete),
    )
    # Retain the legacy single-axis field as a derived compatibility alias for
    # provider-response completeness.  It is reserved above, so a caller cannot
    # use extra_metrics to promote a failed or partial provider response.
    legacy_observation_complete = (
        provider_response_complete if isinstance(provider_response_complete, bool)
        else generic_complete if isinstance(generic_complete, bool)
        else False
    )
    metrics.update({
        "observation_complete": legacy_observation_complete,
        "trace_observation_complete": trace_observation_complete,
        "process_observation_complete": process_observation_complete,
        "provider_response_complete": provider_response_complete,
        "operation_observation_complete": evidence.operation_complete,
        "artifact_set_complete": artifact_set_complete,
        "observation_evidence": evidence.to_dict(),
    })
    # Telemetry precedence: an explicit provider/estimated/not-applicable block
    # supplied by a runner wins over a trace-derived block. Both artifacts then
    # receive the same v3 envelope, including an explicit unavailable state when
    # no value was observed. A missing trace can never become numeric zero.
    for key in ("usage_normalized", "cost_normalized"):
        block = (metadata or {}).get(key)
        if isinstance(block, dict) and block.get("source") in {"provider_reported", "trace_normalized", "price_table_estimated", "estimated", "not_applicable"}:
            metrics[key] = block
        metrics.setdefault(key, {"source": "missing"})
    # Legacy scalar mirrors remain useful to existing report readers, but are
    # derived here from the authoritative normalized provider/trace blocks.
    # They are reserved above and cannot be injected through extra_metrics.
    usage_block = metrics["usage_normalized"]
    if isinstance(usage_block, dict):
        for key in ("input_tokens", "output_tokens", "total_tokens",
                    "cache_read_tokens", "cache_write_tokens"):
            value = usage_block.get(key)
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                metrics[key] = value
        if isinstance(usage_block.get("cache_write_tokens"), int):
            metrics["cache_creation_tokens"] = usage_block["cache_write_tokens"]
    cost_block = metrics["cost_normalized"]
    if (isinstance(cost_block, dict) and cost_block.get("currency") == "USD"
            and isinstance(cost_block.get("total_cost"), (int, float))
            and not isinstance(cost_block.get("total_cost"), bool)):
        metrics["cost_usd"] = float(cost_block["total_cost"])
    # Metrics contains the resolved usage/cost precedence. Metadata is useful
    # for identity/basis but must not let an explicit `source: missing` erase a
    # usable trace-normalized observation.
    envelope_input = {**(metadata or {}), **metrics}
    # Process, provider response, trace, and artifact-set completeness are
    # independent axes. The typed evidence value above is the only owner of
    # operation completeness; no generic success boolean promotes another axis.
    # A crash/timeout can leave a syntactically valid partial JSONL trace. Keep
    # its legacy debugging fields, but v3 must not present trace-derived usage
    # or cost as complete measurement evidence. Provider-reported blocks remain
    # valid independent observations.
    if not evidence.operation_complete:
        for key in ("usage_normalized", "cost_normalized"):
            block = envelope_input.get(key)
            if isinstance(block, dict) and block.get("source") == "trace_normalized":
                envelope_input[key] = {"source": "missing"}
    envelope = telemetry_domain.telemetry_envelope(
        envelope_input,
        source=source,
        population=str(envelope_input.get("population") or "answer"),
    )
    metrics["telemetry_schema_version"] = 3
    metrics["telemetry"] = envelope
    write_json(out_events or run_dir / "events.json", events)
    write_json(out_metrics or run_dir / "metrics.json", metrics)
    if environment:
        write_json(run_dir / "environment.json", environment)
    if write_metadata:
        existing: dict[str, Any] = {}
        if metadata:
            existing.update(metadata)
        existing.update({k: v for k, v in metrics.items() if k not in {"schema_version", "source"}})
        existing.setdefault("usage_normalized", {"source": "missing"})
        existing.setdefault("cost_normalized", {"source": "missing"})
        existing["telemetry_schema_version"] = 3
        existing["telemetry"] = envelope
        existing["trace_source"] = source
        write_json(run_dir / "metadata.json", existing)
    return events, metrics


def final_answer_from_events(events: dict[str, Any]) -> str:
    messages = [e for e in events.get("events", []) if isinstance(e, dict) and e.get("type") == "message"]
    for event in reversed(messages):
        role = str(event.get("role", event.get("name", ""))).casefold()
        if role and role not in {"assistant", "message", ""}:
            continue
        text = event.get("output_summary") or event.get("input_summary")
        if isinstance(text, str) and text.strip():
            return text.strip()
    return ""
