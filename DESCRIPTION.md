# Integrate Google Antigravity (`agy`) as a native backend

Replaces #62, which is closed. This branch shares no commits with it: the
adapter was re-authored from scratch on top of the typed abstractions that
landed in #65, #71 and #81, as recommended in that thread. The empirical
protocol knowledge from #62 is preserved; the structure is not.

## Summary

Adds `agy` as a native answer and judge backend, so evaluations can run
`--agent agy` and `--judge-backend agy` and have their tool activity, telemetry
and evidence measured on the same footing as the existing backends.

The work is in three parts, each independently useful:

1. **A typed isolation/containment posture on the backend registry.** Capability
   booleans cannot express "safe only on a disposable host". `IsolationPosture`
   can, and the registry refuses to advertise unattended execution on a backend
   that is not contained.
1. **An adversarial conformance pack applied to every native answer backend** —
   claude, codex, gemini, vibe and agy — rather than to agy alone.
1. **The agy answer and judge adapters**, built on `ProcessInvocationPlan`,
   `InvocationResult` and lifecycle-bearing `JudgeInvocation`, with all wire
   parsing confined to a new `agy_contracts.py`.

Trigger and ablation support are deliberately not advertised; they are gated on
live activation proof and follow separately.

## What #62 taught us

#62 grew from a straightforward adapter into a defence-in-depth validation suite
and still leaked defects at every review round. The failures shared one shape,
and it is worth stating plainly because it drove every decision here:

- **A tolerant parser turns missing data into good news.** Accepting a bare JSON
  envelope when `stream-json` was requested caused trigger runs to score as
  clean no-triggers while silently dropping every step update. The run
  "succeeded".
- **Unmapped input is indistinguishable from an idle model.** `agy` emits step
  types the adapter did not know, including the literal `unknown`. Ignoring them
  scored tool activity as zero on runs that did real work — a gap and a genuine
  no-op produce the same number.
- **Capability booleans cannot express conditional safety.** #62 advertised
  answer, judge, autonomous trigger and ablation support while simultaneously
  recording `config_isolated=False`, `ambient_tools_auto_approved=True`,
  `sandbox_contains_run=False` and demonstrated writes outside the workspace.
  Nothing in the type system objected.
- **Search intent is not activation.** Treating a search for a skill as evidence
  that the skill was read inflates trigger results.
- **Adding another check does not remove the class of bug.** Each round found
  fewer and less serious defects, which felt like progress but never produced
  confidence that the next round would find none.

The correction, per the review thread: parse provider protocols into closed
states *before* anything derives success, telemetry or trigger evidence, and
make unknown or incomplete evidence unavailable rather than zero, false or
success.

## What is done differently

| #62                                                                                             | This PR                                                                                                                                                                                                 |
| ----------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Tolerant parse, then validate the result                                                        | Wire bytes become frozen typed events in `agy_contracts.py` before any consumer sees them                                                                                                               |
| `json.loads(..., strict=False)` to absorb `agy`'s invalid control characters                    | No permissive JSON. Where a non-conforming envelope must still be read it is a named, versioned legacy dialect that preserves its own invalidity and is never described as strict JSON                  |
| `shlex.split(agy_cmd)`, allowing a free-form launcher prefix to reinterpret harness-owned flags | One literal executable token. Prompt, model and schema values are bound so dash-prefixed data cannot become CLI structure                                                                               |
| Parallel provider-shaped dictionaries and `cast(...)`-based recovery                            | `ProcessInvocationPlan`, `InvocationResult`, lifecycle-bearing `JudgeInvocation`, one complete `Provider.AGY` registry row                                                                              |
| Capability booleans                                                                             | Typed `IsolationPosture`; flipping `autonomous_trigger=True` on an uncontained backend fails at import unless an operator opt-in is set, and two tests assert no shipped backend uses that escape hatch |
| One model identity                                                                              | Requested, configured and provider-resolved identities kept separate; zero, one and many reported models treated distinctly                                                                             |
| Checks added in response to observed bugs                                                       | Four conservation laws, each with a meta-test that reintroduces the original defect — a conservation law that silently holds is worse than none                                                         |

**The conservation laws** answer a different question from the conformance pack.
Conformance asks whether bad input fails closed. These ask whether the
translation is *total*: every terminal tool step in an accepted stream is
accounted for, every activity kind reaches a metric, every advertised tool is
explicitly classified, and the graded verdict agrees with what the run
observably did.

One of them required a parser change to be meaningful. A step carrying tool
fields under a step type the adapter does not translate was previously skipped,
so a renamed step type would have conserved trivially by being invisible to both
the adapter and the law. It now fails closed.

## Acceptance

Closed statement of what this PR does and does not claim.

