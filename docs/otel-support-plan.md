# Minimal end-to-end OpenTelemetry support roadmap

Status: proposed; source-audited 2026-07-31 against `main` at `99da1f8` and
the active PR/issue snapshot listed below.

## Decision

Build OpenTelemetry support as independently reviewable slices. Slices 0–1 are
the implementation-ready MVP: opt-in traces for native answer-runner attempts.
Slices 2–6 extend the same contract across planning, every execution and import
surface, grading and judging, aggregation and publication, and finally
operational signals. “Minimal” means the smallest useful span vocabulary and no
function-by-function tracing; it does not mean stopping at the first runner.

One answer attempt initially becomes one bounded trace that can join
runner-native spans when the child process understands W3C Trace Context:

```text
skill.eval.run
├── skill.eval.runner.invoke
│   └── runner-native model/tool spans, when available
└── skill.eval.artifacts.write
```

This document covers the whole artifact pipeline; “not in the first slice” does
not mean “not planned.” The [coverage inventory](#verified-pipeline-and-coverage-contract)
accounts for every current CLI command, and the [post-MVP slices](#post-mvp-roadmap)
give every durable handoff an owner and entry gate. Items that should never enter
the harness core are listed separately as [permanent non-goals](#permanent-non-goals).

The harness's saved artifacts remain the grading and debugging source of truth.
The existing `otel` blocks in `events.json` and `metrics.json` are normalized
offline evidence; this plan does not replay them as live spans or make successful
export part of evaluation validity.

## Verified pipeline and coverage contract

The source audit corrected three assumptions in the first version of this plan:

- the pipeline is an artifact graph, not a single `prepare → run → grade → report`
  process;
- `grade`, `benchmark`, `token-overhead`, `export-anthropic`, `aggregate`, and
  run-aware audits can call the shared grading owner directly, so a
  `grade` command trace cannot be the parent of later aggregation; and
- there is no `run-vibe` compatibility command. Vibe runs through
  `run-agent --agent vibe`; only `run-codex` and `run-claude` are compatibility
  frontends.

The accompanying corrections to [`architecture.md`](architecture.md) and
[`abstractions.md`](abstractions.md) record those facts plus the authoritative
`answer-design.json`, `judge_input_sha256`, and `artifact-commit.json`
boundaries. The command inventory was checked against `build_arg_parser`, the
installed console entry points, shared-function call sites, tests, complete
reachable history, and the active PR/issue set.

End-to-end coverage means all five of these statements are true:

1. When OTel is enabled, every CLI invocation has one bounded command root with
   an enumerated command name, outcome, and run-group ID when available. It is a
   navigation and operational span, never the parent of an unbounded matrix.
2. Every expensive unit has its own bounded root: one answer attempt, trigger
   query repetition, remote Jetty attempt, grading unit, opt-in external
   script/embedding oracle, or judge invocation.
3. Every durable handoff carries a content-free correlation envelope or an
   explicit `unavailable` state: `answer-design.json` plus prepared tasks,
   remote submissions/results, run artifacts, judge tasks/results, comparison
   tasks/results, benchmark reports, and published output.
4. Every command that imports, migrates, enriches, or publishes artifacts traces
   validation plus atomic commit. It never overwrites the original producer's
   correlation or manufactures a causal parent.
5. A synchronous subprocess or RPC continues the active W3C context. A durable
   queue/file handoff, human step, delayed resume, batch fan-in, or deliberate
   trust-boundary restart begins a new trace and uses bounded span links when
   there is direct causality. Work sharing only experiment membership uses
   `skill.eval.run_group.id`, not a fabricated causal link. No trace or link set
   grows with total experiment size.

The inventory below assigns all current commands to that contract. A generic
command root is sufficient for model-free utilities unless a later column names
a more specific unit.

<!-- otel-command-inventory:start -->
| Pipeline surface | Current commands / entry points | Required telemetry owner | Slice |
|---|---|---|---:|
| Authoring and preflight | `validate`, `migrate`, `profile-skill`, `audit-manifest`, `materialize-ablations`, `suite-run` | command root; bounded validation/materialization and policy-gate operations | 2 |
| Task preparation and outbound handoff | `prepare`, `export-jetty`, `compare-tasks` | prepare/export root; durable run group plus task/export digest | 2 |
| Native and in-process answers | `run-agent`, `run-codex`, `run-claude`, `run-subagent`; Pi answer smoke | one root per prepared attempt; runner invocation and artifact commit | 1, 3 |
| Remote answer execution | `run-jetty` | one root per remote attempt; submit, poll, and download operations with remote links | 3 |
| Ingestion and artifact mutation | `import-jetty-results`, `import-trace`, `migrate-telemetry`, `compare-results` | new ingest/migration root; validate then atomic commit, linked to producer/export context | 3, 5 |
| Autonomous activation | `skill-trigger-matrix`, `skill-pi-trigger-eval`, `trigger-compare` | one root per query repetition; bounded comparison root over completed observations | 3, 5 |
| Deterministic evaluation and judges | `grade`, `judge`, `judge-robustness`, `compare-judges`, `judge-alignment` | one grade root per discovered run; bounded opt-in script/embed operations; judge task, invocation, result-ingest, and analysis operations | 4, 5 |
| Aggregation and analysis | `benchmark`, `aggregate`, `cost-summary`, `token-overhead`, `contamination`, `error-analysis`, `trend` | bounded aggregate/analysis roots linked to inputs; shared grade roots where those commands re-grade | 5 |
| Publication and generated follow-up | `report`, `render-viewer`, `export-anthropic`, `suggest-cases` | publish/render commit; separate model invocation only when `--generate-cmd` is used | 5 |
<!-- otel-command-inventory:end -->

Manual answer runners, human judges, and arbitrary external commands cannot be
made traceable retroactively. Their import boundary records either validated
correlation supplied by the producer or explicit absence. That is full coverage
without pretending the harness observed work that happened elsewhere.

## Repository prerequisites and landing order

This is a commit-pinned snapshot, not a claim that mutable branch counts will
remain current. The 2026-07-31 audit covered complete reachable history through
`90c77e2`, every then-open PR (#47, #61, #62, and #65), and every open issue
(#37, #48, #49, #52, and #64). The history repeatedly succeeds by stabilizing a
typed owner before instrumenting its callers; cross-cutting changes are then
rebased once after their prerequisites.

Current status and remaining order:

1. [#60](https://github.com/adewale/skill-eval-harness/pull/60), the focused `ty`
   gate; [#59](https://github.com/adewale/skill-eval-harness/pull/59), the first
   roadmap; [#57](https://github.com/adewale/skill-eval-harness/pull/57), the
   fail-closed evidence/design boundary; and
   [#58](https://github.com/adewale/skill-eval-harness/pull/58), the Unicode-safe
   comparison boundary; and [#63](https://github.com/adewale/skill-eval-harness/pull/63),
   this audited roadmap, are on `main` through `90c77e2`. #58 closed
   [#55](https://github.com/adewale/skill-eval-harness/issues/55).
2. This change lands [#61](https://github.com/adewale/skill-eval-harness/pull/61),
   the typed complete/incomplete/empty cohort owner plus the explicit terminal
   output and nonzero-exit contract. It completes the user-visible/runtime work
   behind the already-closed
   [#54](https://github.com/adewale/skill-eval-harness/issues/54); it is not
   superseded by #57.
3. Rebase and land [#65](https://github.com/adewale/skill-eval-harness/pull/65),
   the unified backend registry prompted by
   [#52 item 4](https://github.com/adewale/skill-eval-harness/issues/52). OTel
   conformance must enumerate that registry/capability model rather than only
   parser subcommands, so a new backend cannot miss answer, trigger, judge,
   workspace, trace, smoke, or privacy policy on one surface.
4. Rebase and land [#47](https://github.com/adewale/skill-eval-harness/pull/47)
   after repairing its current Python 3.12 failure. Preserve its token-backed
   live Jetty evidence while adapting the old branch to the typed evidence and
   unified-registry owners.
5. Complete the relevant [#64](https://github.com/adewale/skill-eval-harness/issues/64)
   type-clean slices and [#52 item 5](https://github.com/adewale/skill-eval-harness/issues/52)
   typed judge-invocation result before instrumenting those central-CLI regions.
6. Rebase and preferably split [#62](https://github.com/adewale/skill-eval-harness/pull/62)
   over #65 and the typed judge boundary. Agy then joins answer, trigger, judge,
   telemetry, privacy, and conformance coverage by registry capability. If #62
   lands after an OTel slice, add Agy in a focused parity PR rather than weakening
   an already-passing gate.

Slice 0 may begin after #63 because its facade is isolated and type-clean. Slice
1 waits for the runner/artifact portion of #64 or an explicit proof that the
identity-aware `ty` regression gate introduces no new diagnostic. Slice 2 applies
the same rule to command/control-plane code. Slice 3 requires #61, #65, the
rebased #47 live Jetty contract, and an atomic owner for `import-trace` /
`migrate-telemetry` augmentation. Slice 4 additionally requires #52 item 5;
adding span lifecycle to the current untyped backend/parse/merge dictionary
would make that boundary harder to close later.

The remaining issues are sequenced by when they change telemetry identity:

- [#48](https://github.com/adewale/skill-eval-harness/issues/48), native skill
  discovery, does not block slices 0–2. If it lands before Slice 3, that slice must
  include its typed activation mode and invocation-evidence availability; if it
  lands later, that parity extension is a separate PR with the same conformance
  gate.
- [#49](https://github.com/adewale/skill-eval-harness/issues/49), composition
  attribution, can operate under today's forced-skill mode and therefore does
  not strictly depend on #48. Landing #48 first is preferred because the two
  identities intersect. If #49 lands before Slice 2, run-group and preparation
  identity include a bounded composition-arm/component-set digest from the
  start; otherwise it gets a focused identity extension before Slice 5.
  Component names never become span names or an unbounded attribute list.
- [#52 item 4](https://github.com/adewale/skill-eval-harness/issues/52) is now
  represented by #65 and must land before backend-specific OTel hooks. Items 1–3
  shipped in #53; item 5 remains the Slice 4 typed-result prerequisite.
- [#64](https://github.com/adewale/skill-eval-harness/issues/64) records the 38
  diagnostics that remain in the central CLI after #58. It does not block the
  new type-clean facade, but its runner, command, grading, judge, and reporting
  regions should become clean before the corresponding OTel slice edits them.
- [#37](https://github.com/adewale/skill-eval-harness/issues/37) is upstream-
  capability-driven and is not on the OTel critical path. Missing Vibe usage,
  schema enforcement, or native telemetry stays explicitly unavailable; OTel
  must not infer or manufacture it.

## MVP goal

Given one `run-agent` attempt, an operator with an OTLP backend should be able to:

- find the attempt by case, variant, repetition, runner, and model;
- see runner and artifact-write duration and operational failure state;
- follow the same trace into a runner that honors propagated context; and
- correlate the trace back to the committed run directory.

This first slice also covers the `run-claude` and `run-codex` compatibility
commands because they use the native `run-agent` path. Vibe uses that path
directly through `run-agent --agent vibe`; there is no `run-vibe` command.

## What similar implementations teach us

| Implementation | Useful pattern | Decision here |
|---|---|---|
| [Eve](https://github.com/vercel/eve/blob/main/docs/guides/instrumentation.md) | The framework owns a stable turn parent while AI SDK instrumentation supplies model/tool children. Exporter setup is user-owned, and framework telemetry failures are best-effort. | Own only the evaluation-attempt boundary, propagate context to the runner, and never let export failure change the run. |
| [Flue](https://github.com/withastro/flue/tree/b814b82b2ce45dc941c77bb010140070e1bd48d5/packages/opentelemetry) | Backend-neutral lifecycle events feed an explicit span state machine while an interceptor owns live async context. Durable W3C propagation, privacy projection, backend adapters, metrics, and logs are separate layers. | Separate the roadmap by those concerns; use composite attempt identity and owner cleanup, persist validated context for later work, and never derive live spans by replaying saved events. |
| [Pydantic AI](https://pydantic.dev/docs/ai/api/models/instrumented/) | Instrumentation accepts a caller-provided provider, versions its emitted schema, distinguishes control flow from errors, and avoids counting usage on both parent and child spans. | Respect an existing global provider, stamp an instrumentation schema version, reserve `Error` for operational failure, and do not copy token usage onto the attempt span. |
| [OpenInference](https://arize-ai.github.io/openinference/spec/configuration.html) | One central trace configuration controls sensitive inputs, outputs, messages, tool definitions, and large payloads independently of the backend. | Use an attribute allowlist and record no prompt, answer, tool arguments, environment values, or file contents in the first slice. |
| [OpenTelemetry](https://opentelemetry.io/docs/specs/otel/library-guidelines/) | Instrumented libraries depend on the API; the application chooses the SDK/exporter; no SDK means a low-overhead no-op. | Put the API in core and SDK plus OTLP exporter in an optional `otel` extra. |
| [OTel environment carriers](https://opentelemetry.io/docs/specs/otel/context/env-carriers/) and [error guidance](https://opentelemetry.io/docs/specs/semconv/general/recording-errors/) | A copied child environment is the CLI propagation boundary. Successful operations leave status unset; failed operations use `Error` and `error.type`. | Inject `TRACEPARENT`/`TRACESTATE` only into each child environment and keep evaluation failure separate from instrumentation failure. |

The [GenAI semantic conventions](https://github.com/open-telemetry/semantic-conventions-genai/blob/434c91dcc34ed038e3048c07720ddfed2c6bddfc/docs/gen-ai/gen-ai-agent-spans.md)
are still evolving. The implementation should pin its instrumentation schema and
use standard `gen_ai.*` attributes only where the harness knows their exact
meaning; harness identity stays under `skill.eval.*`.

### Flue branch-history audit

The 2026-07-31 audit inspected every public branch tree. There were no open Flue
PRs, and [`main` at `b814b82b`](https://github.com/withastro/flue/commit/b814b82b2ce45dc941c77bb010140070e1bd48d5)
is the latest design, so it remains the implementation cited below. It is not the
only surviving OTel tree: [`redesign-02` at `2d387a91`](https://github.com/withastro/flue/tree/2d387a91bfd46b303158e46698fa7e8ac94771d3/packages/opentelemetry),
[`wip/event-stream` at `d112d0b4`](https://github.com/withastro/flue/tree/d112d0b4609885be1a44f17975f47a1cbe594617/packages/opentelemetry),
`v0.10`, `feat/invoke-console`, and several durable-agent branches preserve older,
materially different iterations. Those branches are useful design history, not
evidence of competing work currently proposed for merge.

The transferable parts are:

- **Observe authoritative lifecycle; activate context around real work.** Flue's
  [event-to-span state machine](https://github.com/withastro/flue/blob/b814b82b2ce45dc941c77bb010140070e1bd48d5/packages/opentelemetry/src/index.ts#L76-L538)
  ends spans from terminal lifecycle events while its interceptor makes the
  matching span current around the asynchronous operation. The harness should
  wrap the live subprocess/agent/artifact owners and use their typed outcomes to
  finish spans, not infer duration and parentage later from `events.json`.
- **Key concurrency by full execution identity.** Flue uses separate composite
  [operation, turn, task, tool, and compaction keys](https://github.com/withastro/flue/blob/b814b82b2ce45dc941c77bb010140070e1bd48d5/packages/opentelemetry/src/index.ts#L563-L859),
  suppresses duplicate starts, and sweeps stranded descendants when the owner
  ends. Harness spans need the same discipline for concurrent repetitions,
  multi-turn subagents, cancellations, and timeouts.
- **Model causal order, not the prettiest tree.** Flue keeps provider inference
  and later local tool execution as siblings under the agent operation rather
  than making a tool the child of a completed chat call. Harness-owned subagent
  spans should follow the same rule and leave runner-native hierarchies intact.
- **Treat durable propagation as its own contract.** Flue validates W3C context
  at [HTTP admission](https://github.com/withastro/flue/blob/b814b82b2ce45dc941c77bb010140070e1bd48d5/packages/runtime/src/runtime/handle-agent.ts#L91-L115),
  [persists it with the submission](https://github.com/withastro/flue/blob/b814b82b2ce45dc941c77bb010140070e1bd48d5/packages/runtime/src/runtime/agent-submissions.ts#L38-L58),
  and [reactivates it when execution begins](https://github.com/withastro/flue/blob/b814b82b2ce45dc941c77bb010140070e1bd48d5/packages/runtime/src/runtime/agent-submissions.ts#L718-L733).
  The harness should validate context written to metadata and degrade explicitly
  when Jetty or another remote runner cannot propagate it. Baggage stays out.
- **Centralize schema and privacy before adding backends.** Flue's shared runtime
  owns the [content policy](https://github.com/withastro/flue/blob/b814b82b2ce45dc941c77bb010140070e1bd48d5/packages/runtime/src/telemetry/content.ts#L1-L104),
  [GenAI projection](https://github.com/withastro/flue/blob/b814b82b2ce45dc941c77bb010140070e1bd48d5/packages/runtime/src/telemetry/projection.ts#L1-L105),
  [revision constants](https://github.com/withastro/flue/blob/b814b82b2ce45dc941c77bb010140070e1bd48d5/packages/runtime/src/telemetry/semconv.ts#L1-L42),
  and structural truncation. Its OTel and Cloudflare adapters consume the same
  mapping. The harness needs one content-free projection rather than redaction
  logic in every runner.
- **Keep provider lifecycle and duplicate instrumentation explicit.** Flue accepts
  [injected or global signal providers](https://github.com/withastro/flue/blob/b814b82b2ce45dc941c77bb010140070e1bd48d5/packages/opentelemetry/src/index.ts#L52-L93),
  owns only registrations it created, and removes overlapping Sentry provider
  instrumentation to avoid duplicate model spans. The harness should likewise
  flush/shut down only its provider and let runner-native spans remain the model
  and tool leaves.

Three cautions matter. Flue currently enables richer content than an eval harness
should, so its capture defaults are not copied. Its OTel and Cloudflare adapters
also classify cancellation differently, so the harness needs one explicit
timeout/cancellation/error taxonomy before backend parity. Finally, public side
branches such as
[`redesign-02`](https://github.com/withastro/flue/tree/2d387a91bfd46b303158e46698fa7e8ac94771d3/packages/opentelemetry/test)
and [`wip/event-stream`](https://github.com/withastro/flue/tree/d112d0b4609885be1a44f17975f47a1cbe594617/packages/opentelemetry/test)
preserve OTel tests, while the older
[in-memory-exporter suite](https://github.com/withastro/flue/blob/09b78e844d03376167c8f88ee0ee83556ca8b09a/packages/opentelemetry/test/index.test.ts#L103-L832)
remains the broadest visible catalogue of hierarchy, privacy, concurrency,
propagation, cleanup, and signal-failure cases. They are historical test designs,
not proof that current `main` CI runs the same suite.

## Slices 0–1: implementation-ready MVP

### Slice 0: optional foundation

- Add `opentelemetry-api` as a runtime dependency.
- Add an `otel` extra containing `opentelemetry-sdk` and the OTLP exporter.
- Add a small `observability.py` facade. With no SDK or with support disabled it
  returns valid no-op spans. Programmatic callers can supply a tracer provider;
  otherwise the facade uses the global provider.
- Keep `observability.py` in #60's blocking `ty` include from its first commit
  and add it to the packaged `py-modules` list. Its public seam consists of
  closed, immutable config/correlation/outcome values and narrow protocols or
  context managers—not `dict[str, Any]` attribute bags or raw SDK objects.
- Install the `otel` extra in the CI job that exercises the in-memory SDK while
  keeping the normal package usable with API-only no-ops. Optional-SDK imports
  must type-check without broad missing-import or `Any` suppressions.
- Put the attribute allowlist, sanitization, instrumentation schema revision, and
  pinned upstream semantic-convention revision behind that one facade. Adapters
  provide typed values; they do not write arbitrary span attributes.
- Enable CLI export only with `SKILL_EVAL_OTEL_ENABLED=1`. Reuse standard
  `OTEL_SERVICE_NAME`, `OTEL_RESOURCE_ATTRIBUTES`, and
  `OTEL_EXPORTER_OTLP_*` configuration rather than adding exporter-specific flags.
- Honor standard `OTEL_TRACES_SAMPLER` and `OTEL_TRACES_SAMPLER_ARG` configuration
  from the first exported span. Harness-owned carriers use W3C Trace Context
  without baggage even if an application has configured a broader global
  propagator; propagation policy is part of the privacy facade, not adapter code.
- Set the default resource `service.name` to `skill-eval-harness` when the user has
  not supplied one.

### Slice 1: one trace per answer attempt

Start `skill.eval.run` in `run_agent_tasks` after the prepared row has been
validated and before workspace/invocation work begins. End it only after
`write_runner_outcome` has committed the artifact set.

Use stable, content-free attributes:

- `skill.eval.instrumentation.version`
- `skill.eval.case.id`
- `skill.eval.variant`
- `skill.eval.repetition`
- `skill.eval.runner.name`
- `skill.eval.model`
- `skill.eval.execution.valid`
- `skill.eval.outcome`

Span names must not contain case IDs, model names, paths, or other unbounded
values. Do not duplicate tokens or cost onto the parent: the existing artifacts
and any runner-native child spans own those values.

Create only two harness child spans:

1. `skill.eval.runner.invoke` around the shared subprocess call.
2. `skill.eval.artifacts.write` around the atomic artifact commit.

This keeps the trace useful without mirroring every Python function.

#### Propagate and correlate

In `invoke_argv_with_timeout`, copy the task environment, inject the active W3C
context into that copy, and pass it to the subprocess. Never mutate the parent
process environment, and do not propagate baggage in this slice.

Capture the attempt root's `SpanContext` before entering the artifact-write child.
Before the atomic artifact commit, add this content-free correlation block to
`metadata.json` whenever that context is valid. `IsRecording` and the sampled bit
do not control propagation: an unsampled valid context is still persisted with
flags `00`, while a disabled/no-provider invalid context produces no block.

```json
{
  "observability": {
    "otel_traceparent": "00-<32 hex trace ID>-<16 hex span ID>-<2 hex flags>",
    "instrumentation_version": 1
  }
}
```

Absence means unavailable, exactly like other optional telemetry. Grading must
not require this block. Persist `traceparent` only in the MVP: vendor-opaque
`tracestate` is not inherently content-free and must not cross a durable or
external trust boundary by default. A future same-trust-domain policy may retain
filtered, size-bounded, provenance-labelled entries, but external/manual imports
never reactivate arbitrary `tracestate`. Parse a persisted `traceparent` into a
validated context for a link; do not reactivate an ended attempt as the parent of
later grading. Malformed context is unavailable, not partially used.

#### Error and privacy rules

- A timeout, spawn error, nonzero runner exit, malformed runner response, or
  artifact-commit failure sets the owning span to `Error` and records a bounded
  `error.type`.
- A valid execution whose assertions later fail is not an OTel error. It is an
  evaluation result, not an operational failure.
- Record exception type and a sanitized description once. Do not attach stdout,
  stderr, prompts, outputs, argv, environment variables, absolute paths, tool
  arguments, rubrics, or fixture contents.
- OTel setup, span, flush, and export errors produce at most a warning and never
  change process exit code, artifact contents other than the optional correlation
  block, or `execution_valid`.
- Flush only a provider created by the harness. Never shut down a caller-owned
  global provider.

## Post-MVP roadmap

The dependency order is `0 → 1`, `0 → 2`, `1 + 2 → 3`, `1 + 2 → 4`, then
`3 + 4 → 5 → 6`. A slice starts only when its entry condition is true; this
keeps end-to-end coverage from turning into one unreviewable instrumentation
change.

### Slice 2: control plane and durable handoffs

**Entry:** slice 0 has shipped, and #57's prepared-task, `answer-design.json`,
judge-input, experiment, and artifact identities are stable. Observability
metadata is explicitly excluded from task, treatment, answer-design,
judge-input, and eval-contract digests so tracing cannot change experimental
identity.

Cover work that can stop or branch before an answer attempt exists:

- Add one `skill.eval.command` root for every CLI invocation with an enumerated
  `skill.eval.command.name`. Record neither argv nor paths. Later attempt/grade
  roots link to this command; they are not descendants of an unbounded command
  trace.
- Create `skill.eval.run_group.id` at `suite-run` or `prepare`, accept a
  caller-supplied validated ID when orchestration already owns one, and persist
  it in `RUN_SCOPE.json` plus a versioned, content-free preparation sidecar keyed
  by the authoritative `answer-design.json` `design_sha256`. Do not add tracing
  fields to `answer-design.json` or include them in `design_sha256`,
  `eval_contract_sha256`, task, treatment, or instruction digests. Do not
  reactivate an old prepare span as the parent of work performed days later.
- Add bounded operations for manifest validation, readiness audit, pin and
  budget gates, ablation materialization, task preparation, Jetty export, and
  blind-comparison task export. Persist the run group and input/output digests at
  each durable handoff without recording prompts, rubrics, filenames, or skill
  content.
- Give `succeeded`, `blocked`, `partial`, `cancelled`, and `failed` one typed
  operational taxonomy. Expected budget/readiness gates and incomplete
  measurement remain domain outcomes with unset OTel status. Timeout, protocol
  violation, failed validation of an expected artifact, and write failure use
  `Error` plus a bounded `error.type`; operator cancellation is not a failure.
- When `suite-run` starts subprocess commands, inject context only into a copied
  child environment. Child commands start bounded roots and link back to the
  suite operation; a suite trace never owns every later model attempt.

**Exit:** every command can be found by command name and run group when enabled;
a run blocked before generation explains which gate stopped it; prepared and
exported work can be correlated without changing answer-key exposure, task
digests, or output when OTel is disabled.

### Slice 3: execution, remote orchestration, and ingestion parity

**Entry:** slices 0–2 have shipped, #65's unified backend registry is the source
of tracing capability, the answer trace is stable across every registered
answer backend fixture, at least one real child runner has proved W3C parentage,
trigger completeness uses #61's typed cohort contract, and the rebased #47 is
the Jetty source of truth. `import-trace` and `migrate-telemetry` must first have
one atomic artifact-augmentation owner that refreshes or deliberately retires
the existing commit marker.

Add the remaining live execution producers while retaining one root per attempt:

- `run-subagent`: keep the in-process attempt context active across the agent
  call and tool executor. Add turn/tool children only when they represent real
  timed operations and are not already emitted by native instrumentation.
- `skill-trigger-matrix` and Pi: create one trace per query repetition, not one
  trace for the whole matrix. Record trigger population, expected polarity, and
  observed trigger state as bounded attributes; an honest no-trigger result is
  not an OTel error.
- Jetty: trace upload, submit, polling, result download, and the terminal remote
  state under one bounded attempt root. Inject W3C context into the remote
  request only when the live API supports it, and persist the submitted context
  with the local Jetty run record. If the result exposes an independently
  created remote trace, preserve it and use a span link; never replay
  `trace.jsonl` records to counterfeit live parent/child spans.
- Treat `import-jetty-results`, `import-trace`, and `migrate-telemetry` as new
  ingest/migration roots. Validate the complete prospective artifact set, link
  to any valid producer context, commit atomically, and append the ingestion
  correlation without replacing the originating attempt context. Invalid or
  absent producer context is an explicit availability state.
- Accept manual/person-written and arbitrary external answer contracts at the
  same boundary. A valid supplied `traceparent` may be linked; untrusted
  `tracestate` is discarded, and absent/invalid correlation records
  `unavailable`. The harness never invents a remote attempt span.
- Reuse `invoke_argv_with_timeout` and `write_runner_outcome` so subprocess,
  error, privacy, and artifact semantics do not fork by adapter.
- Key active spans by the complete prepared-task identity, suppress duplicate
  starts, and end any still-open owned children on timeout/cancellation/attempt
  completion. Never keep one process-global “current run.”

**Exit:** a registry-driven conformance suite proves the same attempt identity,
error taxonomy, secret allowlist, concurrency isolation, and artifact
correlation for every backend capability that claims tracing support; every
other capability declares an explicit unavailable reason. Unsupported remote
propagation is never fabricated. Owner completion also proves that no descendant
span remains open after interruption. #61 proves the remaining #54 runtime
contract, and the rebased #47 live contract is the source of truth for Jetty
submission, polling, artifact commit, and failure states. Import and migration
failure leaves the previous committed artifact set and its producer correlation
intact.

### Slice 4: grading and judge lifecycle

**Entry:** slices 1–2 correlation metadata is stable; grading can consume old or
external runs whose metadata has no observability block; #58's comparison view
is on `main`; and #52 item 5 has replaced the judge invocation dictionary with a
closed typed result.

Instrument work that may happen minutes or days after generation without making
one misleading long-lived trace:

- Instrument the shared `grade_case_variant` ownership boundary, not only the
  `grade` command. Every logical grading of one discovered run therefore creates
  one bounded `skill.eval.grade` root whether invoked by `grade`, `benchmark`,
  `judge` task reconstruction, `token-overhead`, `export-anthropic`, `aggregate`,
  or a run-aware audit. Link it to the evaluated run context from `metadata.json`
  when present.
- Identify a re-grade by a content-free digest over the committed run identity,
  authoritative answer design, manifest/assertion contract, strict/script/embed
  policy, and validated `judge_input_sha256`/judge-result set. Reuse those #57
  identities rather than defining an observability-only digest. The same run
  graded under a changed manifest is a new evaluation, not the old grade trace
  reused under a misleading ID.
- When `--allow-scripts` or `--embed-cmd` actually invokes an external oracle,
  create a bounded `skill.eval.grade.external.invoke` child with an enumerated
  `script_assertion` or `embedding` kind. Record no command, path, assertion
  label, input, output, or embedding vector. Operational subprocess failure is
  an OTel error; an oracle's pass/fail/score remains evaluation data.
- Trace judge-task emission and optional `judge-tasks.jsonl` commit separately
  from invocation. `judge` reconstructing tasks from manifest+runs must produce
  the same task identity as `grade --judge-tasks`.
- Create `skill.eval.judge.invoke` only when an LLM or external judge command is
  actually called. Repeats and panel members are distinct bounded invocations
  linked to the same judge task. Native model telemetry may appear beneath them;
  fail-closed paths that intentionally avoid model spend emit no invocation span.
- Validate and commit judge results under a bounded result-ingest operation.
  External/human result files carry validated correlation or explicit absence.
  `judge-robustness` reuses the same invocation and privacy owner rather than
  opening an unrelated model-call path.
- Represent pass/fail/score as evaluation results, not span status. Adopt the
  version-pinned [`gen_ai.evaluation.result`](https://github.com/open-telemetry/semantic-conventions-genai/blob/434c91dcc34ed038e3048c07720ddfed2c6bddfc/docs/gen-ai/gen-ai-events.md#event-gen_aievaluationresult)
  event when the Python event API can carry the evaluated context reliably;
  until then, keep the result in artifacts instead of inventing a lookalike.
- Do not record judge prompts, rubrics, explanations, candidate output, or
  sanitized-workspace contents. Scores and low-cardinality labels are sufficient
  for correlation; artifacts retain the reviewable evidence.

**Exit:** emitted evaluation results match committed `grading.json`, grade
reports, and judge rows; direct grading through every consumer produces the same
identity and semantics; missing or unsampled run context degrades cleanly; judge
cost is not duplicated; and tests prove held-out rubrics and candidate content
never reach telemetry.

### Slice 5: aggregation, analysis, and publication

**Entry:** slices 3–4 have stable execution, ingestion, grade, and judge
identities, and slice 2 run groups survive every tested process/file handoff.

Cover every consumer of committed run, judge, trigger, and benchmark artifacts
without creating a giant all-or-nothing trace:

- Add bounded aggregate roots for `benchmark`, `aggregate`, `trigger-compare`,
  `cost-summary`, and `token-overhead`. Before root creation, select at most 32
  contributing grade, trigger, or run contexts by one documented deterministic
  rule and provide those links at span creation. Record `input.count`,
  `linked.count`, and `links.omitted.count`; keep the complete input-digest
  inventory in the output artifact/sidecar. They never attach arrays of case IDs,
  grow an O(N) link list, or make thousands of grade traces their children.
- Give `compare-results`, `compare-judges`, `judge-alignment`, `contamination`,
  `error-analysis`, and `trend` bounded analysis/import roots. A finding,
  regression, contamination hit, or poor agreement is domain data, not an OTel
  error. Malformed input or failed output commit is an operational error.
- Add publish/commit operations for `report`, static `render-viewer`, and
  `export-anthropic`, carrying the run group and producer context into the
  output's content-free observability block or a versioned adjacent sidecar when
  the destination schema cannot accept additive metadata. When
  `render-viewer --serve` is used, trace the bounded render/startup and each
  feedback commit, not one trace for the full server lifetime.
- Keep `compare-tasks` export linked to `compare-results` import through an
  export digest and run group; never put candidate text or the truth map in
  telemetry.
- `suggest-cases` stays a model-free analysis command unless `--generate-cmd`
  is present. That optional call uses the shared invocation/error/privacy
  contract and a separate bounded model-operation span.
- Add variant, model, repetition, ablation, and judge-panel identities only as
  documented attributes with a cardinality budget. Raw prompts, filesystem
  paths, and free-form assertion names remain artifacts, not index fields.
- Treat retries as new attempts with their own span IDs while retaining the same
  experimental identity; never overwrite trace correlation without the atomic
  artifact commit.

**Exit:** an operator can navigate from one run group to every command,
execution, import, evaluation, aggregate, and published artifact; paired
variants and populations remain distinguishable; partial aggregates cannot look
complete; and a large fake matrix proves that no trace grows with total
experiment size.

### Slice 6: operational signals and deployment guidance

**Entry:** slices 2–5 have produced enough real traces to choose metrics from
observed operational questions rather than speculation.

- Add low-cardinality attempt counts and duration/error histograms for harness
  operations. The metric allowlist is separate from trace attributes and
  explicitly excludes case, run-group, repetition, task, model, panel, and
  digest identities. Do not re-export provider token/cost values already
  represented by native spans or canonical artifacts.
- Correlate existing structured warnings/errors with active trace and span IDs
  before considering an OTel log exporter. Routine evaluation failures remain
  data, not error logs.
- Require every native/exporter integration to consume the shared schema/privacy
  projection and pass the same conformance fixtures, even when its span-opening
  mechanics differ. Disable overlapping provider auto-instrumentation rather
  than emitting two model spans for one call.
- Document Collector-based OTLP deployment, batching, resource attributes, and
  exporter-health checks. Slice 0 already owns sampler configuration and
  propagation tests; operators retain sampling and retention policy.
- Publish vendor-neutral query recipes for runner latency, operational error
  rate, missing propagation, and export failures. Vendor-specific dashboards can
  live outside core.

**Exit:** a cardinality review passes, metrics cannot double-count runner-native
usage, exporter failure remains non-fatal, and an unavailable Collector leaves
the same command result and artifact set.

## Permanent non-goals

- Reconstructing “live” spans from historical `trace.jsonl` or `events.json`.
  Historical artifacts may be analyzed or imported as data, but replay would
  invent timing, context, and sampling decisions that did not occur.
- Using spans, events, metrics, or export success as grading evidence or
  replacing any run artifact.
- Shipping vendor SDKs, hosted backends, dashboards, retention policy, or
  delivery guarantees in the core package.
- Default prompt, output, tool-argument, rubric, fixture, environment, baggage,
  or file-content capture. A future content profile would require a separate
  threat model and explicit opt-in; it is not implicitly part of slice 6.

## Slices 0–1 acceptance tests

Use the SDK's in-memory exporter; no live collector or model call belongs in CI.

1. Disabled support preserves current artifacts and produces no finished spans.
2. A successful fake native run produces the exact three-span tree and leaves
   span status unset.
3. The runner child receives an isolated `TRACEPARENT` whose trace ID matches the
   attempt-root context committed as `otel_traceparent`; a sibling task receives
   a different parent span, and the artifact-write child ID is never persisted as
   the attempt identity.
4. Timeout and nonzero exit set `Error` plus the expected bounded `error.type`;
   an assertion failure does not.
5. Span attributes pass an explicit allowlist test, including a sentinel secret
   placed in prompt, output, stderr, environment, and paths.
6. A throwing exporter leaves exit code, `execution_valid`, and the pre-existing
   artifact contract unchanged.
7. Concurrent attempts have distinct root spans and no cross-task parentage.
8. `ty check` passes with `observability.py` in the project-owned include and no
   OTel-specific broad ignore; an optional-SDK-disabled run follows the same
   typed facade as an exporting run.
9. `always_off` sampling exports no spans but propagates and persists a valid
   flags-`00` attempt context; sampled roots export normally; an unsampled remote
   parent and an independently sampled linked root follow documented policy.
10. Baggage and `tracestate` sentinels reach neither durable artifacts nor an
    external/manual reactivation boundary; malformed context becomes unavailable.

## End-to-end acceptance gates

Each later slice adds offline conformance tests; no live backend is required in
default CI.

1. A suite blocked by manifest, pin, readiness, or budget policy produces a
   completed command trace with `outcome=blocked`, no attempt traces, no content
   attributes, and the same exit/artifact behavior with OTel disabled.
2. `prepare` and a later `run-agent` in separate processes share a run group but
   not a parent span. The preparation sidecar binds the exact
   `answer-design.json` digest, while observability metadata changes none of the
   answer-design, task, treatment, instruction, or eval-contract digests.
   Missing, stale, duplicated, or extra design rows remain `partial` and cannot
   publish aggregate headlines merely because tracing succeeded.
3. Every capability in #65's backend registry either passes one conformance
   matrix for identity, error taxonomy, W3C availability, secret filtering,
   cancellation cleanup, and artifact correlation or declares tracing
   unavailable with a bounded reason. A new backend value fails this gate until
   one of those states is explicit.
4. Jetty submit/poll, remote trace linking, result import, `import-trace`, and
   `migrate-telemetry` each preserve the previous committed artifact set under
   injected validation/write failure; successful augmentation commits a new
   correlation entry without overwriting the producer entry.
5. Grading one run through `grade`, `judge` reconstruction, `benchmark`,
   `token-overhead`, `export-anthropic`, and `aggregate` produces the same grade
   identity and evaluation semantics. Changing answer design, assertion,
   `judge_input_sha256`, or judge-result digest produces a distinct re-grade
   identity. Opt-in script/embed subprocesses use content-free child spans.
6. `grade --judge-tasks` and `judge` reconstruction yield the same task IDs;
   repeats/panels remain distinct invocations; external and human verdicts merge
   with valid correlation or explicit absence; prompts and rubrics never appear
   in exported telemetry.
7. A partial benchmark, incomplete trigger comparison, failed contamination
   gate, and poor judge-alignment result remain domain outcomes rather than OTel
   errors. Malformed inputs and failed artifact commits are errors.
8. A large synthetic multi-model, multi-repeat suite proves constant trace size:
   each aggregate has at most 32 deterministic links, records total/linked/omitted
   counts, retains the full input digest inventory and run group in artifacts,
   and carries no unbounded identity or content list.
9. Metrics expose only their signal-specific low-cardinality allowlist, and log
   correlation sentinel tests prove no eval content reaches structured messages.
10. The executable documentation guard derives parser subcommands and installed
    console scripts, requires each exactly once inside the delimited coverage
    inventory, and rejects phantom or duplicate command assignments.

## Delivery units

Each numbered slice is a separate review and release decision. Slices 0–1 may
share one focused implementation PR if it stays reviewable:

1. on the merged #57/#58 foundation, add the optional dependency, typed facade,
   packaging, sampler policy, and no-op tests;
2. instrument the shared native answer path and subprocess propagation;
3. persist trace correlation and add the acceptance tests; and
4. document one local OTLP example plus the privacy/default behavior.

If that PR cannot stay reviewable, split after slice 0. Slices 2–6 must remain
separate follow-up PRs; passing an earlier exit gate is what authorizes the next
surface, not a desire to make the first PR appear comprehensive.
