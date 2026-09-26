#!/usr/bin/env python3
"""Deterministic stub 'model' for the demo eval — no network, no API key.

run-codex copies the (real or ablated) skill tree into an isolated workspace and
runs this with that workspace as the cwd. We answer by keying off the skill content
that is actually mounted, so:

  - with_skill            -> both load-bearing pieces present  -> both assertions pass
  - ablation:no-severity  -> '## Severity rules' section removed -> severity label missing -> regression
  - ablation:no-checklist -> references/checklist.md blanked    -> file/line citation missing -> regression
  - without_skill         -> nothing mounted                   -> both assertions fail

That makes the materialized ablations produce a real, confirmable regression with
zero model calls — the example runs in CI.

The trace is equally genuine: every skill file the stub reads appears as one
Codex `command_execution` (`cat <path>`), so the run's events.json records the
path it actually took. `--loop` re-reads SKILL.md twice more before answering, the
way a flailing agent re-checks context it already has. The re-reads really happen,
change nothing in the answer, and exist to be caught by the path checks in
docs/did-my-skill-change-how-the-model-works.md.
"""
import glob
import json
import sys

sys.stdin.read()  # the task prompt; we deliberately key off the MOUNTED skill, not the text

reads: list[tuple[str, str]] = []  # (path, content) in the order actually read
for path in sorted(glob.glob("skills/**/*", recursive=True)):
    if path.endswith(".md"):
        try:
            with open(path, encoding="utf-8") as skill_file:
                reads.append((path, skill_file.read()))
        except OSError:
            pass
skill = "".join(content + "\n" for _, content in reads)

if "--loop" in sys.argv:
    for path in [p for p, _ in reads if p.endswith("SKILL.md")] * 2:
        with open(path, encoding="utf-8") as skill_file:
            reads.append((path, skill_file.read()))

lines = ["Review of the change:"]
if "Blocking, Minor, or Clean" in skill:                 # the '## Severity rules' section
    lines.append("Severity: Blocking — the change ships without a test.")
if "cite the file and line" in skill:                    # only in references/checklist.md
    lines.append("Per the review checklist, cite the file and line for each finding.")
if len(lines) == 1:
    lines.append("Looks fine to me; no concerns.")        # no skill mounted -> fails both assertions

answer = "\n".join(lines)
if "--output-last-message" in sys.argv:
    output_path = sys.argv[sys.argv.index("--output-last-message") + 1]
    with open(output_path, "w", encoding="utf-8") as output_file:
        output_file.write(answer)
    # run-codex consumes stdout as the provider trace. Emit the real lifecycle
    # shape so the offline example exercises the same fail-closed dialect as a
    # Codex CLI run instead of relying on a permissive empty-stream stub seam.
    print(json.dumps({"type": "thread.started", "thread_id": "offline-demo"}))
    print(json.dumps({"type": "turn.started"}))
    for number, (path, content) in enumerate(reads, 1):
        command = {"id": f"read-{number}", "type": "command_execution",
                   "command": f"cat {path}", "aggregated_output": ""}
        print(json.dumps({"type": "item.started",
                          "item": {**command, "exit_code": None, "status": "in_progress"}}))
        print(json.dumps({"type": "item.completed",
                          "item": {**command, "aggregated_output": content,
                                   "exit_code": 0, "status": "completed"}}))
    print(json.dumps({
        "type": "item.completed",
        "item": {"id": "answer", "type": "agent_message", "text": answer},
    }))
    print(json.dumps({
        "type": "turn.completed",
        "usage": {"input_tokens": 0, "output_tokens": 0},
    }))
else:
    print(answer)
