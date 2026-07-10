# Jetty Support Spec

Status: phase-1 adapter implemented for deterministic export, REST submission/polling, dry-run mode, and mocked trajectory import; live Jetty response shapes still need token-backed validation. Grounded in Jetty public docs and `jettyio/jettyio-skills` checked on 2026-06-09.

This spec defines the simplest useful Jetty adapter for Skill Eval Harness. Jetty executes harness tasks in runbook mode and records trajectories/artifacts. Skill Eval Harness remains the source of truth for manifests, variants, splits, deterministic assertions, benchmark summaries, and saturation/no-lift flags.

## Design principle

**Jetty executes. Skill Eval Harness decides.**

Jetty should provide:

- sandboxed agent execution;
- agent runtime/model/snapshot selection;
- trajectory records;
- artifact persistence;
- optional LLM judge workflows later.

The harness should continue to decide:

- which cases exist;
- which split is being run;
- which variant is being compared;
- what files/prompts/skills are allowed in that variant;
- which deterministic assertions pass;
- how judge results are merged;
- whether a case is saturated, no-lift, flaky, or failed.

## Grounding sources

Public Jetty docs consulted:

- `https://docs.jetty.io/docs/api/chat-completions`
- `https://docs.jetty.io/docs/api/overview`
- `https://docs.jetty.io/docs/guides/writing-runbooks`
- `https://docs.jetty.io/docs/guides/custom-benchmarks`
- `https://docs.jetty.io/docs/guides/evaluating-llms`
- `https://docs.jetty.io/docs/agents/overview`
- `https://docs.jetty.io/docs/architecture/overview`

Jetty skills repo consulted:

- `https://github.com/jettyio/jettyio-skills`
- `README.md` — MCP tools and setup flow.
- `skills/jetty/SKILL.md` — concrete chat-completions runbook-mode payload, file upload, trajectory fetch, and sandbox conventions.
- `skills/jetty/references/agents-and-models.md` — practical agent runtime IDs, model/provider mapping, snapshots, `primary_outputs`, and API-key storage.
- `skills/create-runbook/SKILL.md` — runbook frontmatter and scaffold conventions.
- `skills/jetty/templates/*.json` and `skills/jetty/references/workflow-templates.md` — `simple_judge` examples.

## Confirmed facts to build on

From Jetty docs and `jettyio-skills`:

- Runbook execution uses `POST https://flows-api.jetty.io/v1/chat/completions`.
- Auth uses `Authorization: Bearer $JETTY_API_TOKEN`.
- Chat completions has:
  - passthrough mode when there is no `jetty` block;
  - runbook mode when the `jetty` block is present.
- The runbook content goes in the **system** message.
- The user message can simply be `Execute the runbook.`.
- Runtime parameters go in `jetty.template_variables`, not in the user message.
- Runbook-mode `jetty` block should include:
  - `runbook: true`
  - `collection`
  - `task`
  - `agent`
  - `model_provider`
  - `snapshot`
  - `template_variables`
  - optional `file_paths`
  - optional `use_trial_keys`
- Files can be uploaded first with `POST https://flows-api.jetty.io/api/v1/files/upload` using multipart fields including `file=@...` and `collection=...`; the returned path is referenced in `jetty.file_paths`.
- The practical agent runtimes from `jettyio-skills` are:
  - `claude-code`
  - `opencode`
  - `codex`
  - `gemini-cli`
- Practical default runtime:
  - `agent: claude-code`
  - `model: claude-sonnet-4-6`
  - `model_provider: anthropic`
  - `snapshot: python312-uv`
- Browser tasks should use `snapshot: prism-playwright`.
- Files written under `/app/results/` persist as artifacts.
- `results_dir` should default to `/app/results` and be passed through `jetty.template_variables`.
- Trajectory details are available through `GET /api/v1/db/trajectory/{collection}/{task}/{trajectory_id}`.
- Provider keys belong in Jetty collection environment variables, not in local harness payloads. The only local secret the adapter should require is `JETTY_API_TOKEN`.
- `simple_judge` supports binary and scale judging for later qualitative judge integration.

## Goals

1. Run existing `evals/shared-benchmark.json` cases on Jetty.
2. Keep the local harness run layout unchanged after import.
3. Keep generation payloads answer-key-safe.
4. Enforce variant file boundaries by construction, especially `without_skill`.
5. Support these variants:
   - `with_skill`
   - `without_skill`
   - `old_skill`
   - `ablation:<id>`
6. Import Jetty outputs so existing `benchmark` can grade them.
7. Add optional Jetty `simple_judge` later, after execution/import works.

## Non-goals for the first slice

