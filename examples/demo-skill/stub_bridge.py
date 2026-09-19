#!/usr/bin/env python3
"""Deterministic stub tool bridge for the demo fault case — no network, no API key.

`run-subagent --tool-bridge --agent-cmd "python3 stub_bridge.py"` runs this once per
prepared row and talks to it over JSON lines: the harness sends the prompt, we ask for
a tool call, the harness answers with the fault the case declares (or a recording, or
tells us to execute live), and we reply with the final answer.

Like stub_runner.py, we key off the skill that is actually mounted in the workspace:

  - with_skill            -> the "## When the test runner is unavailable" section is
                             present -> run the tests ONCE, take the denial as final,
                             say the runner is unavailable, review statically
  - ablation:no-fallback  -> that section removed -> retry the denied command, then
                             rubber-stamp ("Looks fine") -> regression on both assertions
  - without_skill         -> nothing mounted -> same as the ablation

So the with-skill arm shows one error result and a declared fallback; the other arms
show two error results and no fallback. That is the paired signal the fault case
measures, and the `no-fallback` ablation confirms which skill text carries it.
"""
import glob
import json
import os
import sys


def send(message: dict) -> None:
    sys.stdout.write(json.dumps(message) + "\n")
    sys.stdout.flush()


def receive() -> dict:
    line = sys.stdin.readline()
    if not line:
        raise SystemExit(1)
    return json.loads(line)


def call_tool(call_id: str, tool: str, tool_input: dict) -> dict:
    """One tool exchange. The harness decides the result: a declared fault, a
    recording, or (outside strict replay) an `execute` request we answer with
    what the tool observed."""
    send({"type": "tool_call", "id": call_id, "tool": tool, "input": tool_input})
    while True:
        reply = receive()
        if reply.get("type") == "execute" and reply.get("id") == call_id:
            # Live path: this stub has no real test runner, so report the truth.
            send({"type": "tool_observed", "id": call_id,
                  "output": "stub bridge: no live test runner in this workspace",
                  "is_error": True})
            continue
        if reply.get("type") == "tool_result" and reply.get("id") == call_id:
            return reply
        raise SystemExit(f"unexpected bridge message: {reply!r}")


def main() -> None:
    opening = receive()
    workspace = opening.get("workspace") or "."
    skill = ""
    for path in sorted(glob.glob(os.path.join(workspace, "skills", "**", "*.md"), recursive=True)):
        try:
            with open(path, encoding="utf-8") as skill_file:
                skill += skill_file.read() + "\n"
        except OSError:
            pass
    knows_fallback = "do not retry the same denied command" in skill

    first = call_tool("call-1", "run_tests", {"command": "./run-tests.sh"})
    if not first.get("is_error"):
        answer = "Tests passed. Severity: Clean."
    elif knows_fallback:
        answer = ("Test runner unavailable: the sandbox denied ./run-tests.sh, so I did not retry it "
                  "and reviewed the change statically instead. Severity: Blocking — the new endpoint "
                  "ships without a test.")
    else:
        # No guidance for a denied command: retry it, then rubber-stamp.
        call_tool("call-2", "run_tests", {"command": "./run-tests.sh"})
        answer = "Looks fine to me; no concerns."
    send({"type": "final", "answer": answer,
          "usage": {"input_tokens": 0, "output_tokens": 0}})


if __name__ == "__main__":
    main()
