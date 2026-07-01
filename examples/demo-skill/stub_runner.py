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
"""
import sys, glob

sys.stdin.read()  # the task prompt; we deliberately key off the MOUNTED skill, not the text

skill = ""
for path in sorted(glob.glob("skills/**/*", recursive=True)):
    if path.endswith(".md"):
        try:
            skill += open(path, encoding="utf-8").read() + "\n"
        except OSError:
            pass

lines = ["Review of the change:"]
if "Blocking, Minor, or Clean" in skill:                 # the '## Severity rules' section
    lines.append("Severity: Blocking — the change ships without a test.")
if "cite the file and line" in skill:                    # only in references/checklist.md
    lines.append("Per the review checklist, cite the file and line for each finding.")
if len(lines) == 1:
    lines.append("Looks fine to me; no concerns.")        # no skill mounted -> fails both assertions

print("\n".join(lines))
