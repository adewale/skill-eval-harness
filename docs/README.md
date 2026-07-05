# Docs index

Four kinds of document live here. They age differently — a spec is finished when the
feature ships, a journey rots the moment its commands drift — so know which kind you
are reading (and which kind you are writing).

## User journeys

Each starts from a question a skill author actually has, and ends at a decision. The
title is the question; the body is the loop that answers it, runnable on
[`examples/demo-skill/`](../examples/demo-skill/) wherever possible.

| Question | Doc |
|---|---|
| How do I write an eval suite for my skill? | [`authoring-evals.md`](authoring-evals.md) |
| How do I make my skill trigger reliably? | [`tuning-skill-activation.md`](tuning-skill-activation.md) |
| Which parts of my skill are load-bearing? | [`ablation-study-walkthrough.md`](ablation-study-walkthrough.md) |
| Is my skill worth its tokens? | [`is-my-skill-worth-its-tokens.md`](is-my-skill-worth-its-tokens.md) |
| How do I gate my skill repo's CI on this? | [`gating-ci-on-evals.md`](gating-ci-on-evals.md) |
| How do I upgrade a v1 manifest to v2? | [`migrating-evals.md`](migrating-evals.md) |

The unwritten ones are tracked in [`TODO.md`](../TODO.md) under "User journeys the
code supports but the docs don't walk" — each names the machinery that already
exists, so writing the journey is documentation work, not feature work.

## Concepts

[`vocabulary.md`](vocabulary.md) is the canonical glossary — a term is **defined there once**,
and the three docs below apply a lens to those terms rather than redefine them:
[`abstractions.md`](abstractions.md) (the engineering shape — what each object is in the code),
[`academic-grounding.md`](academic-grounding.md) (the research construct behind each term), and
[`evals-are-not-tests.md`](evals-are-not-tests.md) (how to read the number a term names). When
a definition changes, change it in the glossary and let the lenses follow. Standing apart from
the glossary: [`architecture.md`](architecture.md) (how the pipeline fits together, a flow not a
definition) and [`agent-parity.md`](agent-parity.md) (which surfaces Codex, Claude, Pi, Jetty,
subagent, and stub support).

## Reference

[`commands.md`](commands.md) is the full per-command reference (flags, examples, output shapes);
the [README](../README.md#commands) carries the grouped index.

## Specs

Design records for shipped or in-flight subsystems:
[`skill-ablation-spec.md`](skill-ablation-spec.md),
[`trace-aware-eval-spec.md`](trace-aware-eval-spec.md),
[`eval-framework-roadmap-spec.md`](eval-framework-roadmap-spec.md),
[`jetty-support-spec.md`](jetty-support-spec.md).

## Audits

Point-in-time findings: [`repo-effectiveness-audit.md`](repo-effectiveness-audit.md).

## Adding a user journey

The mold, distilled from `tuning-skill-activation.md` and
`ablation-study-walkthrough.md`:

1. **Title it with the user's question**, in their words ("How do I make my skill
   trigger reliably?"), and open with the principle that reframes it — usually why
   the naive ask (determinism, a single number) is not available and what the
   measurable substitute is.
2. **Make it runnable on the bundled demo.** An offline path first
   (`examples/demo-skill` + a stub) so the reader proves their setup for free, then
   the live command. If the demo can't carry the journey, extend the demo — that is
   what it is for.
3. **Paste real output, dated.** Run the exact command you print and show what it
   printed, environment noted. An invented table teaches the reader to expect the
   wrong shape; a real one (like the Haiku 1/3 cell) usually teaches more than the
   prose around it.
4. **Show how to read the output, symptom by symptom.** Each branch names what the
   reader sees, what it means, and the edit it calls for — with a worked example
   from a real skill where one exists (`LESSONS_LEARNED.md` is the quarry).
5. **Name what keeps the measurement honest**, and why: each rule earned its place
   by a wrong number somewhere. Say which evidence class the numbers carry.
6. **End at the boundary**: what the journey does not establish, and where the
   deeper tool picks up.

Housekeeping when the doc lands: add its row to the main README's documentation
map and the table above; tick (or add) its entry in the TODO backlog; keep any
`name:line` code references accurate — `tests/test_doc_refs.py` fails on drift;
if the journey adds a command, `CONTRIBUTING.md`'s checklist applies to the
command too.