- No custom benchmark zip upload.
- No MCP dependency in CI or core harness commands.
- No live Jetty calls in unit tests.
- No full repo checkout upload unless live API tests prove the shape.
- No hidden answer-key upload to executor jobs.
- No claim that ablations are useful until `ablation:<id>` rows are actually run.

MCP remains useful for humans/operators. The harness adapter should use REST directly for reproducibility and CI.

## Minimal architecture

```text
manifest + prepare logic
   |
   v
export-jetty: task -> runbook payload + upload plan
   |
   v
run-jetty: upload files, submit, poll trajectory
   |
   v
import-jetty-results: trajectory/artifacts -> local run layout
   |
   v
existing benchmark/grading
```

No grading logic moves into Jetty for the first implementation. Jetty returns artifacts; the harness grades them locally.

## CLI commands

### `export-jetty`

Builds Jetty payload JSONL and upload plans. It should be pure/deterministic: no network required.

```sh
skill-benchmark export-jetty evals/shared-benchmark.json \
  --split tune \
  --runs-per-variant 3 \
  --out jetty-payloads.jsonl
```

Initial flags:

- `--split tune|holdout|holdback`
- `--runs-per-variant N`
- `--include-old-skill`
- `--include-ablations`
- `--allow-missing-prompts` only for dry-run planning
- `--jetty-collection NAME`
- `--jetty-task-prefix PREFIX`
- `--jetty-agent claude-code|opencode|codex|gemini-cli`
- `--jetty-model MODEL`
- `--jetty-model-provider anthropic|openrouter|openai|google|bedrock`
- `--jetty-snapshot python312-uv|prism-playwright`
- `--use-trial-keys`
- `--dry-run`

Do not support `--include-answer-key` for executor payloads. If judge export later needs rubrics, it should be a separate `export-jetty-judge` path.

Payload shape:

```json
{
  "harness": {
    "skill_name": "good-pr",
    "case_id": "pos-security-meaningless-test",
    "variant": "with_skill",
    "run_number": 1,
    "split": "tune",
    "run_dir": "pos-security-meaningless-test/with_skill"
  },
  "jetty_request": {
    "model": "claude-sonnet-4-6",
    "messages": [
      {"role": "system", "content": "<RUNBOOK.md contents>"},
      {"role": "user", "content": "Execute the runbook."}
    ],
    "stream": false,
    "jetty": {
      "runbook": true,
      "collection": "skill-evals",
      "task": "good-pr-pos-security-meaningless-test-with-skill-1",
      "agent": "claude-code",
      "model_provider": "anthropic",
      "snapshot": "python312-uv",
      "template_variables": {
        "results_dir": "/app/results",
        "task_json": "uploads/.../task.json"
      },
      "file_paths": [
        "uploads/.../task.json",
        "uploads/.../skills/good-pr/SKILL.md",
        "uploads/.../fixtures/diff.patch"
      ]
    }
  },
  "upload_plan": {
    "files": [
      {"local_path": "/abs/repo/evals/fixtures/security-pr/diff.patch", "remote_path_hint": "fixtures/diff.patch", "role": "fixture", "private": false}
    ]
  }
}
```

Use `stream: false` by default for simplicity. Streaming can be added after non-streaming import is stable.

### `run-jetty`

Submits exported payloads and polls for completion.

```sh
skill-benchmark run-jetty \
  --payloads jetty-payloads.jsonl \
  --out jetty-runs.jsonl
```

Execution is currently sequential. The parser reserves `--concurrency`, but the executor does not
yet apply it; streaming/concurrent execution remains in `TODO.md`.

Environment:

- `JETTY_API_TOKEN` required for live submission.
- `JETTY_BASE_URL` optional; default `https://flows-api.jetty.io`.

Responsibilities:

1. Upload each file in `upload_plan.files` to `/api/v1/files/upload`.
2. Replace local upload placeholders with returned Jetty file paths.
3. Submit `jetty_request` to `/v1/chat/completions`.
4. Persist the trajectory ID immediately.
5. Poll `/api/v1/db/trajectory/{collection}/{task}/{trajectory_id}` until terminal status.
6. Write one JSONL run record per task, including failures/timeouts.
7. Retry bounded transient 429/5xx failures.

### `import-jetty-results`

Imports Jetty run records and artifacts into local run directories.

```sh
skill-benchmark import-jetty-results \
  --manifest evals/shared-benchmark.json \
  --jetty-runs jetty-runs.jsonl \
  --runs eval-runs/jetty

skill-benchmark benchmark evals/shared-benchmark.json \
  --runs eval-runs/jetty \
  --out benchmark.json
```

Expected layout:

```text
eval-runs/jetty/<case_id>/<variant>/run-<n>/output.md
eval-runs/jetty/<case_id>/<variant>/run-<n>/metadata.json
eval-runs/jetty/<case_id>/<variant>/run-<n>/outputs/...
eval-runs/jetty/<case_id>/<variant>/run-<n>/jetty_raw.json
```

