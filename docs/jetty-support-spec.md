# Jetty Support Spec

Status: planned implementation, grounded in public Jetty docs checked on 2026-06-09.

This spec defines how Skill Eval Harness should integrate with Jetty without changing the harness manifest, split, assertion, or grading model. Jetty is an execution adapter: it can run tasks, record trajectories, persist artifacts, and optionally run LLM judges, but the local harness remains the source of truth for manifests and deterministic grading.

## Goals

1. Run existing `evals/shared-benchmark.json` manifests on Jetty.
2. Preserve the current harness run layout so existing `benchmark`, `render-viewer`, `compare-results`, and judge-result merging keep working.
3. Keep answer keys, hidden prompts, and skill files fail-closed by default.
4. Support paired variants:
   - `with_skill`
   - `without_skill`
   - `old_skill`
   - `ablation:<id>`
5. Import Jetty trajectories/artifacts into local run directories.
6. Add Jetty judge support as an optional layer, not a replacement for local deterministic assertions.
7. Implement the adapter with red-green-refactor TDD and correctness-by-construction constraints.

## Non-goals

- Do not rewrite the manifest schema around Jetty.
- Do not make Jetty required for local or non-Jetty users.
- Do not make live Jetty API calls from deterministic unit tests.
- Do not claim ablation value until `ablation:<id>` rows are actually run and benchmarked.
- Do not upload answer keys or hidden prompt files into public artifacts.

## Jetty docs consulted

Public docs checked:

- `https://docs.jetty.io/docs/api/chat-completions`
- `https://docs.jetty.io/docs/api/overview`
- `https://docs.jetty.io/docs/guides/writing-runbooks`
- `https://docs.jetty.io/docs/guides/custom-benchmarks`
- `https://docs.jetty.io/docs/guides/evaluating-llms`
- `https://docs.jetty.io/docs/agents/overview`
- `https://docs.jetty.io/docs/architecture/overview`

Confirmed by docs:

- Chat completions endpoint: `POST https://flows-api.jetty.io/v1/chat/completions`.
- Auth: `Authorization: Bearer $JETTY_API_TOKEN`.
- `chat/completions` has two modes:
  - passthrough OpenAI-compatible mode, without a `jetty` block;
  - runbook mode, with a `jetty` block.
- Runbook-mode `jetty` block supports at least:
  - `runbook: true`
  - `collection`
  - `task`
  - `agent`
  - `file_paths`
- Runbook sandbox persists files written to `/app/results/` as trajectory artifacts.
- Polling trajectory status is supported.
- File upload exists before referencing uploaded paths in `file_paths`.
- Workflow APIs also exist under `/api/v1/run/{collection}/{task}`, `/api/v1/run-sync/{collection}/{task}`, and trajectory detail/list endpoints.
- Agent runtime IDs include:
  - `claude-code`
  - `codex`
  - `gemini-cli`
  - `hermes`
- `simple_judge` exists for binary and scale LLM-as-judge workflows.

Open API questions to verify with a real Jetty token:

1. Which polling endpoint is canonical for chat-completion runbook trajectories:
   - docs mention `GET /api/v1/trajectories/{trajectory_id}`;
   - API overview also documents `/api/v1/db/trajectory/{collection}/{task}/{trajectory_id}`.
2. Exact file upload endpoint request/response shape for chat-completion runbook `file_paths`.
3. Exact artifact download URLs and whether they require auth, signed URLs, or a separate API call.
4. Whether `flows-api.jetty.io` vs `api.jetty.io` differences in docs are intentional or docs drift.
5. Whether directory trees/full repo checkouts can be uploaded directly, zipped, or must be enumerated as individual files.
6. Exact terminal status strings beyond `pending`, `running`, `completed`, and `failed`.
7. Custom sandbox image support and availability by agent runtime.

## Architecture

The adapter has five layers:

```text
manifest + cases
   |
   v
prepared harness task rows
   |
   v
Jetty export planner  ---> mount plan / upload plan / safety policy
   |
   v
Jetty payloads + runbook
   |
   v
Jetty execution / polling
   |
   v
Jetty importer
   |
   v
existing harness run layout -> existing benchmark/grading
```

