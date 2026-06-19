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
own projects (`anti-slop-writing`, `slide-maker`, `xampler`, `pythonbyexample`). Full
design in [`docs/eval-framework-roadmap-spec.md`](docs/eval-framework-roadmap-spec.md).

**Moat — do not dilute:** causal lift, `tune`/`holdout`/`holdback` splits, leakage lint,
ablations, and saturation/no-lift flags are unique to this harness. None of the surveyed
frameworks have them, and notably *none* have first-class multi-model comparison either.
Every item below slots around these, not over them.

## Bucket 1 — near drop-in (fits the existing contract)

- [ ] Ship built-in judge presets (`factuality`, `tool_call`, `structured_output`) that return the existing `{passed, score, rationale}` shape with numeric thresholds, layered on `--judge-cmd`. (`tool_call` / `structured_output` are largely deterministic; reuse `command_order` / `json_field_equals` substrate.)
- [ ] Add a GitHub Actions reporter (PR annotations + Check Runs) and/or JUnit XML emitter over `benchmark.json`.
- [ ] Add a centralized judge config slot and a `validate` check that enforces "judge model ≠ model under test."
- [ ] Add a local/deterministic `similarity` scorer (e.g. levenshtein/normalized-ratio) with a threshold. (Embedding-backed variant is Bucket 4.)
- [ ] Add a `golden_output` assertion: normalize `output.md` and compare to a reference file (byte or normalized-text equality). Drawn from `adewale/pythonbyexample` and `adewale/xampler`, where an example *is* a deterministic eval case (input + expected output + check).
- [ ] Add oracle-strength labeling + a hygiene check: let an assertion/fixture declare its oracle tier (strong deterministic / marked demo-seam / opt-in live, per `adewale/xampler`'s "best no-lies / deliberate demo seams / remote"), and report how much of a case's pass rate rests on strong vs. weak oracles. Extends leakage lint from prompts to oracles.
- [x] Author an opinionated workflow/quickstart guide (`docs/authoring-evals.md`).
- [ ] Fold the workflow guidance into `validate`/`audit-manifest` hints where enforceable.

## Bucket 2 — natural extension of an existing subsystem

- [ ] **Multi-model fan-out (priority).** Accept a model list in `prepare`/`run-codex`; `benchmark` groups by model and computes per-model lift. Per-run `model` already lands in `metadata.json`; the fan-out parallels variant fan-out.
- [ ] Graded scoring + gate/soft severity + statistical lift on assertions (`gate` / `soft` / `atLeast`), with anchored `graded_dimensions`, `dynamic_rubric`, and a paired-bootstrap lift gate. Reference impls: `adewale/anti-slop-writing` (graded dimensions, `score_delta.py`) and `adewale/slide-maker` (held-out rubric). Includes a `structurally-pass-but-forgettable` flag (objective saturated + graded low).
- [ ] Held-out rubric discipline: withhold grading criteria from generation, not just prompts, and score with a subagent judge — as `adewale/slide-maker` does ("criteria deliberately absent from generation rules"). Extends `holdback` from prompts to rubrics. `prepare` already omits `review_rubric` from generation payloads; this makes it a first-class split-level rule.
- [ ] Tool replay: record tool inputs+outputs (keyed/sanitized/versioned) so a live re-run is deterministic and free of external-dep cost. Runner-side; pairs with the subagent runner. (Distinct from grading, which already replays from disk.)
- [ ] Adopt OpenTelemetry GenAI semantic conventions as the `events.json`/`metrics.json` normalization target.
- [ ] Dataset abstraction: fan one case template over a row set; optionally persist/generate datasets.
- [ ] Cross-run trend tracking (lift / saturation / token-overhead drift over time).
- [ ] Built-in subagent runner (Claude Code / Agent SDK Task) that emits the run-output contract; analogue of vitest-evals' OpenAI-Agents harness.
- [ ] Interactive served report (feedback capture + richer artifact rendering: image/pdf/xlsx) over static `render-viewer`. eval-viewer's `generate_review.py` is a blueprint.
- [ ] Iteration-over-time workflow: `iteration-N/` dirs + `--previous-workspace` diff + `feedback.json` capture.
- [ ] "Living eval" loop: act on saturation flags by proposing harder cases (generation step opt-in/external, never in core grading).

## Bucket 3 — bigger lift (new axis or core-contract change)

- [ ] Multi-turn / scripted cases (changes the single-`output.md` run contract, runners, and grading).
- [ ] Per-model lift/pairing analysis and reporting (the heavier half of multi-model, beyond simple fan-out).
- [ ] No-code template/registry eval definitions (overlaps the dataset abstraction; broader reframing of manifest authoring).

## Bucket 4 — adopt with care (design-principle tension)

Core grading is local, deterministic, and never calls a model; the harness does not pick a
model. Keep anything that needs a model behind opt-in commands / external `--judge-cmd`,
never in the core grade path.

- [ ] Embedding-backed `similarity` scorer (needs a model/embeddings) — keep optional like `script`.
- [ ] Auto-generation of harder cases for the living-eval loop (needs a model) — separate opt-in command; generated cases reviewed before entering a manifest.

## Punted — questionable value

- [ ] **Ship the harness as an agent-authoring skill** (à la vitest-evals' `npx skills add ...`).
      Punted as **questionable value**: it is meta/self-referential, overlaps the new
      `docs/authoring-evals.md` guide, adds a packaging/maintenance surface, and serves a
      narrow audience (agents authoring manifests) versus shipping real grading
      capability. Revisit only if external skill authors actually adopt the harness and ask
      for an agent-guided authoring path.
