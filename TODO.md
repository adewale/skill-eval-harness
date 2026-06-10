# TODO — Jetty compatibility

Detailed spec: [`docs/jetty-support-spec.md`](docs/jetty-support-spec.md).

Jetty is an OpenAI-compatible workflow/agent platform with `POST https://flows-api.jetty.io/v1/chat/completions`, trajectory recording, sandboxed runbook execution, agent runtimes, workflow steps, and `simple_judge` evaluation. The spec is grounded in Jetty public docs plus `github.com/jettyio/jettyio-skills` (`skills/jetty/SKILL.md`, `skills/jetty/references/agents-and-models.md`, and `skills/create-runbook/SKILL.md`). To keep the first implementation simple, target REST runbook mode first: `export-jetty`, `run-jetty`, `import-jetty-results`; no MCP dependency, no streaming, no custom images, and no Jetty judge export until execution/import works.

## API adapter

- [x] Add `export-jetty` command that converts prepared task rows into Jetty chat-completion payloads.
- [x] Support Jetty auth via `JETTY_API_TOKEN` for live `run-jetty`.
- [x] Use base URL `https://flows-api.jetty.io` by default with `JETTY_BASE_URL` override.
- [x] Emit OpenAI-compatible fields: `model`, `messages`, `stream`.
- [x] Emit Jetty extension block with `runbook`, `collection`, `task`, `agent`, `model_provider`, `snapshot`, `template_variables`, `use_trial_keys`, and `file_paths` where applicable.
- [x] Preserve harness IDs in Jetty records: `skill_name`, `case_id`, `variant`, `run_number`, `split`, `run_dir`.

## Runbook execution

- [x] Add a canonical Jetty runbook template for one harness task.
- [x] The runbook must write `/app/results/output.md`.
- [x] The runbook must write `/app/results/metadata.json`.
- [x] The runbook should copy artifacts to `/app/results/outputs/`.
- [x] Support practical Jetty agent runtimes from `jettyio-skills`: `claude-code`, `opencode`, `codex`, and `gemini-cli`.
- [x] Add per-variant runbook instructions for `with_skill`, `without_skill`, `old_skill`, and `ablation:<id>`.

## Skill and fixture mounting

- [x] Upload/mount skill files for `with_skill` runs.
- [x] Hide or omit skill files for `without_skill` runs; do not rely only on prose instructions when file controls are possible.
- [x] Upload old skill snapshots for `old_skill` runs.
- [x] Generate an explicit instruction-simulated ablation marker for `ablation:<id>`.
- [x] Put prompt content only in private generated `task.json`, not public artifacts.
- [x] Upload fixture repos/files referenced by eval cases.
- [ ] Materialize true ablated skill files instead of instruction-simulated ablations.

## Running and polling

- [x] Add `run-jetty` command that submits payloads and polls trajectory status.
- [x] Handle non-streaming Jetty responses.
- [x] Persist Jetty trajectory IDs in run records as soon as submit returns them.
- [x] Retry transient 429/5xx API failures with bounded backoff.
- [ ] Support concurrency limits and rate-limit handling.
- [x] Support dry-run payload loading without submission.
- [ ] Handle streaming Jetty responses.

## Importing results

- [x] Add `import-jetty-results` command.
- [x] Import final assistant output into `runs/<case_id>/<variant>/run-<n>/output.md` when repeated, or the existing one-run layout.
- [x] Import generated artifacts into `outputs/` when present in trajectory records.
- [x] Normalize Jetty metrics into `metadata.json`: `elapsed_ms`, `input_tokens`, `output_tokens`, `total_tokens`, `model`, `total_tool_calls`, `errors_encountered`.
- [x] Preserve raw Jetty metadata: `jetty_trajectory_id`, `jetty_collection`, `jetty_task`, `jetty_agent`, `trace_url`, `jetty_raw`.
- [x] Keep local deterministic grading unchanged after import.

## Jetty evaluation integration

- [ ] Export qualitative judge tasks to Jetty workflows using `simple_judge` where useful.
- [ ] Import Jetty judge results as `judge-results.jsonl` keyed by `judge_task_id`.
- [ ] Support `local_only`, `jetty_only`, and `merge` grader modes.
- [ ] Map Jetty score scales to harness `passed`, `score`, `threshold`, and `evidence` fields.

## Manifest extensions

- [x] Read optional `jetty` block from manifests.
- [x] Suggested fields: `collection`, `task_prefix`, `agent`, `model`, `model_provider`, `snapshot`, `use_trial_keys`, `grader_mode`, `skill_mount_strategy`.
- [ ] Allow per-variant Jetty overrides under `jetty.variants.<variant>`.
- [ ] Validate Jetty fields without making them required for non-Jetty users.

## CI and tests

- [x] Add golden-style tests for `export-jetty` payload shape.
- [x] Add mocked Jetty trajectory fixture coverage inline in tests.
- [x] Add importer round-trip test: Jetty result JSON -> harness run layout -> `benchmark`.
- [x] Add hidden-prompt/answer-key safety test proving answer keys are not uploaded in executor payloads.
- [x] Add README quick start for the current mocked/dry-run Jetty path.
- [ ] Add README live-smoke notes after API behavior is verified with a real account.

## Open questions to verify against current Jetty docs/API

- [ ] Exact JSON response shape from `/api/v1/files/upload`.
- [ ] Exact artifact listing/download shape from trajectory details.
- [ ] Exact chat-completion response field containing `trajectory_id` in non-streaming runbook mode.
- [ ] Full terminal status set in production.
- [ ] Whether directory trees should be uploaded as individual files or archives.
- [ ] Whether `use_trial_keys` is available to all relevant accounts or only trial collections.