If there is only one run per variant, importer may follow the existing non-repeated layout. Repeated runs must use `run-<n>`.

## Optional manifest extension

The implemented optional fields are `collection`, `task_prefix`, `agent`, `model`,
`model_provider`, `snapshot`, and `use_trial_keys`:

```json
{
  "jetty": {
    "collection": "skill-evals",
    "task_prefix": "good-pr",
    "agent": "claude-code",
    "model": "claude-sonnet-4-6",
    "model_provider": "anthropic",
    "snapshot": "python312-uv",
    "use_trial_keys": false
  }
}
```

`export-jetty` validates `agent` against `claude-code`, `opencode`, `codex`, and `gemini-cli`;
`model_provider` should be explicit, and `snapshot` defaults to `python312-uv`. Manifest-wide
shape validation, `grader_mode`, per-variant overrides, and configurable mount strategies remain
proposed follow-ons tracked in `TODO.md`; the current exporter always derives the mount from the
validated prepared task.

## Canonical harness runbook

The harness should generate a small runbook rather than asking each skill repo to maintain one.

```md
---
version: "1.0.0"
evaluation: programmatic
agent: claude-code
model: claude-sonnet-4-6
model_provider: anthropic
snapshot: python312-uv
primary_outputs:
  - output.md
---

# Skill Eval Harness Task

## Objective

Execute one Skill Eval Harness task exactly once. Write the final assistant answer and metadata to the required output files.

## REQUIRED OUTPUT FILES

| Path | Purpose |
|---|---|
| `{{results_dir}}/output.md` | Final assistant answer only. |
| `{{results_dir}}/metadata.json` | JSON metadata for model/runtime/tool/error data. |
| `{{results_dir}}/outputs/` | Optional generated artifacts. |

## Parameters

- `{{results_dir}}` — defaults to `/app/results` on Jetty.
- `{{task_json}}` — uploaded task JSON generated by Skill Eval Harness.

## Steps

1. Read `{{task_json}}`.
2. Read every fixture listed in `task_json.input_files`.
3. If `task_json.variant` is `with_skill`, read and follow the mounted skill files.
4. If `task_json.variant` is `without_skill`, do not use a skill. No skill files should be mounted.
5. Answer the user task directly.
6. Write `{{results_dir}}/output.md`.
7. Write `{{results_dir}}/metadata.json`.
8. Put any additional generated artifacts under `{{results_dir}}/outputs/`.

## Evaluation

Programmatic evaluation happens after import by Skill Eval Harness. Do not include hidden grading rubrics or answer keys in the output.
```

## Mount policy

| Variant | Skill files | Fixture files | Hidden prompt content | Notes |
|---|---|---|---|---|
| `with_skill` | include | include | executor-only | normal skill run |
| `without_skill` | omit | include | executor-only | file controls matter more than prose |
| `old_skill` | include old only | include | executor-only | requires `old_skill_paths` |
| `ablation:<id>` | include materialized ablated skill or explicit approximation | include | executor-only | approximation must be labeled |

`task.json` should include only generation-safe fields:

- harness identity;
- prompt/user task;
- variant;
- input file paths;
- allowed skill file paths for mounted-skill variants;
- no answer key;
- no judge rubric.

## Secrets policy

Local harness:

- may read `JETTY_API_TOKEN` to call Jetty;
- must not upload local provider API keys;
- must not write local provider API keys into payloads or metadata.

Jetty collection:

- stores provider keys such as `ANTHROPIC_API_KEY`, `OPENROUTER_API_KEY`, `OPENAI_API_KEY`, `GEMINI_API_KEY`;
- may use trial keys when `use_trial_keys: true` is explicitly set.

Optional future preflight:

- call Jetty collection environment endpoint;
- check whether required provider key exists for selected `agent`/`model_provider`;
- fail early with a helpful message if missing.

## Correctness by construction

Use typed internal objects before serializing to JSON.

```python
class VariantKind(Enum):
    WITH_SKILL = "with_skill"
    WITHOUT_SKILL = "without_skill"
    OLD_SKILL = "old_skill"
    ABLATION = "ablation"

JettyLifecycle = Queued | Running | Succeeded | Failed | TimedOut | ProtocolInvalid

@dataclass(frozen=True)
class HarnessTaskIdentity:
    skill_name: str
    case_id: str
    variant: str
    run_number: int
    split: str
    run_dir: str

@dataclass(frozen=True)
class MountItem:
    local_path: Path
    role: Literal["task", "skill", "old_skill", "ablation_skill", "fixture", "prompt"]
    private: bool

@dataclass(frozen=True)
class MountPlan:
    items: tuple[MountItem, ...]
```

