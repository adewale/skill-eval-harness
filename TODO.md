# TODO — Jetty compatibility

Detailed spec: [`docs/jetty-support-spec.md`](docs/jetty-support-spec.md).

Jetty is an OpenAI-compatible workflow/agent platform with `POST https://flows-api.jetty.io/v1/chat/completions`, trajectory recording, sandboxed runbook execution, agent runtimes, workflow steps, and `simple_judge` evaluation. The spec is grounded in Jetty public docs plus `github.com/jettyio/jettyio-skills` (`skills/jetty/SKILL.md`, `skills/jetty/references/agents-and-models.md`, and `skills/create-runbook/SKILL.md`). To keep the first implementation simple, target REST runbook mode first: `export-jetty`, `run-jetty`, `import-jetty-results`; no MCP dependency, no streaming, no custom images, and no Jetty judge export until execution/import works.

## API adapter

- [ ] Add `export-jetty` command that converts prepared task rows into Jetty chat-completion payloads.
- [ ] Support Jetty auth via `JETTY_API_TOKEN` / `JETTY_API_KEY`.
- [ ] Use base URL `https://flows-api.jetty.io` by default with override env/flag.
- [ ] Emit OpenAI-compatible fields: `model`, `messages`, `stream`.
- [ ] Emit Jetty extension block with `runbook`, `collection`, `task`, `agent`, `model_provider`, `snapshot`, `template_variables`, `use_trial_keys`, and `file_paths` where applicable.
- [ ] Preserve harness IDs in Jetty metadata: `skill_name`, `case_id`, `variant`, `run_number`, `split`, `run_dir`.

## Runbook execution

- [ ] Add a canonical Jetty runbook template for one harness task.
- [ ] The runbook must write `/app/results/output.md`.
- [ ] The runbook must write `/app/results/metadata.json`.
- [ ] The runbook should copy artifacts to `/app/results/outputs/`.
- [ ] Support practical Jetty agent runtimes from `jettyio-skills`: `claude-code`, `opencode`, `codex`, and `gemini-cli`.
- [ ] Add per-variant runbook instructions for `with_skill`, `without_skill`, `old_skill`, and `ablation:<id>`.

## Skill and fixture mounting

- [ ] Upload/mount skill files for `with_skill` runs.
- [ ] Hide or omit skill files for `without_skill` runs; do not rely only on prose instructions when file controls are possible.
- [ ] Upload old skill snapshots for `old_skill` runs.
- [ ] Materialize ablated skill variants or generate an explicit ablation patch/instruction for `ablation:<id>`.
- [ ] Upload private `prompt_ref` content only to executor jobs, not to public artifacts.
- [ ] Upload fixture repos/files referenced by eval cases.

## Running and polling

- [ ] Add `run-jetty` command that submits payloads and polls trajectory status.
- [ ] Handle streaming and non-streaming Jetty responses.
- [ ] Persist Jetty trajectory IDs immediately.
- [ ] Retry transient API failures with bounded backoff.
- [ ] Support concurrency limits and rate-limit handling.
- [ ] Support dry-run payload export without submission.

## Importing results

- [ ] Add `import-jetty-results` command.
- [ ] Download final assistant output into `runs/<case_id>/<variant>/run-<n>/output.md`.
- [ ] Download generated artifacts into `outputs/`.
- [ ] Normalize Jetty metrics into `metadata.json`: `elapsed_ms`, `input_tokens`, `output_tokens`, `total_tokens`, `model`, `total_tool_calls`, `errors_encountered`.
- [ ] Preserve raw Jetty metadata: `jetty_trajectory_id`, `jetty_collection`, `jetty_task`, `jetty_agent`, `trace_url`, `jetty_raw`.
- [ ] Keep local deterministic grading unchanged after import.

## Jetty evaluation integration

- [ ] Export qualitative judge tasks to Jetty workflows using `simple_judge` where useful.
- [ ] Import Jetty judge results as `judge-results.jsonl` keyed by `judge_task_id`.
- [ ] Support `local_only`, `jetty_only`, and `merge` grader modes.
- [ ] Map Jetty score scales to harness `passed`, `score`, `threshold`, and `evidence` fields.

## Manifest extensions

- [ ] Add optional `jetty` block to manifests.
- [ ] Suggested fields: `collection`, `task_prefix`, `agent`, `model`, `model_provider`, `snapshot`, `use_trial_keys`, `grader_mode`, `skill_mount_strategy`.
- [ ] Allow per-variant Jetty overrides under `jetty.variants.<variant>`.
- [ ] Validate Jetty fields without making them required for non-Jetty users.

## CI and tests

- [ ] Add golden tests for `export-jetty` payload shape.
- [ ] Add mocked Jetty trajectory fixture.
- [ ] Add importer round-trip test: Jetty result JSON -> harness run layout -> `benchmark`.
- [ ] Add hidden-prompt safety test proving answer keys are not uploaded in executor payloads.
- [ ] Add README quick start for Jetty once API behavior is verified with a real account.

## Open questions to verify against current Jetty docs/API

- [ ] Exact JSON response shape from `/api/v1/files/upload`.
- [ ] Exact artifact listing/download shape from trajectory details.
- [ ] Exact chat-completion response field containing `trajectory_id` in non-streaming runbook mode.
- [ ] Full terminal status set in production.
- [ ] Whether directory trees should be uploaded as individual files or archives.
- [ ] Whether `use_trial_keys` is available to all relevant accounts or only trial collections.
