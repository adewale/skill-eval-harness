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

Jetty runbooks emit a standardized machine-readable `validation_report.json` per trajectory
(`jettyio/jettyio-skills`, `skills/create-runbook/SKILL.md`). Rubric evaluation scores 3-7
dimensions on a 1-5 scale; programmatic evaluation returns `PASS` / `PARTIAL` / `FAIL`. The
items below map that report onto the harness judge-result row `{judge_task_id, passed, score,
threshold, evidence}` (`load_judge_results:1721`, merged in `grade_case_variant:1877`).

- [ ] Export qualitative judge tasks to Jetty workflows using `simple_judge` where useful.
      Carry `judge_task_id` (`case::variant::run-n::assertion`) into the Jetty task so the
      imported result can be keyed back without guessing. Manifest `judge`/`rubric` dimensions
      become the runbook's rubric dimensions; a manifest `threshold` becomes the exit gate.
- [ ] Import Jetty judge results as `judge-results.jsonl` keyed by `judge_task_id`, reading
      from each trajectory's `validation_report.json`.
- [ ] Map Jetty score scales to harness `passed`, `score`, `threshold`, `evidence`:
  - [ ] Rubric (1-5): `score` = mean (or min) of the 1-5 dimension scores; `threshold` from the
        manifest assertion (default `>= 4`); `passed` = `score >= threshold`. Keep per-dimension
        scores in `evidence`.
  - [ ] Programmatic: `PASS` -> `passed: true, score: 1.0`; `FAIL` -> `passed: false, score: 0.0`;
        `PARTIAL` -> `score: 0.5`, with `passed` decided by `threshold` (soft by default once the
        2.2 severity tier lands).
  - [ ] `evidence` = the report's rationale/notes; preserve the raw Jetty score block alongside it.
- [ ] Support `local_only`, `jetty_only`, and `merge` grader modes for combining locally-run and
      Jetty-run judge results before benchmarking.
- [ ] Add a round-trip test: manifest judge assertion -> exported Jetty rubric -> mocked
      `validation_report.json` -> imported `judge-results.jsonl` -> `benchmark` merge. No live API.

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

---

# Eval-framework parity & ideas

Backlog distilled from a comparison against eve.dev, Sentry vitest-evals, viteval,
openai/evals, Anthropic skill-creator + eval-viewer, the "Your evals will break" essay,
the OpenAI eval-skills blog, `jettyio/jettyio-skills`, and a deeper look at the author's
own projects (`anti-slop-writing`, `slide-maker`, `xampler`, `pythonbyexample`).

Design lives in [`docs/eval-framework-roadmap-spec.md`](docs/eval-framework-roadmap-spec.md),
keyed to the same number (`1.1`, `2.2`, `CF.1`, …). This file tracks status only; open the
spec for each item's goal, abstractions, design, and tests. Items with no spec section are
marked *(TODO-native)*.

**Moat — do not dilute:** causal lift, `tune`/`holdout`/`holdback` splits, leakage lint,
ablations, and saturation/no-lift flags are unique to this harness. None of the surveyed
frameworks have them, and notably *none* have first-class multi-model comparison either.
Every item below slots around these, not over them.

## Confidence floor (do first) — make a reported lift believable

These are tests of the harness, not a new eval suite; sequence them before the buckets.

- [ ] CF.1 Detector meta-fixtures (keystone) — paired should-fire/should-pass per detector
- [ ] CF.2 One cross-runner baseline-isolation invariant
- [ ] CF.3 Re-grade idempotence
- [ ] CF.4 Guard: no model / no network in the core grade path

## Bucket 1 — near drop-in (fits the existing contract)

- [ ] 1.1 Built-in judge presets (`factuality`, `tool_call`, `structured_output`)
- [ ] 1.2 GitHub Actions reporter / JUnit XML over `benchmark.json`
- [ ] 1.3 Judge config slot + "judge model ≠ model under test" guard
- [ ] 1.4 Local/deterministic `similarity` scorer
- [x] 1.5 Workflow/quickstart guide (`docs/authoring-evals.md`)
- [ ] 1.5 Fold the guide's rules into `validate`/`audit-manifest` hints
- [ ] 1.6 `golden_output` assertion (reference-file equality)
- [ ] 1.7 Oracle-strength labeling + hygiene check
- [ ] 1.8 Graded script oracles (`{score, max_score}` from stdout)
- [ ] 1.9 Staleness / pruning hygiene (depends on 2.6)
- [ ] Exapt a reusable detector library from real usage *(TODO-native; revisit end of 2026)*. Mine the GitHub fork graph and issue tracker, then harvest the deterministic checks forkers actually hand-rolled — a detector earns its place when ≥2 independent skills re-implement it (`anti-slop-writing`, `swiss-poster-skill`, `guardrails-skill` already do by hand). Each ships with the CF.1 fire/pass fixtures before it is trusted.

## Bucket 2 — natural extension of an existing subsystem

- [ ] 2.1 Multi-model fan-out (priority)
- [ ] 2.2 Graded scoring + gate/soft/critical severity + statistical lift
- [ ] 2.3 Tool replay (record/replay tool I/O for deterministic re-runs)
- [ ] 2.4 OpenTelemetry GenAI normalization target
- [ ] 2.5 Dataset abstraction (one case template × row set)
- [ ] 2.6 Cross-run trend tracking
- [ ] 2.7 Built-in subagent runner (Claude Code / Agent SDK)
- [ ] 2.7b Held-out rubric discipline (depends on 2.2, 2.7)
- [ ] 2.8 Interactive served report + richer artifacts
- [ ] 2.9 Iteration-over-time workflow
- [ ] 2.10 "Living eval" loop on saturation

## Bucket 3 — bigger lift (new axis or core-contract change)

- [ ] 3.1 Multi-turn / scripted cases
- [ ] 3.2 Per-model lift / pairing analysis
- [ ] 3.3 No-code template/registry definitions

## Bucket 4 — adopt with care (needs a model; keep out of the core grade path)

- [ ] 4.1 Embedding-backed `similarity` scorer
- [ ] 4.2 Auto-generation of harder cases (living-eval loop)

## Punted — questionable value

- [ ] Ship the harness as an agent-authoring skill *(TODO-native)*. Meta/self-referential, overlaps `docs/authoring-evals.md`, adds a packaging surface, narrow audience. Revisit only if external authors adopt the harness and ask for an agent-guided authoring path.
