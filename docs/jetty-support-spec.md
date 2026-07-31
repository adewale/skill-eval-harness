# Jetty Support Spec

Status: adapter validated against production `flows-api.jetty.io` with a real token on
2026-07-17 — the full `export-jetty -> run-jetty -> import-jetty-results -> benchmark`
loop ran live (fixture-free case, fixture-backed case, and a server-side failure path).
Captured, redacted response shapes are committed under `tests/fixtures/jetty/` and
pinned by `tests/test_jetty_live_contract.py`; the opt-in live smoke is
`RUN_JETTY_SMOKE=1` (`tests/test_smoke_jetty.py`). Originally grounded in Jetty public
docs and `jettyio/jettyio-skills` checked on 2026-06-09; the live pass corrected four
assumptions from that snapshot (upload endpoint, in-sandbox paths, trajectory-id shape,
and artifact transport) — see "Live-token answers" below.

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

Verified live on 2026-07-17 (superseding the 2026-06-09 doc snapshot where they differ):

- Runbook execution uses `POST https://flows-api.jetty.io/v1/chat/completions`.
- Auth uses `Authorization: Bearer $JETTY_API_TOKEN`.
- flows-api sits behind Cloudflare, which rejects urllib's default
  `Python-urllib/x.y` User-Agent outright (`403`, error code 1010) — every
  client request must send a real `User-Agent`.
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
  - `timeout_hint` — how long the non-streaming call may block synchronously
    before returning HTTP 202 + `status: "running"`. The server default is
    1200s, which outlives any sane client HTTP timeout; the adapter sends 60s
    and lets polling drive the wait.
  - `template_variables`
  - optional `file_paths`
  - optional `use_trial_keys` (plus `timeout_sec`, `cpus`, `memory`,
    `network_enabled`, `agent_env`, `mcp_servers`, `files`, `webhook_url` —
    unused by the adapter today)
- File upload: there is **no** `POST /api/v1/files/upload` (it 405s). The two
  real surfaces are:
  - `POST /api/v1/sandbox/upload` — multipart field **`files`** (repeatable);
    the collection comes from the bearer token. Returns
    `{"upload_id", "file_paths": [...], "count"}`; those storage paths go in
    `jetty.file_paths`. Individual filenames are flattened to their basename —
    a **zip** is the only shape that preserves a directory tree (auto-extracted
    under `/app/assets/` with member paths intact).
  - `POST /api/v1/files` — OpenAI-style Files API returning an opaque
    `file-...` id for `jetty.files`. A `file-...` id placed in
    `jetty.file_paths` is silently dropped, so the adapter never uses this
    surface.
- Uploaded files mount under `/app/assets/` inside the sandbox, so the
  agent-visible path of every bundled file is knowable at export time.
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
- Files written under `/app/results/` persist as artifacts. Each is stored
  under a flattened name `<trajectory_id>.<step>.<NNNN>.<dir--parts--stem.ext>`
  (slashes become `--`, dots in the stem become `-`), listed with its storage
  path in the runbook step's `outputs.results_files[]`, and downloadable via
  `GET /api/v1/file/{storage_path}`.
- `results_dir` should default to `/app/results` and be passed through `jetty.template_variables`.
- Status polling uses `GET /api/v1/db/trajectory/{collection}/{task}/{trajectory_id}`
  (DB-indexed status/metadata; the row can lag the submission by a few seconds,
  so a 404 while waiting means queued). Step outputs — `results_files`,
  `primary_files`, and the `usage` block (`prompt_tokens`, `completion_tokens`,
  `total_tokens`, `cost_usd`, `duration_seconds`, `api_calls`, cache token
  counts) — come from the storage detail route
  `GET /api/v1/trajectory/{collection}/{task}/{trajectory_id}`.
- The production status set is `pending`, `running`, `completed`, `failed`,
  `cancelled`, plus administrative `archived`; terminal statuses are
  `completed` / `failed` / `cancelled`. There is no server-side `timeout`
  status — an exceeded agent budget surfaces as `failed`, and the sync HTTP
  wait returns 202 `running` while the workflow keeps going.