| Claim                                        | Status                                    | Evidence                                                                       |
| -------------------------------------------- | ----------------------------------------- | ------------------------------------------------------------------------------ |
| Answer runs                                  | **supported**                             | `--agent agy`                                                                  |
| Judge runs                                   | **supported**                             | `--judge-backend agy`, stream-json + `--json-schema`                           |
| Autonomous trigger                           | **deliberately unavailable**              | no live activation observation; the registry also forbids it while uncontained |
| Trigger ablation                             | **deliberately unavailable**              | same                                                                           |
| Tool replay                                  | **not applicable**                        | `agy` exposes no replay seam                                                   |
| Dollar cost                                  | **explicitly missing**                    | `agy` reports tokens, no cost figure                                           |
| Token usage                                  | **provider-reported when a model ran**    | all-zero blocks normalize to absent, never to zero                             |
| Containment                                  | **uncontained; disposable host required** | antigravity-cli #36 and #155 both open as of 2026-08-01                        |
| Search ≠ activation                          | **fixed by construction**                 | disjoint partitions; `AgySearch` carries no path field                         |
| Absent telemetry ≠ zero                      | **fixed by construction**                 | `AgyUsagePresent` refuses all-zero counters                                    |
| Provider error survives nonzero exit         | **fixed**                                 | verbatim 1.1.9 auth-failure capture asserted                                   |
| Red-test evidence                            | **recorded**                              | `docs/evidence/agy-red-tests.md`, 7 failures against the stub                  |
| Token-backed answer semantics                | **outstanding**                           | needs credentials                                                              |
| Token-backed judge semantics                 | **outstanding**                           | needs credentials                                                              |
| Containment behaviour under production flags | **outstanding**                           | needs a disposable host                                                        |

### Containment requirement

`agy`'s `--sandbox` does not contain a run when combined with
`--dangerously-skip-permissions`; writes outside the workspace were
demonstrated. Both upstream issues behind this — antigravity-cli #36 and #155 —
were re-checked on 2026-08-01 and are still open, so the posture is current for
1.1.9 rather than inherited from 1.1.8. Issue #36 notes the bypass is a
regression from Gemini CLI, where the equivalent flags kept the sandbox
effective.

Pairing auto-approval with `--sandbox` in a test does not establish containment
when that sandbox is known to be bypassable. Accordingly this PR declares the
backend uncontained and requires a disposable host, rather than asserting an
isolation it cannot demonstrate.

### `agy` version and fixture provenance

Every fixture's origin is recorded in `tests/fixtures/agy/README.md`, including
which are real captures, which are derived from a real capture, and which are
hand-constructed.

- `stream-json-auth-failure.jsonl` — **verbatim 1.1.9**, byte-identical to
  captured stdout from a network-restricted container with no credentials.
- Nine fixtures ported from the #62 branch — captured against **1.1.8**;
  refreshing them needs an authenticated CLI. In redacted or derived fixtures
  only conversation id, workspace path, model name and response text are
  replaced; field names, event order, tool lifecycle and telemetry shapes remain
  faithful.
- Two fixtures — **hand-constructed** and labelled as such. They are not claims
  that a token-backed run was observed in this repository.

## Validation

- [x] `ty check --error-on-warning` — **All checks passed**
- [x] `ruff check .` — **All checks passed**
- [x] `python3 -m unittest discover tests` — **Ran 1385 tests**, 63 of them
  agy-specific

On the container used for development the suite is fully green. On a macOS host
without gcloud ADC, three `test_gemini_backend.GeminiIsolationTests` cases fail;
they fail identically on unmodified `main` at `2297000` (verified in a separate
worktree) and this branch touches no Gemini code or tests.

Guards are mutation-verified rather than merely present: the static narrowing
proofs and each conservation law were confirmed to fail when the defect they
exist to catch is reintroduced.

## Eval / docs impact

- [x] README, changelog and command reference record the backend, leading with
  the disposable-host requirement rather than mentioning it last
- [x] Tests added for all new CLI behaviour
- [x] Existing manifests remain answer-key safe

## Notes / risks

- **Containment vocabulary wants your sign-off.** The `IsolationPosture` member
  names were chosen without maintainer agreement. Renaming touches only
  `agent_capabilities.py`, `docs/agent-parity.md`,
  `tests/test_isolation_posture.py` and `tests/test_consolidation_guards.py`.
- **Four questions are answered provisionally**, each recorded in a code comment
  where it bites, all resolvable only with an authenticated CLI: whether
  `skill_search` deserves an evidence category distinct from generic search and
  from a file read; whether `--json-schema` actually constrains the stream-json
  final result; whether the `with_skill` arm needs `--disable-slash-commands` or
  whether agy's `.agents/skills` discovery *is* that expansion; and whether a
  dash-prefixed prompt is bound as a value once credentials are present.
- **The branch is not cleanly bisectable.** The tip is green, but three
  intermediate commits are not — two are the step that deliberately produces
  failing tests before the parser exists plus the step that inherited them, and
  one is a broken doc anchor fixed in the following commit. Measured by running
  the suite at each commit. History was left as-is rather than rewritten; if the
  containment posture is split into its own PR it needs rebasing ahead of the
  red step to stand alone.