The local deterministic grading path stays unchanged. Jetty produces `output.md`, `metadata.json`, and optional artifacts; the harness imports those into the same layout local runners already use.

## CLI commands

### `export-jetty`

Converts harness cases into Jetty payload JSONL. This should support two entry styles:

```sh
# Directly from a manifest, mirroring prepare flags
skill-benchmark export-jetty evals/shared-benchmark.json \
  --split tune \
  --runs-per-variant 3 \
  --out jetty-payloads.jsonl

# Or from previously prepared task rows
skill-benchmark prepare evals/shared-benchmark.json --split tune --out tasks.jsonl
skill-benchmark export-jetty evals/shared-benchmark.json \
  --tasks tasks.jsonl \
  --out jetty-payloads.jsonl
```

Flags:

- `--split tune|holdout|holdback`
- `--runs-per-variant N`
- `--include-old-skill`
- `--include-ablations`
- `--allow-missing-prompts` only for dry-run planning
- `--include-answer-key` should be rejected for executor payloads unless a future explicit judge-export mode needs it
- `--jetty-collection NAME`
- `--jetty-task-prefix PREFIX`
- `--jetty-agent claude-code|codex|gemini-cli|hermes|...`
- `--jetty-model MODEL`
- `--runbook RUNBOOK.md` or `--runbook-template default`
- `--upload-plan-out upload-plan.json`
- `--dry-run`

Output: JSONL rows. Each row contains a harness identity block, a Jetty request body, and a mount/upload plan. Example shape:

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
      {"role": "system", "content": "<runbook contents>"},
      {"role": "user", "content": "Execute the harness task."}
    ],
    "stream": true,
    "jetty": {
      "runbook": true,
      "collection": "skill-evals",
      "task": "good-pr-pos-security-meaningless-test-with-skill-1",
      "agent": "claude-code",
      "file_paths": ["uploads/.../task.json", "uploads/.../skills/good-pr/SKILL.md"]
    }
  },
  "upload_plan": {
    "files": [
      {"local_path": "/abs/repo/evals/fixtures/security-pr/diff.patch", "remote_path": "uploads/fixtures/diff.patch", "role": "fixture"}
    ]
  }
}
```

### `run-jetty`

Submits exported payloads and polls for completion.

```sh
skill-benchmark run-jetty \
  --payloads jetty-payloads.jsonl \
  --out jetty-runs.jsonl \
  --concurrency 4
```

Environment:

- `JETTY_API_TOKEN` or `JETTY_API_KEY`
- optional `JETTY_BASE_URL`, default `https://flows-api.jetty.io`

Responsibilities:

- Upload files before submission.
- Submit requests.
- Persist `trajectory_id` immediately after receiving it.
- Poll until terminal status.
- Retry transient API failures with bounded backoff.
- Respect concurrency and rate-limit responses.
- Write a run record even on failure/timeout.

### `import-jetty-results`

Downloads/imports Jetty trajectory outputs into the local run directory layout.

```sh
skill-benchmark import-jetty-results \
  --manifest evals/shared-benchmark.json \
  --jetty-runs jetty-runs.jsonl \
  --runs eval-runs/jetty

skill-benchmark benchmark evals/shared-benchmark.json \
  --runs eval-runs/jetty \
  --out benchmark.json
```

Expected output layout:

```text
eval-runs/jetty/<case_id>/<variant>/run-<n>/output.md
eval-runs/jetty/<case_id>/<variant>/run-<n>/metadata.json
eval-runs/jetty/<case_id>/<variant>/run-<n>/outputs/...
eval-runs/jetty/<case_id>/<variant>/run-<n>/jetty_raw.json
```

For single-run variants, the importer may follow the existing harness convention and omit `run-<n>` if `runs_per_variant == 1`; repeated runs must use `run-<n>`.

## Manifest extension

Add an optional `jetty` block. It is never required for local users.

