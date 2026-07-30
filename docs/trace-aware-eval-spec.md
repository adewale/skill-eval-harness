# Trace-Aware Skill Eval Spec

Status: v0.4.1 implementation note, with later follow-on items now shipped (see below). The shipped slice covers three things: trace artifacts, trace-backed assertions, and runner adapters. Concretely, the harness can import traces, run Codex JSONL tasks, normalize observed Pi/Codex shapes plus documented/mocked Jetty trajectory shapes, write Pi smoke and Pi trigger trace artifacts, scope assertions by variant, and report process/efficiency evidence alongside outcome scores. This plan uses the subset of ideas from OpenAI's `eval-skills` article, SkillsBench, and Anthropic's `skill-creator` that fit the existing harness model.

> **Superseded in part by [`eval-framework-roadmap-spec.md`](eval-framework-roadmap-spec.md) (implemented).** Several items once listed here as follow-on work have since shipped: OpenTelemetry GenAI normalization (`events.json`/`metrics.json` are now `schema_version: 2` with `gen_ai.*` attributes; version-1 files still grade), the in-process subagent runner with record/replay tool I/O, output-side contamination lint, and the viewer's serve mode with `feedback.json` capture. The Phase checkboxes below are reconciled; live Jetty token validation and OpenCode/Gemini adapters remain open.

## Design principle

**Runners execute. Skill Eval Harness decides.**

Pi, Codex, OpenCode, Gemini CLI, Jetty, Claude Code, or custom scripts may execute tasks and emit traces. The harness remains the source of truth for:

- manifests, splits, cases, variants, ablations, and fixtures;
- answer-key-safe task preparation;
- local deterministic grading;
- judge-task IDs and judge-result merge contracts;
- leakage/audit checks;
- benchmark summaries, deltas, and failure flags.

This mirrors the Jetty adapter principle: Jetty or any other runner supplies artifacts and trajectory evidence; the harness normalizes and grades them.

## Trace-aware design scope

The consistent, high-fit subset is:

1. First-class trace artifacts for runner event streams.
2. Normalized process and efficiency metrics derived from traces.
3. Optional process/efficiency assertions that fail closed when required evidence is missing.
4. A Codex `codex exec --json` adapter alongside Pi and Jetty trace imports.
5. Report additions for absolute delta, normalized gain, negative-delta cases, and domain/difficulty slices.
6. Manifest taxonomy fields for trigger, domain, difficulty, and success-goal reporting.
7. Stronger dataset/oracle audits inspired by SkillsBench.
8. Structured judge-result schemas and stable per-check IDs layered on the existing `judge --judge-cmd` path.
9. Optional skill-profile audits for oversized or over-broad skills.

## What is excluded or deferred

The following ideas remain deferred or deliberately out of scope:

- **Self-generated skills as release proof.** They can be a research/control variant later, but release claims should continue to use curated `with_skill` versus `without_skill`/`old_skill`/materialized `ablation:<id>` runs.
- **Mandatory containerization.** Containerized task environments improve rigor for some tasks, but the current repos already use lightweight fixtures and script oracles. Containers can be a runner requirement later, not a baseline harness dependency.
- **Strict leakage by default.** Leakage lint should remain warning-first until legacy keyword checks are remediated.
- **Live API/model calls in unit tests.** Runner adapters need mocked fixtures and opt-in live smoke paths only.
- **Uploading hidden prompts, answer keys, or judge rubrics to executor jobs.** Generation payloads stay answer-key-safe.
- **Runner-specific pass semantics.** Missing optional trace support is reported as unavailable. If a manifest declares a process assertion, missing trace evidence fails that assertion.

## Existing contracts preserved

Trace support must not break these current contracts:

- `evals/shared-benchmark.json` supports `version` `1` and `2`; both grade identically (version 2 only makes the severity/oracle-tier defaults explicit, and `migrate` upgrades a v1 manifest). A v1 manifest keeps validating and benchmarking unchanged.
- Existing objective assertions (`contains`, `regex`, `file_exists`, `json_field_equals`, `script`) keep their behavior.
- `script` assertions remain gated by `--allow-scripts`.
- `prepare` still omits `expected_behavior`, judge rubrics, and answer keys unless an explicit debug/judge flag requests them.
- `benchmark` can grade the current `output.md`/`metadata.json` layout without traces.
- Jetty remains an execution/import adapter, not the grading source of truth.