- The trajectory id arrives as `jetty_metadata.trajectory_id` on HTTP 200, and
  on HTTP 202 as `jetty_metadata.workflow_id` shaped
  `<collection>-<task>--<trajectory_id>` — the trajectory routes key on the
  bare suffix after the final `--`.
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
      "timeout_hint": 60,
      "template_variables": {
        "results_dir": "/app/results",
        "task_json": "/app/assets/tasks/good-pr-pos-security-meaningless-test-with-skill-1.json"
      },
      "file_paths": [
        "upload://good-pr-pos-security-meaningless-test-with-skill-1/bundle/zip"
      ]
    }
  },
  "upload_plan": {
    "bundle": {
      "placeholder": "upload://good-pr-pos-security-meaningless-test-with-skill-1/bundle/zip",
      "archive_name": "good-pr-pos-security-meaningless-test-with-skill-1.zip"
    },
    "files": [
      {"local_path": "/abs/repo/evals/fixtures/security-pr/diff.patch", "remote_path_hint": "fixtures/diff.patch", "sandbox_path": "/app/assets/fixtures/diff.patch", "role": "fixture", "private": false}
    ]
  }
}
```

The whole upload plan ships as **one zip** whose member names are the items'
`remote_path_hint`s: `/api/v1/sandbox/upload` flattens individual filenames to
their basename, so a zip (auto-extracted under `/app/assets/` with member
paths preserved) is the only shape that keeps skills/fixtures/tasks trees
intact. Model-visible references (`template_variables.task_json`, the task
JSON's `input_files`/`skill_files`) are therefore deterministic
`/app/assets/...` paths baked at export time; the single `file_paths`
placeholder is the only run-time substitution, replaced by the zip's storage
path after upload.

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

1. Zip `upload_plan.files` (member names = `remote_path_hint`) and upload the
   bundle to `/api/v1/sandbox/upload` (multipart field `files`).
2. Replace the single bundle placeholder in `jetty.file_paths` with the
   returned storage path.
3. Submit `jetty_request` to `/v1/chat/completions` and extract the trajectory
   id from `jetty_metadata` (`trajectory_id` on 200; on 202 the bare suffix of
   `workflow_id`). Only a documented 429 rejection returns to the automatically
   submit-ready state. Every other exception or unproven HTTP response becomes
   `submission_unknown`; a blind resubmit could double the sandbox spend.
4. Require file output, resolve all journal aliases to one canonical path, and
   take one exclusive OS lock before loading it. Reject hard-linked journals
   and symlinked/non-regular lock files. Atomically persist the uploaded bundle receipt, then mark the
   attempt as `submitting` before the non-idempotent POST. Persist the trajectory
   ID and response immediately after validating the acknowledgement. A restart
   with an acknowledged ID resumes it; a restart while the acknowledgement was
   pending becomes `submission_unknown` and cannot submit again without the
   explicit `--resubmit-unknown` operator override. The OS releases the lock
   when the owner exits or crashes, so overlapping invocations cannot both
   cross the submit boundary.
5. Poll `/api/v1/db/trajectory/{collection}/{task}/{trajectory_id}` until
   terminal status, treating early 404s as queued (the DB row can lag the
   submission). A local polling deadline is not a provider terminal state: the
   attempt remains acknowledged and a later invocation polls the same ID. Its
   output remains nonterminal, exits nonzero, and cannot be imported or graded.
6. On success, fetch the storage detail
   (`GET /api/v1/trajectory/{collection}/{task}/{trajectory_id}`), download
   every `results_files[]` artifact via `GET /api/v1/file/{path}`, and inline
   them into the run record (text as `content`, binary as base64
   `content_b64`) so the import step stays local and network-free.
7. Atomically checkpoint terminal status and downloaded artifacts, then
   atomically republish the complete ready prefix of the JSONL result and mark
   each record committed. Before the first replacement, reconstruct every
   downloaded or committed result from the journal, so another interruption
   cannot erase or truncate prior commits. An empty input still clears stale
   output. A restart rebuilds results without remote calls.
8. Retry bounded transient 429/5xx failures on the idempotent calls
   (upload, poll, fetch, download).

The journal identity includes the full attested task contract and digest plus
collection, task, and model. Submit and poll responses are projected onto the
minimal validated receipt needed for resumption; provider debug, environment,
input, and credential-bearing fields are never journaled. Jetty does not currently accept a server-side
idempotency key on this route, so the harness reports the ambiguous-response
window instead of claiming exactly-once execution; `--resubmit-unknown` may
create a duplicate paid attempt. Atomic replacement is process-crash-safe on
supported platforms; directory sync is best-effort where the OS does not expose
directory `fsync`.

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
3. If `task_json.skill_files` is non-empty, read and follow the mounted skill files.
4. If `task_json.skill_files` is empty, do not use a skill. No skill files are mounted.
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
`tests/test_jetty_contracts.py`. Captured production shapes live in `tests/fixtures/jetty/`
and are pinned by `tests/test_jetty_live_contract.py` (upload endpoint + multipart field +
User-Agent, both submit shapes, db-record lifecycles, storage-detail artifact/usage
normalization).

Security checks prove executor payloads omit answer keys/judge rubrics and provider credentials,
`without_skill` uploads no skill files, hidden prompt placeholders remain non-executable, unsafe or
duplicate imported destinations fail before writes, and unknown/conflicting statuses fail closed.

Phase 5 — opt-in live validation — shipped as `tests/test_smoke_jetty.py`, gated by
`RUN_JETTY_SMOKE=1` + `JETTY_API_TOKEN` (never in default CI). It runs one fixture-free
tune case, one fixture-backed tune case, and a cheap server-side failure path
(`jetty.timeout_sec: 15`) through export → run → import → benchmark, and first passed
against production on 2026-07-17.

## Live-token answers (2026-07-17)

The six open questions, answered against production with a real token; the
captured shapes live in `tests/fixtures/jetty/`:

1. **Upload response**: `/api/v1/files/upload` does not exist (405). The
   adapter uses `POST /api/v1/sandbox/upload` (multipart field `files`), which
   returns `{"upload_id", "file_paths": ["<collection>/_sandbox_uploads/<id>/<name>"], "count"}`.
2. **Artifact listing/download**: storage detail
   `GET /api/v1/trajectory/{c}/{t}/{id}` lists
   `steps.run.outputs.results_files[] = {path, content_type, extension}` (plus
   `files[]`, `primary_files[]`); each `path` is a flattened storage key
   downloadable as raw bytes via `GET /api/v1/file/{path}`. There is also a
   whole-trajectory zip at `GET /api/v1/trajectory/{c}/{t}/{id}/download`
   (same flattened names inside).
3. **Trajectory id field**: non-streaming 200 → `jetty_metadata.trajectory_id`
   (also `id: "chatcmpl-<id>"`); 202 → `jetty_metadata.workflow_id` shaped
   `<collection>-<task>--<id>`, keyed by its bare suffix.
4. **Terminal status set**: `completed`, `failed`, `cancelled` (non-terminal:
   `pending`, `running`; administrative: `archived`). No server-side
   `timeout` status — an exceeded `jetty.timeout_sec` surfaces as `failed`.
5. **Directory trees**: archives. Individual sandbox uploads flatten to
   basenames; a zip in `file_paths` is auto-extracted under `/app/assets/`
   with member paths preserved.
6. **`use_trial_keys`**: accepted for every account and effectively advisory —
   server-side trial-key injection is automatic, gated on the collection
   having an active trial budget and the run lacking a user-supplied provider
   key, independent of the flag.

One operational surprise worth restating: Cloudflare fronts the API and bans
urllib's default `Python-urllib` User-Agent (`403`, error code 1010), so the
client sends `User-Agent: skill-eval-harness` on every request.

## Simplicity bias

Implement only the REST path first:

- no MCP dependency;
- no custom benchmark zip upload;
- no streaming;
- no custom images;
- no Jetty judge export until execution/import is working;
- no full repo mounting until file upload behavior is proven live.

The first useful integration is: export one task, run it on Jetty, import `output.md`, and grade it locally.
