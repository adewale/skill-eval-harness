# Minimal OpenTelemetry support plan

Status: proposed, researched 2026-07-30.

## Decision

Add opt-in, trace-only OpenTelemetry support for native answer-runner attempts. One
attempt becomes one bounded trace that can join runner-native spans when the child
process understands W3C Trace Context:

```text
skill.eval.run
├── skill.eval.runner.invoke
│   └── runner-native model/tool spans, when available
└── skill.eval.artifacts.write
```

The harness's saved artifacts remain the grading and debugging source of truth.
The existing `otel` blocks in `events.json` and `metrics.json` are normalized
offline evidence; this plan does not replay them as live spans or make successful
export part of evaluation validity.

## Goal

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
| [Pydantic AI](https://pydantic.dev/docs/ai/api/models/instrumented/) | Instrumentation accepts a caller-provided provider, versions its emitted schema, distinguishes control flow from errors, and avoids counting usage on both parent and child spans. | Respect an existing global provider, stamp an instrumentation schema version, reserve `Error` for operational failure, and do not copy token usage onto the attempt span. |
| [OpenInference](https://arize-ai.github.io/openinference/spec/configuration.html) | One central trace configuration controls sensitive inputs, outputs, messages, tool definitions, and large payloads independently of the backend. | Use an attribute allowlist and record no prompt, answer, tool arguments, environment values, or file contents in the first slice. |
| [OpenTelemetry](https://opentelemetry.io/docs/specs/otel/library-guidelines/) | Instrumented libraries depend on the API; the application chooses the SDK/exporter; no SDK means a low-overhead no-op. | Put the API in core and SDK plus OTLP exporter in an optional `otel` extra. |
| [OTel environment carriers](https://opentelemetry.io/docs/specs/otel/context/env-carriers/) and [error guidance](https://opentelemetry.io/docs/specs/semconv/general/recording-errors/) | A copied child environment is the CLI propagation boundary. Successful operations leave status unset; failed operations use `Error` and `error.type`. | Inject `TRACEPARENT`/`TRACESTATE` only into each child environment and keep evaluation failure separate from instrumentation failure. |

The [GenAI semantic conventions](https://github.com/open-telemetry/semantic-conventions-genai/blob/434c91dcc34ed038e3048c07720ddfed2c6bddfc/docs/gen-ai/gen-ai-agent-spans.md)
are still evolving. The implementation should pin its instrumentation schema and
use standard `gen_ai.*` attributes only where the harness knows their exact
meaning; harness identity stays under `skill.eval.*`.

## Minimal implementation

### 1. Optional foundation

- Add `opentelemetry-api` as a runtime dependency.
- Add an `otel` extra containing `opentelemetry-sdk` and the OTLP exporter.
- Add a small `observability.py` facade. With no SDK or with support disabled it
  returns valid no-op spans. Programmatic callers can supply a tracer provider;
  otherwise the facade uses the global provider.
- Enable CLI export only with `SKILL_EVAL_OTEL_ENABLED=1`. Reuse standard
  `OTEL_SERVICE_NAME`, `OTEL_RESOURCE_ATTRIBUTES`, and
  `OTEL_EXPORTER_OTLP_*` configuration rather than adding exporter-specific flags.
- Set the default resource `service.name` to `skill-eval-harness` when the user has
  not supplied one.

### 2. One trace per answer attempt

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

### 3. Propagate and correlate

In `invoke_argv_with_timeout`, copy the task environment, inject the active W3C
context into that copy, and pass it to the subprocess. Never mutate the parent
process environment, and do not propagate baggage in this slice.

Before the atomic artifact commit, add this content-free correlation block to
`metadata.json` when the span is recording:

```json
{
  "observability": {
    "otel_trace_id": "32 lowercase hex characters",
    "otel_span_id": "16 lowercase hex characters",
    "instrumentation_version": 1
  }
}
```

Absence means unavailable, exactly like other optional telemetry. Grading must
not require this block.

### 4. Error and privacy rules

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

## Explicitly not in the first slice

- OTel metrics or log export.
- Judge, trigger-matrix, Jetty, Pi, or `run-subagent` spans.
- Reconstructing spans from historical `trace.jsonl`/`events.json`.
- Prompt/output/tool-content capture, baggage, or a content opt-in.
- Vendor SDKs, dashboards, sampling policy, Collector deployment, or delivery
  guarantees.
- Using spans as grading evidence or replacing any run artifact.

These are follow-ups only after the answer-runner trace proves useful and stable.

## Acceptance tests

Use the SDK's in-memory exporter; no live collector or model call belongs in CI.

1. Disabled support preserves current artifacts and produces no finished spans.
2. A successful fake native run produces the exact three-span tree and leaves
   span status unset.
3. The runner child receives an isolated `TRACEPARENT` whose trace ID matches the
   committed metadata; a sibling task receives a different parent span.
4. Timeout and nonzero exit set `Error` plus the expected bounded `error.type`;
   an assertion failure does not.
5. Span attributes pass an explicit allowlist test, including a sentinel secret
   placed in prompt, output, stderr, environment, and paths.
6. A throwing exporter leaves exit code, `execution_valid`, and the pre-existing
   artifact contract unchanged.
7. Concurrent attempts have distinct root spans and no cross-task parentage.

## Delivery order

One focused implementation PR should:

1. add the optional dependency/facade and no-op tests;
2. instrument the shared native answer path and subprocess propagation;
3. persist trace correlation and add the acceptance tests; and
4. document one local OTLP example plus the privacy/default behavior.

If that PR cannot stay reviewable, split after step 1; do not widen the span
surface to make either half look more complete.