## Run artifact contract

Current required/optional artifacts remain valid:

```text
runs/<case_id>/<variant>/run-1/output.md
runs/<case_id>/<variant>/run-1/metadata.json
runs/<case_id>/<variant>/run-1/outputs/<artifact files>
```

Trace-aware runners should add these optional artifacts:

```text
runs/<case_id>/<variant>/run-1/trace.jsonl
runs/<case_id>/<variant>/run-1/events.json
runs/<case_id>/<variant>/run-1/metrics.json
runs/<case_id>/<variant>/run-1/environment.json
runs/<case_id>/<variant>/run-1/git-status.txt
```

Artifact roles:

| Artifact | Purpose |
|---|---|
| `trace.jsonl` | Raw runner event stream. Preserve enough data to debug adapter bugs, with secrets redacted where possible. |
| `events.json` | Normalized event list used by process assertions and viewer output. |
| `metrics.json` | Derived counters and timings: tokens, commands, retries, errors, tool calls, elapsed time. |
| `environment.json` | Runner, model, sandbox, adapter version, and host capability metadata. |
| `git-status.txt` | Optional final repo cleanliness snapshot for repo-changing tasks. |

`metadata.json` stays the lightweight summary used by existing reports. `metrics.json` is the richer process/efficiency record.

## Normalized event schema

`events.json` should be a JSON object:

```json
{
  "schema_version": 2,
  "source": "codex",
  "events": [
    {
      "index": 1,
      "timestamp": "2026-06-11T12:00:00Z",
      "type": "command",
      "name": "bash",
      "status": "completed",
      "input_summary": "npm test",
      "output_summary": "17 passed",
      "exit_code": 0,
      "duration_ms": 1200,
      "tokens": {"input": 100, "output": 20},
      "otel": {"gen_ai.operation.name": "execute_tool", "gen_ai.tool.name": "bash", "process.exit_code": 0},
      "raw_ref": {"file": "trace.jsonl", "line": 12},
      "raw_result_ref": {"file": "trace.jsonl", "line": 13}
    }
  ]
}
```

`events.json` and `metrics.json` are now `schema_version: 2`: each event carries an `otel` block of OpenTelemetry GenAI semantic-convention attributes (`gen_ai.operation.name`, `gen_ai.tool.name`, `gen_ai.usage.*`, …) alongside the harness's own fields, and `metrics.json` gains a `gen_ai.usage` block plus the normalized `usage_normalized`/`cost_normalized` cost telemetry. Grading still reads the harness fields, and a version-1 `events.json` still grades unchanged.

Required normalized fields:

- `index`
- `type`
- `status`
- `state_source`
- `raw_ref` when a raw trace exists

`raw_ref.line` is the physical JSONL line of the invocation, including any blank,
malformed, or non-object lines that preceded it. For providers that split tool
invocation and result into separate records, `raw_result_ref` points to the physical
result line. A completed error result remains a completed action, carries
`is_error: true`, increments the error counter, and emits `error.type` telemetry.

`status` is the serialized form of `trace_contracts.EventState`: `completed`, `in_progress`,
`failed`, or `unknown`. The adapter records whether the state came from an explicit provider
status, an intrinsically terminal/start event kind, explicit legacy adaptation, or remained
unknown. Missing/misspelled status is never optimistically completed. A provider event kind may prove state
only when the status field is absent; a present null or wrong-typed status is malformed/unknown and
cannot be upgraded by the kind. Command grading and derived
tool/command/file/skill counters consume completed events only, so a start event counts zero and a
start/end pair counts once.

Recommended fields:

- `timestamp`
- `name`
- `input_summary`
- `output_summary`
- `exit_code`
- `duration_ms`
- `tokens`

Initial event `type` values:

| Type | Meaning |
|---|---|
| `message` | User/assistant/system message or final answer event. |
| `tool_call` | Generic tool call where no more specific type is available. |
| `command` | Shell/process command. |
| `file_read` | File read/open event. |
| `file_write` | File write/edit event. |
| `skill_load` | Skill file or reference was loaded. |
| `error` | Runner, model, tool, or adapter error. |
| `metric` | Token/timing/counter event from runner metadata. |

Adapters may preserve additional fields under `raw` or `details`, but assertions should depend on normalized fields.

## Metrics schema

`metrics.json` should be a JSON object:

```json
{
  "schema_version": 2,
  "elapsed_ms": 12345,
  "input_tokens": 1000,
  "output_tokens": 500,
  "total_tokens": 1500,
  "tool_calls": 8,
  "commands": 3,
  "file_reads": 4,
  "file_writes": 2,
  "errors": 0,
  "retries": 0,
  "repeated_command_max": 1,
  "skill_invoked": true,
  "skill_invocation_evidence": ["skills/good-readme/SKILL.md"]
}
```

If a runner does not expose a metric, omit the field or set it to `null`; do not invent values.

## Manifest taxonomy additions

These fields are optional and backward-compatible:

```json
{
  "id": "case-id",
  "split": "tune",
  "kind": "behavior",
  "domain": "software-engineering",
  "difficulty": "core",
  "estimated_human_minutes": 30,
  "trigger_type": "implicit",
  "success_goals": ["outcome", "process", "style", "efficiency"],
  "runner_requirements": {
    "writes_files": true,
    "network": false,
    "trace_required": false,
    "sandbox": "workspace-write"
  }
}
```

Recommended values:

| Field | Values |
|---|---|
| `difficulty` | `core`, `extended`, `extreme` |
| `trigger_type` | `explicit`, `implicit`, `contextual`, `negative-near-miss` |
| `success_goals` | `outcome`, `process`, `style`, `efficiency`, `trigger` |

`audit-manifest` should warn when taxonomy coverage is thin, but taxonomy should not be required for existing manifests.

## New assertion families

Process and efficiency assertions are objective assertions, but they grade `events.json`, `metrics.json`, and optional repo snapshots rather than final text.

### Process assertions

```json
{"name": "loaded-skill", "type": "skill_invoked", "expected": true}
{"name": "ran-tests", "type": "command_ran", "pattern": "npm test"}
{"name": "did-not-delete", "type": "command_not_ran", "pattern": "rm -rf"}
{"name": "order", "type": "command_order", "patterns": ["npm install", "npm test"]}
{"name": "bash-budget", "type": "tool_count_le", "tool": "bash", "max": 12}
{"name": "no-command-loop", "type": "no_repeated_command_loop", "max_repeats": 2}
```

Process assertions fail when required trace evidence is missing. That fail-closed behavior prevents a runner without trace support from silently satisfying a process requirement. Use variant filters for asymmetric checks, for example `skill_invoked=true` on `with_skill` and `skill_invoked=false` on `without_skill`.

### Efficiency assertions

```json
{"name": "token-budget", "type": "total_tokens_le", "max": 60000}
{"name": "time-budget", "type": "elapsed_seconds_le", "max": 300}
{"name": "command-budget", "type": "command_count_le", "max": 20}
```

Efficiency assertions fail only when explicitly declared and the required metric is missing or over budget. Reports should separately show metric availability so missing telemetry is not confused with poor performance.

### Repository/artifact assertions

These are useful for code-writing skills, but should be implemented after event/metric assertions:

```json
{"name": "repo-clean", "type": "git_clean", "allow": ["eval-runs/**"]}
{"name": "changed-only-src", "type": "file_allowlist", "paths": ["src/**", "package.json"]}
```

Build/test verification should usually stay as a `script` oracle because it is already fail-closed behind `--allow-scripts`.

## Runner adapter plan

### `import-trace`

Add a pure local command that converts a raw runner trace into normalized artifacts:

```sh
skill-benchmark import-trace \
  --source codex \
  --trace eval-runs/latest/case/with_skill/run-1/trace.jsonl \
  --run-dir eval-runs/latest/case/with_skill/run-1
```

`import-trace` currently accepts `generic`, `codex`, `pi`, and `jetty` source dialects. Runner-specific adapters can still add richer normalization as their event streams stabilize.

### `run-codex`

`run-codex` is the Codex runner command for prepared task rows:

```sh
skill-benchmark prepare evals/shared-benchmark.json --split tune --out tasks.jsonl
skill-benchmark run-codex --tasks tasks.jsonl --runs eval-runs/codex-tune
skill-benchmark benchmark evals/shared-benchmark.json --runs eval-runs/codex-tune
```

Current behavior:

- execute each task with `codex exec --json`;
- save raw stdout JSONL as `trace.jsonl`;
- extract final answer into `output.md`;
- normalize events into `events.json`;
- write token/tool/command counts into `metrics.json` and summary fields into `metadata.json`;
- treat nonzero/timeout runs as failed run records, not silent skips.

### Existing runners

- **Pi smoke and trigger evals** use stream events for skill-load evidence and now write normalized `trace.jsonl`, `events.json`, and `metrics.json` artifacts where enabled. Pi smoke runs execute from isolated temporary workspaces so `without_skill` cannot discover source-repo skill files by grep/find/read.
- **Jetty** imports trajectories and metadata into `trace.jsonl`, `events.json`, and `metrics.json` using documented/mocked trajectory shapes; live token validation is still needed for production response shape drift.
- **OpenCode/Gemini CLI** can be added later if they provide stable machine-readable event streams.

## Report additions

Add these fields to `benchmark.json` summary sections when enough data exists:

| Field | Meaning |
|---|---|
| `absolute_delta` | `with_skill_pass_rate - without_skill_pass_rate`. |
| `normalized_gain` | `(with_skill - without_skill) / (1 - without_skill)` when `with_skill >= without_skill` and baseline `< 1`; otherwise `null`. |
| `negative_delta_cases` | Cases where `with_skill` underperforms `without_skill`, `old_skill`, or a relevant ablation. |
| `process_pass_rate` | Pass rate for process assertions only. |
| `efficiency_summary` | Token/elapsed/command distributions by variant. |
| `slice_summary` | Pass rates grouped by `domain`, `difficulty`, `trigger_type`, and `success_goals`. |
| `telemetry_availability` | Which variants/runners emitted trace, token, command, and skill-load evidence. |

Do not weaken saturated outcome assertions just to make process metrics look useful. Outcome, process, style, trigger, and efficiency are separate evidence channels.

## Dataset and oracle audit additions

Extend `audit-manifest` or add `audit-dataset` with warnings for:

- missing positive/negative/adversarial/trigger balance;
- missing `domain`, `difficulty`, or `success_goals` coverage;
- all cases concentrated in one easy slice;
- weak literal `contains*` assertions that duplicate prompt text;
- prompt or fixture leakage of exact answer-key constants;
- fixtures without deterministic oracles for artifact-heavy tasks;
- script oracles that are referenced but not runnable under `--allow-scripts`;
- hidden split placeholders that are accidentally public;
- with-skill saturation where `without_skill` also passes;
- ablations declared but never run.

SkillsBench-style reference-solution verification can be added later as an opt-in field, for example:

```json
{"oracle_reference_run": "evals/reference/case-id"}
```

Dataset/oracle audit work should continue as warnings and report hygiene before adding mandatory gates.

## Structured judge results

The harness keeps the judge-command model: it exports prompts and reads user-supplied model output.
Canonical boolean/scored/dimension/dynamic verdict schemas are validated locally. Schema-invalid
new judge output always fails closed and retains diagnostics. The old `--strict-judge-schema` and
manifest `judge.schema_enforcement` controls remain accepted as deprecated compatibility no-ops:

```json
{
  "judge_task_id": "case::with_skill::run-1::style-rubric",
  "passed": true,
  "score": 4,
  "threshold": 3,
  "check_id": "style-rubric",
  "evidence": "Specific evidence from output",
  "schema_version": 1
}
```