Required invariants:

- `without_skill` mount plan contains no `skill`, `old_skill`, or `ablation_skill` items.
- `with_skill` mount plan contains at least one skill item unless explicitly running in instruction-only mode.
- `old_skill` cannot export unless `old_skill_paths` exists.
- `ablation:<id>` must either mount a materialized ablated skill or mark the run as instruction-simulated approximation.
- executor payload never contains `expected_behavior`, `review_rubric`, judge assertion rubrics, or answer keys.
- dry-run payloads with missing prompt refs are labeled non-executable.
- provider status aliases parse once into `JettyLifecycle`; unknown/missing aliases and a conflicting persisted discriminator become `ProtocolInvalid`;
- `Succeeded` is semantic success only when both a non-empty `trajectory_id` and an `output.md` artifact exist; either missing field is protocol-invalid;
- timeout is its own lifecycle, not an ordinary provider failure; dry-run is planning outside the execution lifecycle.

## Import metadata

Normalize Jetty fields into `metadata.json`:

```json
{
  "provider": "jetty",
  "model": "claude-sonnet-4-6",
  "model_provider": "anthropic",
  "elapsed_ms": 12345,
  "input_tokens": 1000,
  "output_tokens": 500,
  "total_tokens": 1500,
  "total_tool_calls": 8,
  "errors_encountered": 0,
  "returncode": 0,
  "timed_out": false,
  "jetty_lifecycle": "succeeded",
  "jetty_trajectory_id": "traj_...",
  "jetty_collection": "skill-evals",
  "jetty_task": "good-pr-pos-security-meaningless-test-with-skill-1",
  "jetty_agent": "claude-code",
  "jetty_snapshot": "python312-uv",
  "trace_url": "https://jetty.io/<collection>/<task>/<trajectory_id>",
  "jetty_raw_path": "jetty_raw.json"
}
```

On failure, still write `output.md` and `metadata.json`:

```md
[JETTY FAILURE: trajectory failed before producing output]
```

## Optional `simple_judge` integration

Do this after execution/import works.

Modes:

- `local_only`: default; no Jetty judge export.
- `jetty_only`: use Jetty judge results for judge assertions.
- `merge`: import Jetty judge results as `judge-results.jsonl` and combine with local objective assertions.

Mapping:

| Harness field | Jetty judge field |
|---|---|
| `judge_task_id` | preserved in task metadata |
| `passed` | binary result or score >= threshold |
| `score` | scale score |
| `threshold` | manifest threshold/default |
| `evidence` | judge explanation/output |

Judge payloads may include rubrics. Executor payloads may not.

## Implemented test record

Phases 1–4 shipped with deterministic tests:

- pure export payload/mount-plan tests cover the runbook block, task template variables, fixtures,
  recursive skill uploads, per-variant mount policy, and answer-key/rubric omission;
- importer integration tests cover completed/failed trajectories, repeated layout, normalized
  artifacts/metadata, safe run destinations, duplicate identity rejection, and local benchmark use;
- fake-client tests cover upload, submit, poll, placeholder replacement, terminal success/failure,
  timeout, protocol-invalid status, and bounded transient retries; and
- CLI tests cover export → mocked execute → import plus token-free dry run and missing-token failure.

The tests use inline provider-shaped records and temporary run trees in
`tests/test_skill_benchmark.py`, `tests/test_runners.py`, `tests/test_cost_telemetry.py`, and
`tests/test_jetty_contracts.py`; there is no separate `tests/fixtures/jetty/` tree.

Security checks prove executor payloads omit answer keys/judge rubrics and provider credentials,
`without_skill` uploads no skill files, hidden prompt placeholders remain non-executable, unsafe or
duplicate imported destinations fail before writes, and unknown/conflicting statuses fail closed.

Phase 5 remains opt-in live validation. It requires `JETTY_API_TOKEN`, never runs in default CI, and
should cover one fixture-free case, one fixture-backed case, and a cheap failure/timeout path.

## Remaining live-token questions

These are narrower after reading `jettyio-skills`:

1. Exact JSON response shape from `/api/v1/files/upload`.
2. Exact artifact listing/download shape from trajectory details.
3. Exact chat-completion response field containing `trajectory_id` in non-streaming runbook mode.
4. Full terminal status set in production.
5. Whether directory trees should be uploaded as individual files or archives.
6. Whether `use_trial_keys` is available to all relevant accounts or only trial collections.

## Simplicity bias

Implement only the REST path first:

- no MCP dependency;
- no custom benchmark zip upload;
- no streaming;
- no custom images;
- no Jetty judge export until execution/import is working;
- no full repo mounting until file upload behavior is proven live.

The first useful integration is: export one task, run it on Jetty, import `output.md`, and grade it locally.