```json
{
  "jetty": {
    "collection": "skill-evals",
    "task_prefix": "good-pr",
    "agent": "claude-code",
    "model": "claude-sonnet-4-6",
    "runbook_url": null,
    "sandbox_image": null,
    "grader_mode": "local_only",
    "skill_mount_strategy": "variant-aware",
    "variants": {
      "with_skill": {
        "agent": "claude-code"
      },
      "without_skill": {
        "skill_mount_strategy": "omit"
      }
    }
  }
}
```

Validation rules:

- `jetty` is optional.
- Unknown `jetty` fields should warn, not fail, until the schema is stable.
- `jetty.agent`, if present, must be a known runtime or explicitly allowed with `--allow-unknown-jetty-agent`.
- `grader_mode` values:
  - `local_only`
  - `jetty_only`
  - `merge`
- `skill_mount_strategy` values:
  - `variant-aware`
  - `force-mount`
  - `omit`
- Per-variant overrides must not weaken the `without_skill` safety invariant.

## Data model and correctness by construction

Use typed internal objects rather than ad hoc dictionaries until final serialization.

Suggested Python types:

```python
class VariantKind(Enum):
    WITH_SKILL = "with_skill"
    WITHOUT_SKILL = "without_skill"
    OLD_SKILL = "old_skill"
    ABLATION = "ablation"

class TrajectoryStatus(Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELED = "canceled"
    TIMEOUT = "timeout"
    UNKNOWN = "unknown"

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
    remote_path: str
    role: Literal["task", "skill", "old_skill", "ablation_skill", "fixture", "prompt"]
    private: bool

@dataclass(frozen=True)
class MountPlan:
    items: tuple[MountItem, ...]

@dataclass(frozen=True)
class JettyPayload:
    identity: HarnessTaskIdentity
    request: dict[str, Any]
    upload_plan: MountPlan
```

Key invariants:

- `without_skill` mount plans must not include `role in {"skill", "old_skill", "ablation_skill"}`.
- `with_skill` mount plans must include at least one skill file unless `--instruction-only-skill` is explicitly set.
- `old_skill` is invalid unless `manifest.old_skill_paths` exists.
- `ablation:<id>` must either:
  - mount a materialized ablated skill artifact, or
  - include an explicit ablation patch/instruction marked as an approximation.
- `prompt_ref` content can be loaded into executor payloads only when the split is being executed; it must not be included in public exports unless explicitly private-labeled.
- `expected_behavior`, judge rubrics, and answer keys must not be present in executor payloads.
- Unknown trajectory status fails closed and writes metadata; it must not be treated as success.

## Skill, prompt, and fixture mounting

Mount/upload roles:

| Variant | Skill files | Fixture files | Hidden prompt refs | Notes |
|---|---|---|---|---|
| `with_skill` | include | include | include only for executor job | normal skill run |
| `without_skill` | omit | include | include only for executor job | file-control matters more than prose instruction |
| `old_skill` | include old only | include | include only for executor job | requires `old_skill_paths` |
| `ablation:<id>` | include ablated artifact or patch | include | include only for executor job | approximation must be labeled |

Implementation detail:

- Generate a `task.json` file per Jetty task containing only the generation-safe prompt, identity, input file paths, and variant instruction.
- Do not put answer keys or judge rubrics in `task.json`.
- If a case uses `files`, upload them under a stable fixture prefix.
- If a case uses `prompt_ref`, resolve it only when the execution split is intentionally being run.

## Canonical runbook template

Jetty docs say runbooks need structure, declared outputs, evaluation, and that files under `/app/results/` are persisted. The default harness runbook should be generated or shipped as a template.