A custom per-assertion schema remains a possible follow-on beyond the shipped canonical shapes:

```json
{
  "name": "style-rubric",
  "type": "judge",
  "rubric": ["specific", "actionable", "maintainer-friendly"],
  "schema": "evals/schemas/style-rubric.schema.json"
}
```

Schema validation is local and deterministic. It does not select a judge model or make live calls by default.

## Skill profile audit

The shipped `profile-skill` command reports:

- `SKILL.md` size and approximate token count;
- number of referenced files;
- reference sizes;
- number of modules/subskills;
- default-load versus conditional-load text;
- likely over-broad/comprehensive-doc warnings.

This adopts the SkillsBench lesson that focused 2–3-module skills often outperform large comprehensive documentation bundles, without turning that heuristic into a hard rule.

## Implementation phases

### Phase 1 — trace schema and local grading plumbing

- [x] Add `trace.jsonl`, `events.json`, `metrics.json`, and `environment.json` to the documented run contract.
- [x] Add pure parser/normalizer helpers with fixture tests.
- [x] Add process/efficiency assertion types with missing-evidence fail-closed tests.
- [x] Extend `benchmark` reports with telemetry availability and negative-delta sections.

### Phase 2 — Codex adapter

- [x] Implement `run-codex` around a `codex exec --json`-compatible command.
- [x] Save raw and normalized traces.
- [x] Extract final answer to `output.md`.
- [x] Treat timeout/nonzero runs as failed records.
- [x] Add mocked JSONL fixtures; keep live Codex smoke opt-in.

### Phase 3 — audit/report quality

- [x] Add taxonomy warnings and slice summaries.
- [x] Add normalized gain for paired variants.
- [x] Expand leakage/anti-cheating lint beyond prompt/assertion literals (the output-side contamination perimeter: `contamination` — canary tripwire, n-gram containment, released_at/cutoff gate).
- [x] Add skill-profile reporting.

### Phase 4 — judge/viewer improvements

- [x] Add viewer panels and richer artifacts, plus human feedback export (`render-viewer --serve` with `feedback.json` capture, image/pdf/xlsx artifact embedding, and a `--previous-workspace` iteration diff).
- [x] Add blind pairwise comparison (`compare-tasks` / `compare-results`, keyed by stable IDs with model-facing blinding).
- [x] Add JSON Schema validation for newly produced judge results (`verdict_schema_for` post-hoc validation always fails malformed evidence closed; `--strict-judge-schema` / manifest `judge.schema_enforcement` remain accepted as deprecated compatibility no-ops).

### Phase 5 — broader runner import

- [x] Normalize mocked/documented Jetty trajectories into trace artifacts during `import-jetty-results`.
- [x] Normalize Pi smoke and Pi trigger stream events where available.
- [ ] Validate live Jetty trajectory shapes with `JETTY_API_TOKEN` before treating Jetty process evidence as production-grade.
- [ ] Add OpenCode/Gemini adapters only when they expose stable machine-readable traces.

## Acceptance criteria for the shipped trace slice

The shipped slice includes:

- fixture-backed unit tests for trace normalization;
- tests that process assertions fail when `events.json` is missing;
- tests that efficiency assertions fail when required metrics are missing;
- tests that existing manifests without trace fields still validate and benchmark;
- tests for variant-scoped process assertions;
- tests for Pi, Codex, Jetty, and Pi-trigger trace artifact paths;
- README updates documenting artifacts, assertion types, runner commands, and the isolated Pi smoke workspace;
- no live runner/API dependency in unit tests;
- no answer-key fields in executor payloads.

## Open questions

- Which Codex JSONL event names beyond the observed `item.completed`, `command_execution`, and `turn.completed` shapes are stable enough to normalize directly?
- Should `skill_invoked` require a file-read event, an explicit runner skill-load event, or either?
- Should `normalized_gain` be reported only for aggregate variants or also per case?
- How aggressively should command inputs be redacted in raw `trace.jsonl` copies?
- Should `git_clean` live as a first-class assertion or remain a `script` oracle pattern?
