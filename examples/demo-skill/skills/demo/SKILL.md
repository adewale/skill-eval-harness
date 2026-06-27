---
name: demo-reviewer
description: Demo skill for the Skill Eval Harness example. Use it to review a proposed change and label the severity of each finding.
---

# Demo Reviewer

A deliberately tiny skill that exists only to demonstrate the harness end to end
(materialized ablations included). It has exactly two load-bearing pieces — a
severity rule and a checklist reference — each targeted by one ablation below.

## Severity rules

Label every finding with one of: Blocking, Minor, or Clean. State the label
explicitly so the reader can triage at a glance. A change that ships without a
test is at least Blocking.

## Evidence

Back each finding with concrete evidence and follow the shared review checklist:
see [the review checklist](references/checklist.md).