```md
---
version: 1
evaluation: programmatic
---

# Skill Eval Harness Task

## Objective

Execute one Agent Skill benchmark task. Produce the final assistant answer and metadata in the required result files.

## REQUIRED OUTPUT FILES

- `/app/results/output.md` — final assistant answer only.
- `/app/results/metadata.json` — JSON object with timing/model/tool/error metadata.
- `/app/results/outputs/` — optional generated artifacts.

## Parameters

- `task_json`: uploaded task file path.
- `skill_files`: uploaded skill file paths, if this variant allows them.
- `fixture_files`: uploaded fixture file paths.

## Steps

1. Read `task_json`.
2. Read fixture files listed in `task_json.input_files` before answering.
3. For `with_skill`, read and follow mounted skill files and relevant references.
4. For `without_skill`, do not read or use skill files; no skill files should be mounted.
5. Produce the answer directly for the user task.
6. Write `/app/results/output.md`.
7. Write `/app/results/metadata.json`.
8. Copy any additional artifacts into `/app/results/outputs/`.

## Expected response

The final response should be in `/app/results/output.md`. Do not include grading rubrics or hidden answer keys.

## Evaluation

Programmatic evaluation is performed later by Skill Eval Harness after importing results.
```

## Import metadata

`metadata.json` should normalize Jetty fields while preserving raw details.

```json
{
  "provider": "jetty",
  "model": "claude-sonnet-4-6",
  "elapsed_ms": 12345,
  "input_tokens": 1000,
  "output_tokens": 500,
  "total_tokens": 1500,
  "total_tool_calls": 8,
  "errors_encountered": 0,
  "returncode": 0,
  "timed_out": false,
  "jetty_trajectory_id": "traj_...",
  "jetty_collection": "skill-evals",
  "jetty_task": "good-pr-pos-security-meaningless-test-with-skill-1",
  "jetty_agent": "claude-code",
  "trace_url": "https://...",
  "jetty_raw_path": "jetty_raw.json"
}
```

On failures, still write `metadata.json` and a minimal `output.md`:

```md
[JETTY FAILURE: trajectory failed before producing output]
```

This lets `benchmark` report missing/failing rows without crashing the entire import.

## Jetty judge integration

Local deterministic assertions remain primary. Jetty judge support is optional.

Modes:

- `local_only`: do not export judge tasks to Jetty.
- `jetty_only`: use Jetty `simple_judge` outputs for judge assertions.
- `merge`: import Jetty judge results as `judge-results.jsonl` and combine with local objective assertions.

`simple_judge` docs confirm:

- `judge_type: "binary"` for yes/no.
- `judge_type: "scale"` with `scale_range` for numeric scores.
- `instruction` defines the evaluation criteria.
- `item_path` points at the content being judged.

Mapping:

| Harness field | Jetty judge field |
|---|---|
| `judge_task_id` | preserved in metadata/input |
| `passed` | binary result or score >= threshold |
| `score` | scale score |
| `threshold` | manifest assertion threshold or default |
| `evidence` | judge explanation/output |

Answer-key safety:

- Judge exports may include rubrics only for judge tasks, not executor tasks.
- Judge task payloads must not be accessible to the generation run.

## TDD plan

Use red-green-refactor. Do not start with live Jetty calls.

### Phase 1 — Exporter golden tests

Red tests:

1. `export-jetty` emits valid OpenAI-compatible fields: `model`, `messages`, `stream`.
2. Runbook payload includes `jetty.runbook: true`, `collection`, `task`, `agent`, `file_paths`.
3. Harness identity is preserved.
4. Executor payload omits `expected_behavior`, judge rubrics, and answer keys.
5. `prompt_ref` missing fails unless `--allow-missing-prompts` is used.
6. `--allow-missing-prompts` dry-run labels payload as non-executable.

Green:

- Implement pure `build_jetty_payload(task, manifest, config)`.
- Add CLI wrapper only after pure function tests pass.

Refactor:

- Extract typed payload/mount plan builders.

### Phase 2 — Variant mount-policy tests

Red tests:

1. `with_skill` mount plan includes skill file(s).
2. `without_skill` mount plan excludes all skill/reference files.
3. `old_skill` without `old_skill_paths` fails.
4. `ablation:<id>` without materialized ablation is labeled approximation or fails depending on mode.
5. Fixture files from `case.files` are included.
6. Private prompt refs are marked private.

Green:

- Implement `build_mount_plan`.

Refactor:

- Make invalid mount states unrepresentable with dataclasses/enums.

### Phase 3 — Importer round-trip tests

Red tests:

1. Mock completed trajectory with `output.md` artifact imports to expected run directory.
2. Mock repeated run imports to `run-<n>` directory.
3. Mock missing artifact writes failure `output.md` and `metadata.json`.
4. Mock artifact directory imports into `outputs/`.
5. Imported runs can be graded by existing `benchmark`.

Green:

- Implement `import_jetty_result` pure transformation.

Refactor:

- Normalize metadata extraction and raw preservation.

### Phase 4 — Polling state-machine tests

Red tests:

1. `pending -> running -> completed` succeeds.
2. `failed` writes failure record.
3. timeout writes timeout record.
4. unknown status fails closed.
5. transient 429/5xx retries with bounded backoff.
6. retry budget exhaustion writes failure record.

Green:

- Implement `JettyClient` with injectable HTTP transport and fake clock.

Refactor:

- Keep network code at the edge; keep state transitions pure.

### Phase 5 — CLI integration tests with mocked HTTP

Red tests:

1. `export-jetty | run-jetty --dry-run` produces stable run records.
2. `run-jetty` with mocked upload/submission/polling writes `jetty-runs.jsonl`.
3. `import-jetty-results` from mocked `jetty-runs.jsonl` creates benchmarkable run dirs.

Green:

- Wire CLI commands.

Refactor:

- Reduce duplication between `prepare` and `export-jetty`.

### Phase 6 — Live smoke, opt-in only

Requires `JETTY_API_TOKEN`.

- One tiny fixture-free tune case.
- One fixture-backed case.
- One trigger/no-trigger check is not required initially; trigger checks are Pi-specific today.
- One failed/timeout scenario if Jetty supports a cheap deterministic failure.

Never run live tests in default CI.

## Test fixtures

Add under `tests/fixtures/jetty/`:

```text
tests/fixtures/jetty/
├── prepared-task-with-skill.json
├── prepared-task-without-skill.json
├── manifest-with-jetty.json
├── trajectory-completed.json
├── trajectory-failed.json
├── trajectory-unknown-status.json
├── artifact-output.md
└── artifact-metadata.json
```

## Security and privacy tests

Required tests:

- Executor payload never contains `expected_behavior`.
- Executor payload never contains `review_rubric`.
- Executor payload never contains judge assertion rubrics unless using judge export mode.
- `without_skill` payload cannot upload skill files.
- Hidden `prompt_ref` content is not included in dry-run/public exports.
- Private uploaded files are labeled private in the upload plan.
- Raw Jetty metadata is preserved in `jetty_raw.json` but not used as grading truth.

## Acceptance criteria for first release

The first Jetty release is acceptable when:

1. `export-jetty` has golden tests and produces stable JSONL.
2. `import-jetty-results` can import mocked trajectories and existing `benchmark` can grade the imported run layout.
3. Variant mount-policy tests pass.
4. Hidden prompt/answer-key safety tests pass.
5. `run-jetty --dry-run` works without a token.
6. Live `run-jetty` is behind `JETTY_API_TOKEN` and has at least one documented manual smoke test.
7. README documents the Jetty flow and explicitly says local harness grading remains source of truth.

## Implementation order

1. Add data types and pure export functions.
2. Add `export-jetty` tests and command.
3. Add mount-plan safety tests.
4. Add mocked importer tests and `import-jetty-results`.
5. Add mocked `JettyClient` and `run-jetty --dry-run`.
6. Add live `run-jetty` submission/polling.
7. Add optional `simple_judge` export/import.
8. Add README and example manifest snippets.
9. Only then run live token-backed validation.

## Documentation updates after implementation

Update:

- `README.md` with Jetty quick start.
- `TODO.md` to mark completed sections.
- `LESSONS_LEARNED.md` with API surprises found during live testing.
- `tests/fixtures/jetty/README.md` documenting fixture provenance.

## Key design principle

Jetty executes. Skill Eval Harness decides.

The harness should continue to decide:

- which cases exist;
- which split is being run;
- which variant is being compared;
- which deterministic assertions pass;
- how judge results are merged;
- whether a case is saturated, flaky, no-lift, or failed.

Jetty should provide:

- isolated execution;
- agent runtime selection;
- trajectory records;
- artifact persistence;
- optional judge workflows.
