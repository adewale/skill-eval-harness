#!/usr/bin/env python3
"""Reference tool bridge: a live Claude agent loop whose tools the harness mediates.

    skill-benchmark run-subagent --tasks tasks.jsonl --runs eval-runs/bridge \\
        --tool-bridge --agent-cmd "python3 examples/tool-bridge/claude_tool_bridge.py" \\
        --tool-replay strict

The harness starts this process once per prepared row and speaks JSON lines on
stdin/stdout (see `tool_bridge_backend` in skill_benchmark.py). This bridge owns the
model loop and two live tools (`bash`, `read_file`, both confined to the workspace),
but before any tool result reaches the model it asks the harness, which may hand back
a declared fault or a recording instead, or (outside strict replay) tell this bridge to
execute the call and report what it observed so it can be recorded.

Requires the official Anthropic Python SDK (`pip install anthropic`) and credentials
(`ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`, or an `ant auth login` profile). The
harness passes the row's model; without one, `claude-opus-5` is used. This example is
opt-in and live: nothing in the harness test suite invokes it.

Deliberately NOT enabled: server-side refusal fallbacks. An eval run must execute on the
model it was designed for; a refusal is returned as a failed run (nonzero returncode)
rather than silently answered by a different model.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
from typing import Any

DEFAULT_MODEL = "claude-opus-5"
MAX_TOOL_ROUNDS = 40

TOOLS: list[dict[str, Any]] = [
    {
        "name": "bash",
        "description": "Run one shell command in the task workspace and return its stdout, stderr and exit code.",
        "input_schema": {
            "type": "object",
            "properties": {"command": {"type": "string", "description": "The shell command to run."}},
            "required": ["command"],
            "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "name": "read_file",
        "description": "Read a UTF-8 text file from the task workspace by relative path.",
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string", "description": "Path relative to the workspace."}},
            "required": ["path"],
            "additionalProperties": False,
        },
        "strict": True,
    },
]


def send(message: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(message, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def receive() -> dict[str, Any]:
    line = sys.stdin.readline()
    if not line:
        raise SystemExit("harness closed the bridge")
    parsed = json.loads(line)
    if not isinstance(parsed, dict):
        raise SystemExit("bridge messages must be JSON objects")
    return parsed


def execute_live(workspace: str, tool: str, tool_input: dict[str, Any]) -> tuple[Any, bool]:
    """The bridge's own tools. Returns (output, is_error)."""
    if tool == "bash":
        try:
            proc = subprocess.run(
                str(tool_input.get("command", "")), shell=True, cwd=workspace,
                capture_output=True, text=True, timeout=300, check=False)
        except subprocess.TimeoutExpired:
            return {"stdout": "", "stderr": "command timed out after 300s", "exit_code": 124}, True
        return ({"stdout": proc.stdout[-8000:], "stderr": proc.stderr[-8000:],
                 "exit_code": proc.returncode}, proc.returncode != 0)
    if tool == "read_file":
        rel = str(tool_input.get("path", ""))
        target = os.path.realpath(os.path.join(workspace, rel))
        if not target.startswith(os.path.realpath(workspace) + os.sep):
            return f"refused: {rel!r} is outside the workspace", True
        try:
            with open(target, encoding="utf-8") as handle:
                return handle.read()[:200_000], False
        except OSError as exc:
            return f"{type(exc).__name__}: {exc}", True
    return f"unknown tool {tool!r}", True


def mediated_tool_result(workspace: str, tool: str, tool_input: dict[str, Any]) -> dict[str, Any]:
    """Ask the harness for this call's result. It replies with a fault, a
    recording, or an `execute` request that we answer from our own tools."""
    call_id = f"call-{uuid.uuid4().hex[:12]}"
    send({"type": "tool_call", "id": call_id, "tool": tool, "input": tool_input})
    while True:
        reply = receive()
        if reply.get("id") != call_id:
            raise SystemExit(f"bridge reply for unknown call: {reply!r}")
        if reply.get("type") == "execute":
            output, is_error = execute_live(workspace, tool, tool_input)
            send({"type": "tool_observed", "id": call_id, "output": output, "is_error": is_error})
            continue
        if reply.get("type") == "tool_result":
            return reply
        raise SystemExit(f"unexpected bridge message: {reply!r}")


def as_tool_result_text(output: Any) -> str:
    return output if isinstance(output, str) else json.dumps(output, ensure_ascii=False)


def main() -> None:
    try:
        import anthropic  # ty: ignore[unresolved-import]
    except ImportError:
        raise SystemExit("pip install anthropic") from None

    opening = receive()
    if opening.get("type") != "prompt":
        raise SystemExit("expected the harness prompt message first")
    workspace = str(opening.get("workspace") or os.getcwd())
    model = str(opening.get("model") or DEFAULT_MODEL)
    client = anthropic.Anthropic()

    messages: list[dict[str, Any]] = []
    for turn in opening.get("history") or []:
        messages.append({"role": "user", "content": str(turn.get("prompt", ""))})
        messages.append({"role": "assistant", "content": str(turn.get("answer", ""))})
    messages.append({"role": "user", "content": str(opening.get("prompt", ""))})

    usage = {"input_tokens": 0, "output_tokens": 0}
    trace: list[dict[str, Any]] = []
    answer = ""
    returncode = 0
    for _round in range(MAX_TOOL_ROUNDS):
        with client.messages.stream(
            model=model,
            max_tokens=64000,
            system=(f"You are working in the directory {workspace}. Use the tools to inspect "
                    "and run things there; do not assume a command succeeded without running it."),
            tools=TOOLS,
            messages=messages,
        ) as stream:
            response = stream.get_final_message()
        usage["input_tokens"] += int(response.usage.input_tokens or 0)
        usage["output_tokens"] += int(response.usage.output_tokens or 0)
        text_blocks = [block.text for block in response.content if block.type == "text"]
        for text in text_blocks:
            trace.append({"type": "message", "role": "assistant", "content": text[:2000]})

        if response.stop_reason == "refusal":
            # The requested model declined; the run fails rather than routing elsewhere.
            answer, returncode = "", 1
            break
        if response.stop_reason == "pause_turn":
            messages.append({"role": "assistant", "content": response.content})
            continue
        if response.stop_reason != "tool_use":
            answer = "\n".join(text_blocks).strip()
            if response.stop_reason == "max_tokens":
                trace.append({"type": "message", "role": "system",
                              "content": "stopped at max_tokens; answer may be truncated"})
            break

        messages.append({"role": "assistant", "content": response.content})
        tool_results: list[dict[str, Any]] = []
        for block in response.content:
            if block.type != "tool_use":
                continue
            tool_input = block.input if isinstance(block.input, dict) else {}
            result = mediated_tool_result(workspace, block.name, tool_input)
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": as_tool_result_text(result.get("output")),
                "is_error": bool(result.get("is_error")),
            })
        messages.append({"role": "user", "content": tool_results})
    else:
        trace.append({"type": "message", "role": "system",
                      "content": f"stopped after {MAX_TOOL_ROUNDS} tool rounds"})
        answer, returncode = "", 1

    final: dict[str, Any] = {"type": "final", "answer": answer, "returncode": returncode,
                             "trace": trace, "usage": {**usage, "total_tokens": usage["input_tokens"] + usage["output_tokens"]}}
    send(final)


if __name__ == "__main__":
    main()
