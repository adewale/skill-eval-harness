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
- [x] Materialize true ablated skill files (removal-only engine, `materialize-ablations` CLI, validation, and gates) — spec: [`docs/skill-ablation-spec.md`](docs/skill-ablation-spec.md).
- [x] Wire materialized ablation trees into the executors: Pi smoke mounts the altered tree, Pi trigger `--ablation` mounts a materialized skill, Jetty `export-jetty` uploads the tree recursively (relative paths preserved). Population-based case routing, run provenance, and an `ablation_regressions` report distinguishing "score regressed" from assertion-level "expected regression confirmed" all landed.
- [ ] Support component **swap/substitution**, not just removal: generalize the ablation mechanisms with `replace_with`/`set`, add whole-file swap for `reference`/`script`/`asset`, and report A-B deltas between two live variants. Recommended as a sibling `swap:<id>` variant (keeping `ablation:<id>` removal-only); framing decision and design in [`docs/skill-ablation-spec.md`](docs/skill-ablation-spec.md). Builds on materialized ablations above; unlocks the `model`/`effort` confound control and degrees-of-freedom experiments.

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
threshold, evidence}` (`load_judge_results:4067`, merged in `grade_case_variant:5075`).

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
        `PARTIAL` -> `score: 0.5`, with `passed` decided by `threshold` (soft by default under the
        2.2 severity tier, which has landed).
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
marked *(TODO-native)*. The roadmap is implemented: migration tooling included
(`migrate` + [`docs/migrating-evals.md`](docs/migrating-evals.md)); tests live in
`tests/test_confidence_floor.py` and `tests/test_roadmap_features.py`. The two TODO-native
items below stay open by design.

**Moat — do not dilute:** causal lift, `tune`/`holdout`/`holdback` splits, leakage lint,
ablations, and saturation/no-lift flags are unique to this harness. None of the surveyed
frameworks have them, and notably *none* have first-class multi-model comparison either.
Every item below slots around these, not over them.

## Confidence floor (do first) — make a reported lift believable

These are tests of the harness, not a new eval suite; sequence them before the buckets.

- [x] CF.1 Detector meta-fixtures (keystone) — paired should-fire/should-pass per detector (`tests/fixtures/detectors/`, meta-test = registration contract)
- [x] CF.2 One cross-runner baseline-isolation invariant (`WORKSPACE_BUILDERS` registry: codex/claude/jetty/subagent + pi smoke)
- [x] CF.3 Re-grade idempotence (byte-identical report modulo `generated_at`)
- [x] CF.4 Guard: no model / no network in the core grade path (subprocess+urllib patched to raise)

## Bucket 1 — near drop-in (fits the existing contract)

- [x] 1.1 Built-in judge presets (`factuality` canned rubric; `tool_call`, `structured_output` deterministic types)
- [x] 1.2 GitHub Actions reporter / JUnit XML over `benchmark.json` (`report --format junit|github`)
- [x] 1.3 Judge config slot + "judge model ≠ model under test" guard (manifest `judge.model`, audit finding, `--strict-judge`)
- [x] 1.4 Local/deterministic `similarity` scorer (difflib ratio + threshold, scored, soft by default)
- [x] 1.5 Workflow/quickstart guide (`docs/authoring-evals.md`)
- [x] 1.5b Fold the guide's rules into `validate`/`audit-manifest` hints (guide pointers on leakage findings and fixture recommendations)
- [x] 1.6 `golden_output` assertion (reference-file equality, explicit normalization, diff evidence)
- [x] 1.7 Oracle-strength labeling + hygiene check (strong/demo/live tiers, per-case strong-share, `weak-oracle-only` audit)
- [x] 1.8 Graded script oracles (`{score, max_score}` from stdout, normalized into the graded channel; exit code still decides pass)
- [x] 1.9 Staleness / pruning hygiene (`trend` prune candidates over history; suggests, never deletes)
- [ ] Exapt a reusable detector library from real usage *(TODO-native; revisit end of 2026)*. Mine the GitHub fork graph and issue tracker, then harvest the deterministic checks forkers actually hand-rolled — a detector earns its place when ≥2 independent skills re-implement it (`anti-slop-writing`, `swiss-poster-skill`, `guardrails-skill` already do by hand). Each ships with the CF.1 fire/pass fixtures before it is trusted.

## Bucket 2 — natural extension of an existing subsystem

- [x] 2.1 Multi-model fan-out (`prepare --models`, model run-dir segment, by_model report, per-(case, model) lift)
- [x] 2.2 Graded scoring + gate/soft/critical severity + statistical lift (veto, graded_dimensions, dynamic_rubric, reference floors, sign-flip significance, `--strict`)
- [x] 2.3 Tool replay (record/replay/strict/auto via `tool-replay.json`, hosted by the subagent runner)
- [x] 2.4 OpenTelemetry GenAI normalization target (otel attributes per event + usage block; events schema v2, v1 still grades)
- [x] 2.5 Dataset abstraction (case `template` × `datasets` rows, materialized early, lint per materialized case)
- [x] 2.6 Cross-run trend tracking (`trend`: append-only history, series, diffs, prevalence×severity failure ranking)
- [x] 2.7 Built-in subagent runner (`run-subagent`: injectable agent seam, contract writer, CF.2-registered)
- [x] 2.7b Held-out rubric discipline (`held-out-rubric-leak` audit finding; `qualitative_by_visibility` report split)
- [x] 2.8 Interactive served report + richer artifacts (`render-viewer --serve`, feedback.json, image/pdf/xlsx encoders)
- [x] 2.9 Iteration-over-time workflow (iteration-N helpers, `--previous-workspace` diff)
- [x] 2.10 "Living eval" loop on saturation (`suggest-cases`; generation opt-in via `--generate-cmd`, never edits a manifest)

## Bucket 3 — bigger lift (new axis or core-contract change)

- [x] 3.1 Multi-turn / scripted cases (case `turns`, turn-indexed transcript, per-turn grading + aggregate; single-shot unchanged)
- [x] 3.2 Per-model lift / pairing analysis (`model_analysis` ranking + lift losers; slice lift concentration)
- [x] 3.3 No-code template/registry definitions (YAML manifests + `dataset_files` JSONL, compiled before validation)

## Bucket 4 — adopt with care (needs a model; keep out of the core grade path)

- [x] 4.1 Embedding-backed `similarity` scorer (`mode: embedding` behind opt-in `--embed-cmd`; fails closed without it)
- [x] 4.2 Auto-generation of harder cases (shipped inside 2.10: `suggest-cases --generate-cmd`, mocked in tests, no manifest mutation)

## Post-#22 follow-ups (trustworthy-measurement gaps from the awesome-evals assessment)

- [x] Significance gate on the ablation confirmation (feature 1): `expected_regression_confirmed` requires a two-sided permutation test over the replicated per-run scores to clear p ≤ 0.05; an observed-but-underpowered drop is `INDETERMINATE`, not confirmed. Single-shot ablations can no longer confirm.
- [x] `judge-alignment` (feature 2): validate a judge against **human labels** — agreement, Cohen's kappa, precision/recall/F1 — the accuracy check `compare-judges` (judge-vs-judge sensitivity) does not make.
- [x] `reliability` report block (feature 5): unbiased **pass@k** and **pass^k** per (case, variant) from repeated runs, plus a pooled per-variant headline.
- [x] `tool_call` BFCL-style taxonomy (feature 6): `expected_no_call`, `required_calls` (subset), `call_set` (exact multiset).
- [x] `error-analysis` (feature 8): open-coding review queue + axial failure taxonomy over a `benchmark.json`, model-free.
- [x] **Contamination perimeter (output side)** — `contamination` command + pure detectors: output↔answer n-gram containment (`ngram_containment`), canary-GUID tripwire (case `canary`), and a `released_at`/`--model-cutoff` gate. Optional case fields, `--fail-on-contamination` CI gate, model-free.
- [x] **Judge robustness probes** — `judge-robustness` command: order-flip self-consistency (`flipped_judge_task`) + empty/master-key negative controls (`JUDGE_NEGATIVE_CONTROLS`) a robust judge must reject; `order_flip_consistency`/`control_leak_rate` summary, `--fail-on-findings` CI gate. Model-touching, opt-in behind `--judge-cmd`/`--judge-model`; kept out of the core grade path.

## Post-#24 external-review gaps (Pydantic / Anthropic / OpenAI / Agent-as-a-Judge)

Six gaps surfaced by an external-source review (see [`docs/academic-grounding.md`](docs/academic-grounding.md)),
deduped against the awesome-evals follow-ups above — none is a dupe; only G3 is adjacent to the
open "Judge robustness probes" item. Sequence:
**G6** · **G4 → G1 → G3** (judge path, serialize) · **G5 → G2** (grade path, serialize).

- [x] G6 Paired pass@k/pass^k lift — `reliability.paired_lift`: with−without delta on pass@k/pass^k, per case + pooled per shared k, sign-flip tested. The sliver of feature 5 (reliability) not merged.
- [x] G4 Schema-constrained judge output — `verdict_schema_for` + post-hoc `json_schema_errors` gate; `report` default (byte-identical), `--strict-judge-schema` / `judge.schema_enforcement` opt-in; `extract_json_object` kept as fallback.
- [x] G1 Run-dir / trajectory judge — `--judge-trajectory` feeds the judge normalized events/metrics + a denylisted artifact inventory (`judge_artifact_inventory` excludes grading.json / answer-key / rubric / reserved files); byte-identical when off. Tool-using follow-on landed: `--judge-explore` lets a native judge explore a SANITIZED copy of the run dir (`sanitized_run_copy` removes every oracle file by construction) with read-only tools (`JUDGE_EXPLORE_TOOLS`), so a filesystem-reading judge cannot read the answer key.
- [x] G3 Cross-judge consensus — `merge_cross_judge_rows` folds a ≥2-model panel into one verdict (majority/median, `agreement` block, ties → `unresolved`/`--quorum`); `--judge-panel`/manifest `judge.panel` via `effective_judge_models`; panel cost summed once; guard checks every member. Distinct from compare-judges/judge-alignment; 1-member short-circuits unchanged.
- [x] G5 Capability/regression intent — per-case `eval_intent`; regression guards route to `regression_guards_holding` (never a blocker), exempt from staleness/suggest, saturated/no-lift findings suppressed. Optional, defaults to `capability`.
- [x] G2 Assertion dependencies — `depends_on` (validated: shape/target/uniqueness/cycle, rejected in turns); a failed/skipped prerequisite SKIPS the dependent out of every denominator + the critical veto (skip, not zero); transitive + deferred-qualitative via a fixed-point post-pass. Byte-identical when unused. Inline judge-call suppression landed (skips a resolved-failed dependent without emitting a judge task / running a script).

## Punted — questionable value

- [ ] Ship the harness as an agent-authoring skill *(TODO-native)*. Meta/self-referential, overlaps `docs/authoring-evals.md`, adds a packaging surface, narrow audience. Revisit only if external authors adopt the harness and ask for an agent-guided authoring path.
