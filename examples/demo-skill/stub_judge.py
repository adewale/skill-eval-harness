#!/usr/bin/env python3
"""Deterministic stub 'judge' for the demo eval — no network, no API key.

`skill-benchmark judge --judge-cmd` pipes one grading prompt to stdin and expects
a JSON verdict on stdout. This stub is the judge half of the offline demo, in two
modes, so the judge-trust loop (docs/can-i-trust-my-judge.md) runs in CI:

  default (careful)  — reads ONLY `candidate_output` from the prompt payload and
                       passes iff the review states a reason (an em-dash
                       justification) and names the concrete gap (the missing
                       test). Because it never greps the case prompt or the
                       rubric, it rejects the empty and master-key negative
                       controls naturally — nothing is hard-coded against them.
  --lenient          — a rubber-stamp: passes everything. This is the judge the
                       calibration commands exist to catch: it leaks both
                       negative controls (judge-robustness), inflates the
                       baseline (compare-judges), and scores kappa 0.0 against
                       human labels (judge-alignment).

A per-step assertion (`per_step: true`) sends `trajectory_steps` instead of
asking for one verdict: the careful judge then reads ONLY those steps and marks
a step unsound when it repeats an earlier step verbatim — a redundant action on
context the run already has (docs/did-my-skill-change-how-the-model-works.md).
`--lenient` marks every step sound.

The verdict shapes match what a real model judge is asked for:
{"passed": bool, "score": number, "rationale": str}, or for per-step
{"criteria": [{"name": "step-N", "met": bool}, ...], "rationale": str}.
"""
import json
import sys

text = sys.stdin.read()
# The grading prompt ends with the task payload as indented JSON; the payload's
# opening brace is the only line-anchored "{" followed by a newline (the schema
# hint above it is dumped compact, on one line).
start = text.index("\n{\n")
payload = json.loads(text[start + 1:])
lenient = "--lenient" in sys.argv[1:]

steps = payload.get("trajectory_steps")
if isinstance(steps, list):
    seen: set[tuple[str, str]] = set()
    criteria = []
    redundant: list[str] = []
    for step in steps:
        action = (str(step.get("type")), str(step.get("input_summary") or step.get("name") or ""))
        met = lenient or action not in seen
        criteria.append({"name": step["step"], "met": met})
        if not met:
            redundant.append(str(step["step"]))
        seen.add(action)
    rationale = ("Looks good to me." if lenient
                 else f"{', '.join(redundant)} repeat an earlier step verbatim" if redundant
                 else "every step is a distinct action")
    print(json.dumps({"criteria": criteria, "rationale": rationale}))
    sys.exit(0)

out = payload.get("candidate_output") or ""
if lenient:
    passed, rationale = True, "Looks good to me."
else:
    reasoned = "—" in out          # an em-dash justification ("... — because ...")
    names_gap = "test" in out.lower()   # names the concrete missing artifact
    passed = reasoned and names_gap
    rationale = ("states a reason and names the missing test" if passed
                 else "no justification for the finding, or the concrete gap (the missing test) is never named")

print(json.dumps({"passed": passed, "score": 1.0 if passed else 0.0, "rationale": rationale}))
