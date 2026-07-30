# Minimal OpenTelemetry support roadmap

Status: proposed, researched 2026-07-30.

## Decision

Build OpenTelemetry support as independently reviewable slices. Slices 0–1 are
the implementation-ready MVP: opt-in traces for native answer-runner attempts.
Slices 2–5 extend that contract to the other execution surfaces, later grading,
experiment correlation, and operational signals only after the preceding slice
has proved its semantics.

One answer attempt initially becomes one bounded trace that can join
runner-native spans when the child process understands W3C Trace Context:

```text
skill.eval.run
├── skill.eval.runner.invoke
│   └── runner-native model/tool spans, when available
└── skill.eval.artifacts.write
```

This document covers the whole roadmap; “not in the first slice” does not mean
“not planned.” The [post-MVP slices](#post-mvp-roadmap) below give every deferred
surface an owner and an entry gate. Items that should never enter the harness
core are listed separately as [permanent non-goals](#permanent-non-goals).

The harness's saved artifacts remain the grading and debugging source of truth.
The existing `otel` blocks in `events.json` and `metrics.json` are normalized
offline evidence; this plan does not replay them as live spans or make successful
export part of evaluation validity.

## Repository prerequisites and landing order

This order follows an audit of all 102 commits reachable from `main`, the 117
additional commits on remote branch tips, and every open PR and issue as of
2026-07-30. The history repeatedly succeeds by stabilizing a typed owner before
instrumenting its callers; large cross-cutting changes are then rebased once,
after their prerequisites, rather than repeatedly merged around one another.

Land the current work in this order:

1. [#60](https://github.com/adewale/skill-eval-harness/pull/60), the focused
   `ty` gate. Its selected modules pass on the current #47, #57, and #58 heads,
   and it reserves `observability.py` and `text_contracts.py` as zero-debt typed
   boundaries.
2. [#59](https://github.com/adewale/skill-eval-harness/pull/59), this plan. It is
   documentation-only and merge-clean with every current branch, so it can set
   policy before runtime instrumentation begins.
3. [#57](https://github.com/adewale/skill-eval-harness/pull/57), the fail-closed
   evidence construction change. It rewrites the shared invocation, artifact,
   trigger, judge, and telemetry seams that later spans must wrap.
4. Rebase and land [#58](https://github.com/adewale/skill-eval-harness/pull/58)
   on #57, closing [#55](https://github.com/adewale/skill-eval-harness/issues/55).
   The branches currently conflict in the grading owner and six documentation
   files; resolving once in this direction makes the narrower Unicode contract
   adapt to the new fail-closed construction boundary.
5. Resolve [#54](https://github.com/adewale/skill-eval-harness/issues/54) in a
   small PR on the post-#57 matrix. Preserve #57's completed-observation
   denominators, add the omitted incomplete count to terminal output, and return
   nonzero when the run cannot support the requested measurement. This must land
   before trigger spans could make an operationally incomplete matrix look valid.
6. Rebase and land [#47](https://github.com/adewale/skill-eval-harness/pull/47)
   last among the current runtime branches. It is based before #50–#53 and
   conflicts with #57/#58 in `skill_benchmark.py` and shared docs; one final
   rebase preserves its live Jetty evidence without repeatedly resolving the
   same ownership changes.

Slices 0–1 may begin after steps 1, 3, and 4. Slice 2 additionally requires
steps 5–6. Slice 3 additionally requires the typed judge-invocation result from
[#52 item 5](https://github.com/adewale/skill-eval-harness/issues/52); adding
span lifecycle to the current untyped backend/parse/merge dictionary would make
that boundary harder to close later.

The remaining issues are sequenced by when they change telemetry identity:

- [#48](https://github.com/adewale/skill-eval-harness/issues/48), native skill
  discovery, does not block slices 0–1. If it lands before Slice 2, the slice must
  include its typed activation mode and invocation-evidence availability; if it
  lands later, that parity extension is a separate PR with the same conformance
  gate.
- [#49](https://github.com/adewale/skill-eval-harness/issues/49), composition
  attribution, follows #48 and precedes Slice 4. Run-group identity must carry a
  bounded composition-arm/component-set digest, not component names as span
  names or an unbounded attribute list.
- [#52 item 4](https://github.com/adewale/skill-eval-harness/issues/52) becomes
  mandatory before OTel would otherwise add parallel hooks to the answer,
  trigger, and judge registries; avoid creating a fourth registry-spanning rule.
  Items 1–3 already shipped in #53 and should be checked off independently.
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

This first slice also covers the `run-claude`, `run-codex`, and `run-vibe`
compatibility commands because they use the native `run-agent` path.

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

### Flue active-branch audit

The 2026-07-30 audit inspected all remote branches. Only
[`main` at `b814b82b`](https://github.com/withastro/flue/commit/b814b82b2ce45dc941c77bb010140070e1bd48d5)
and [`snapshot` at `4b29e770`](https://github.com/withastro/flue/commit/4b29e770929e6ed919f430d2ef0e34e4b8780121)
contain `packages/opentelemetry`; their OTel, runtime, and docs trees are identical
apart from package version. Recent `redesign-02` and June WIP branches predate the
implementation, and there were no open PRs. `main` is therefore the cited design,
not one choice among competing active implementations.

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
timeout/cancellation/error taxonomy before backend parity. Finally, the active
nightly branches do not contain the OTel test tree; the last visible
[historical in-memory-exporter suite](https://github.com/withastro/flue/blob/09b78e844d03376167c8f88ee0ee83556ca8b09a/packages/opentelemetry/test/index.test.ts#L103-L832)
is a strong catalogue of hierarchy, privacy, concurrency, propagation, cleanup,
and signal-failure cases, but not evidence that current public-branch CI runs them.

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

Before the atomic artifact commit, add this content-free correlation block to
`metadata.json` when the span is recording:

```json
{
  "observability": {
    "otel_traceparent": "00-<32 hex trace ID>-<16 hex span ID>-<2 hex flags>",
    "instrumentation_version": 1
  }
}
```

Absence means unavailable, exactly like other optional telemetry. Grading must
not require this block. Add `otel_tracestate` only when the propagator produced a
validated non-empty value. The W3C carrier leaves a valid correlation token for
later, separately executed grading without putting prompt or result content into
metadata. Validate every field before persistence and again before reactivation;
malformed context is unavailable, not partially used.

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

The dependency order is `0 → 1 → 2`, `1 → 3`, then `2 + 3 → 4 → 5`. A slice
starts only when its entry condition is true; this keeps “support everything”
from turning into one unreviewable instrumentation change.

### Slice 2: execution-surface parity

**Entry:** slices 0–1 have shipped, the answer trace is stable across concurrent
Claude, Codex, and Vibe fixture runs, and at least one real child runner has
proved W3C parentage.

Add the remaining live execution producers while retaining one root per attempt:

- `run-subagent`: keep the in-process attempt context active across the agent
  call and tool executor. Add turn/tool children only when they represent real
  timed operations and are not already emitted by native instrumentation.
- `skill-trigger-matrix` and Pi: create one trace per query repetition, not one
  trace for the whole matrix. Record trigger population, expected polarity, and
  observed trigger state as bounded attributes; an honest no-trigger result is
  not an OTel error.
- Jetty: inject W3C context into the remote request when the API supports it. If
  the imported result exposes an independently created remote trace, preserve
  it and use a span link; never replay `trace.jsonl` records to counterfeit live
  parent/child spans.
- Reuse `invoke_argv_with_timeout` and `write_runner_outcome` so subprocess,
  error, privacy, and artifact semantics do not fork by adapter.
- Key active spans by the complete prepared-task identity, suppress duplicate
  starts, and end any still-open owned children on timeout/cancellation/attempt
  completion. Never keep one process-global “current run.”

**Exit:** a shared conformance suite proves the same attempt identity, error
taxonomy, secret allowlist, concurrency isolation, and artifact correlation for
every runner that claims tracing support. Unsupported remote propagation is
reported as unavailable, never silently fabricated. Owner completion also proves
that no descendant span remains open after interruption. #54 is closed and the
rebased #47 live contract is the source of truth for Jetty submission, polling,
artifact commit, and failure states.

### Slice 3: grading and judge correlation

**Entry:** slice 1 correlation metadata is stable and grading can consume old
runs whose metadata has no observability block. #58 has fixed the comparison
view, and #52 item 5 has replaced the judge invocation dictionary with a closed
typed result.

Instrument work that may happen minutes or days after generation without making
one misleading long-lived trace:

- Create a bounded `skill.eval.grade` trace for deterministic grading of one run
  and link it to the evaluated run context from `metadata.json` when present.
- Add a `skill.eval.judge.invoke` child only when an LLM judge is actually
  called. Native model telemetry may appear beneath it; fail-closed paths that
  intentionally avoid model spend emit no judge-invocation span.
- Represent pass/fail/score as evaluation results, not span status. Adopt the
  version-pinned [`gen_ai.evaluation.result`](https://github.com/open-telemetry/semantic-conventions-genai/blob/434c91dcc34ed038e3048c07720ddfed2c6bddfc/docs/gen-ai/gen-ai-events.md#event-gen_aievaluationresult)
  event when the Python event API can carry the evaluated context reliably;
  until then, keep the result in artifacts instead of inventing a lookalike.
- Do not record judge prompts, rubrics, explanations, candidate output, or
  sanitized-workspace contents. Scores and low-cardinality labels are sufficient
  for correlation; artifacts retain the reviewable evidence.

**Exit:** emitted evaluation results match committed `grading.json`/judge rows,
missing or unsampled run context degrades cleanly, judge cost is not duplicated,
and tests prove held-out rubrics and candidate content never reach telemetry.

### Slice 4: experiment and suite correlation

**Entry:** execution and grading traces have stable per-attempt identities.

Add cross-attempt navigation without creating a giant all-or-nothing trace:

- Persist a content-free `skill.eval.run_group.id` for a prepare/run/grade/report
  cycle and attach it to attempt, trigger, grade, and judge spans.
- For queued or resumed work, persist only validated `traceparent`/`tracestate`,
  reactivate it at the actual execution boundary, and make an explicit link/new
  root when the original operation is no longer the causal parent.
- Keep each attempt and grade as its own trace so sampling, retries, concurrency,
  and backend limits remain bounded. Suite/command spans describe orchestration
  and use links or the run-group ID rather than becoming parents of thousands of
  spans.
- Add variant, model, repetition, ablation, and judge-panel identities only as
  documented attributes with a cardinality budget. Raw prompts, filesystem
  paths, and free-form assertion names remain artifacts, not index fields.
- Treat retries as new attempts with their own span IDs while retaining the same
  experimental identity; never overwrite trace correlation without the atomic
  artifact commit.

**Exit:** an operator can find every execution and evaluation belonging to one
run group, paired variants remain distinguishable, and a large fake matrix
proves that no trace grows with total experiment size.

### Slice 5: operational signals and deployment guidance

**Entry:** slices 2–4 have produced enough real traces to choose metrics from
observed operational questions rather than speculation.

- Add low-cardinality attempt counts and duration/error histograms for harness
  operations. Do not re-export provider token/cost values already represented by
  native spans or canonical artifacts.
- Correlate existing structured warnings/errors with active trace and span IDs
  before considering an OTel log exporter. Routine evaluation failures remain
  data, not error logs.
- Require every native/exporter integration to consume the shared schema/privacy
  projection and pass the same conformance fixtures, even when its span-opening
  mechanics differ. Disable overlapping provider auto-instrumentation rather
  than emitting two model spans for one call.
- Document Collector-based OTLP deployment, batching, resource attributes,
  sampling, and exporter-health checks. The harness supplies safe defaults and
  examples; operators retain sampling and retention policy.
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
  threat model and explicit opt-in; it is not implicitly part of slice 5.

## Slices 0–1 acceptance tests

Use the SDK's in-memory exporter; no live collector or model call belongs in CI.

1. Disabled support preserves current artifacts and produces no finished spans.
2. A successful fake native run produces the exact three-span tree and leaves
   span status unset.
3. The runner child receives an isolated `TRACEPARENT` whose trace ID matches the
   committed `otel_traceparent`; a sibling task receives a different parent span.
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

## Delivery units

Each numbered slice is a separate review and release decision. Slices 0–1 may
share one focused implementation PR if it stays reviewable:

1. after #60 and #57 land, add the optional dependency, typed facade, packaging,
   and no-op tests;
2. instrument the shared native answer path and subprocess propagation;
3. persist trace correlation and add the acceptance tests; and
4. document one local OTLP example plus the privacy/default behavior.

If that PR cannot stay reviewable, split after slice 0. Slices 2–5 must remain
separate follow-up PRs; passing an earlier exit gate is what authorizes the next
surface, not a desire to make the first PR appear comprehensive.
