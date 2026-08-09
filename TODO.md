# TODO — Jetty compatibility

Detailed spec: [`docs/jetty-support-spec.md`](docs/jetty-support-spec.md).

Jetty is an OpenAI-compatible workflow/agent platform with `POST https://flows-api.jetty.io/v1/chat/completions`, trajectory recording, sandboxed runbook execution, agent runtimes, workflow steps, and `simple_judge` evaluation. The spec is grounded in Jetty public docs plus `github.com/jettyio/jettyio-skills` (`skills/jetty/SKILL.md`, `skills/jetty/references/agents-and-models.md`, and `skills/create-runbook/SKILL.md`). To keep the first implementation simple, target REST runbook mode first: `export-jetty`, `run-jetty`, `import-jetty-results`; no MCP dependency, no streaming, no custom images, and no Jetty judge export until execution/import works.

## Current status and production-readiness gap

Current support is an adapter scaffold, not production-proven Jetty evidence. The harness can export deterministic runbook payloads, dry-run them, submit/poll through a REST client, and import mocked trajectory-shaped results. The missing step is token-backed contract validation against real Jetty responses; until then the main risk is mock-reality drift.

- [x] Mark README/docs examples clearly: Jetty is optional and live response shapes remain unverified until the first token-backed smoke passes (README "optional; see the Jetty adapter" + `docs/commands.md` Jetty section opener).
- [ ] Capture redacted live responses as committed contract fixtures for file upload, chat-completion submit, trajectory poll, artifact list/download, failed/timeout status, and rate-limit response.
- [ ] Update `JettyClient`, `execute_jetty_payloads`, and `import_jetty_results` to consume the real response shapes, not only the current mocked inline-artifact shape.
- [ ] Promote Jetty evidence to production-grade only after `export-jetty -> run-jetty -> import-jetty-results -> benchmark` passes on one fixture-free and one fixture-backed demo case.

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
- [ ] Support concurrency limits and rate-limit handling: bounded worker pool, `Retry-After` support, jittered backoff, and no unbounded in-flight submissions.
- [x] Add a resumable run ledger so an interrupted suite can resume submitted trajectory IDs instead of resubmitting tasks; ambiguous submit responses fail closed until an explicit operator override.
- [x] Support dry-run payload loading without submission.
- [ ] Handle streaming Jetty responses when/if needed by the production API.

## Importing results

- [x] Add `import-jetty-results` command.
- [x] Import final assistant output into `runs/<case_id>/<variant>/run-<n>/output.md` when repeated, or the existing one-run layout.
- [x] Import generated artifacts into `outputs/` when present in trajectory records.
- [ ] Support non-inline artifact contracts if production Jetty returns artifact IDs, URLs, signed download links, nested output blocks, or workspace-relative paths instead of `{path, content}` objects.
- [ ] Fail closed with clear metadata when a completed trajectory has no importable `/app/results/output.md` artifact.
- [x] Normalize Jetty metrics into `metadata.json`: `elapsed_ms`, `input_tokens`, `output_tokens`, `total_tokens`, `model`, `total_tool_calls`, `errors_encountered`.
- [x] Preserve raw Jetty metadata: `jetty_trajectory_id`, `jetty_collection`, `jetty_task`, `jetty_agent`, `trace_url`, `jetty_raw`.
- [x] Keep local deterministic grading unchanged after import.

## Jetty evaluation integration

Jetty runbooks emit a standardized machine-readable `validation_report.json` per trajectory
(`jettyio/jettyio-skills`, `skills/create-runbook/SKILL.md`). Rubric evaluation scores 3-7
dimensions on a 1-5 scale; programmatic evaluation returns `PASS` / `PARTIAL` / `FAIL`. The
items below map that report onto the harness judge-result row `{judge_task_id, passed, score,
threshold, evidence}` (`load_judge_results:12140`, merged in `grade_case_variant:14549`).

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
- [ ] Add opt-in live smoke gated by `RUN_JETTY_SMOKE=1` and `JETTY_API_TOKEN`; never run it in default CI.
- [ ] Live smoke should exercise one fixture-free tune case, one fixture-backed tune case, and one cheap failure/timeout path if Jetty exposes one.
- [ ] Add README live-smoke notes after API behavior is verified with a real account.

## Open questions to verify against current Jetty docs/API

- [ ] The live-token questions tracked in `docs/jetty-support-spec.md` § "Remaining live-token questions"
      (upload response shape, artifact listing shape, `trajectory_id` field, terminal status set,
      directory-tree upload strategy, `use_trial_keys` availability). One list, one owner — the spec.

---

# Agent backend parity follow-ups

Design and acceptance criteria live in [`docs/agent-backend-interface-spec.md`](docs/agent-backend-interface-spec.md).
Claude, Codex, Gemini, and Vibe are native `run-agent`/judge backends. Keep the invariant
from the v0.5.0 work: new providers implement
shared backend protocols and conformance tests, not one-off grading or benchmark paths.

## Gemini CLI

- [x] Add a native Gemini answer backend (`run-agent --agent gemini`) with isolated cwd/config,
      `InvocationRequest`/`InvocationResult` plumbing, answer extraction from Gemini's JSON or
      stream JSON mode, provider telemetry normalization, and failure artifacts on spawn/nonzero/
      timeout. Materialize the same provider-neutral skill workspace used by other forced-load
      answer backends.
- [x] Add a native Gemini judge backend (`judge --judge-backend gemini`) that returns the canonical
      verdict JSON shape, stamps backend/requested-resolved-model/CLI-version/usage metadata,
      rejects unimplemented judge explore, and uses harness-side schema
      validation (`verdict_schema_for`, `--strict-judge-schema`) unless Gemini exposes a reliable
      provider-enforced schema hook.
- [ ] Add a Gemini autonomous trigger adapter for `skill-trigger-matrix --agent gemini` only after a
      token-backed run proves `activate_skill` can be allowed safely in headless mode without
      enabling unrelated tools. Until then the registry must keep `autonomous_trigger=false` and
      expose no trigger binding. Once proven: use a fresh
      config/home per run, skill mounting, activation evidence from Gemini skill/tool events when
      available, path-evidence fallback, and `RUN_GEMINI_TRIGGER_SMOKE` live-smoke coverage.
- [x] Add Gemini offline conformance fixtures/tests: success, malformed JSON/schema failure, timeout,
      nonzero/spawn failure, usage-present, usage-missing, tool lifecycles, and judge verdict
      parsing, plus auth/sandbox transport, workspace-control, invalid-byte, and version-provenance
      guards. Skill-activation evidence stays with the gated trigger item above.

## Mistral Vibe

Mistral support should mean first-class Vibe CLI support, not a raw chat-completions call: raw Mistral API can be used today through `--judge-cmd`, but it cannot measure Agent Skills discovery/loading. Vibe is the useful target because it exposes noninteractive runs, Agent Skills discovery, tool controls, and `MISTRAL_API_KEY` auth.

- [x] Validate the local Vibe CLI contract (`vibe 2.19.1` installed): noninteractive prompt syntax (`--prompt "$PROMPT"`; headless stdin prompt mode is not reliable without a tty), `--output json|streaming`, `--enabled-tools re:^$` no-tools mode, `--workdir`, `--trust`, isolated `VIBE_HOME` outside the model workdir, and `VIBE_ACTIVE_MODEL`.
- [x] Add a native Mistral Vibe answer backend (`run-agent --agent vibe`) with isolated `VIBE_HOME` outside the model workdir
      or equivalent config, project workspace setup, answer extraction from Vibe JSON/stream output,
      usage/cost normalization when available, and the same native failure artifact contract as
      Claude/Codex/Gemini.
- [x] Add a native Mistral Vibe judge backend (`judge --judge-backend vibe`) using the canonical
      verdict schema and strict-schema gate; preserve raw transcripts and normalized usage/cost
      blocks for every verdict.
- [x] Add a Mistral Vibe autonomous trigger adapter for `skill-trigger-matrix --agent vibe`: mount
      skills under `.agents/skills` or Vibe's native skill path, detect explicit skill activation
      events where possible, fall back to mounted-path evidence, and add `RUN_VIBE_TRIGGER_SMOKE`.
- [x] Add Vibe offline conformance fixtures matching the Gemini fixture set, including tool-call /
      skill-activation evidence and missing-telemetry cases.
- [x] Run token-backed Vibe live smokes after `MISTRAL_API_KEY` is available: direct no-tools prompt, `run-agent --agent vibe`, native `judge --judge-backend vibe`, `RUN_AGENT_INVOKE_SMOKE=1`, and `RUN_VIBE_TRIGGER_SMOKE=1` passed on 2026-07-09; Vibe usage/cost telemetry was absent and normalized as explicit `missing`.

## Cross-provider registry/docs

- [x] Extend `agent_capabilities.py`, `docs/agent-parity.md`, CLI help, README command tables, and
      smoke-test environment documentation for Vibe.
- [x] Add a registry/conformance guard that fails when a backend is partially registered (for
      example `run-agent` supports it but parity docs or capability rows do not).
- [x] Replace the parallel capability, answer, trigger, judge, workspace, trace, smoke,
      failure-policy, and provider-CLI registries with one validated `BACKENDS` row per backend
      (issue #52 item 4). Keep the old maps as replacement-only compatibility projections and
      expose the non-invoking `agent-capabilities` JSON view.
Standing invariant (not a tickable task): `--judge-cmd` stays the escape hatch for arbitrary
providers while first-class backends move through the native registry — the code documents it
as "the universal escape hatch" and `judge` points unknown backends at it.

- [ ] Apply the explicit availability/comparability approach to every cross-lab CLI wrapper:
      model prompt transport, final-answer channels, schema support, tool policy, config isolation,
      skill discovery, trace completeness, failure semantics, and telemetry as typed capabilities
      rather than loose provider dictionaries or implied parity. Keep thin native adapters—not a
      lowest-common-denominator wrapper—and add conformance fixtures for each declared surface.
      Design: [`docs/telemetry-availability-and-comparability-spec.md`](docs/telemetry-availability-and-comparability-spec.md)
      § “Follow-on: cross-lab CLI wrappers” and [`docs/agent-cli-control-plane.md`](docs/agent-cli-control-plane.md).

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
`tests/test_confidence_floor.py` and the subject files (`test_grading.py`,
`test_reporting.py`, `test_runners.py`, `test_manifest.py`, `test_judging.py`,
`test_stats.py`). The two TODO-native items below stay open by design.

**Moat — do not dilute:** causal lift, `tune`/`holdout`/`holdback` splits, leakage lint,
ablations, and saturation/no-lift flags are unique to this harness. None of the surveyed
frameworks have them, and notably *none* have first-class multi-model comparison either.
Every item below slots around these, not over them.

## Confidence floor (do first) — make a reported lift believable

These are tests of the harness, not a new eval suite; sequence them before the buckets.

- [x] CF.1 Detector meta-fixtures (keystone) — paired should-fire/should-pass per detector (`tests/fixtures/detectors/`, meta-test = registration contract)
- [x] CF.2 One cross-runner baseline-isolation invariant (`WORKSPACE_BUILDERS` registry:
      Claude/Codex/Gemini/Vibe/Jetty/subagent + Pi smoke)
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

---

# Consolidation follow-ups (2026-07 duplication audit) — done

All of the audit's deferred items landed in the follow-up pass:

- [x] Cost ledgers merged onto shared owners: `spend_of`/`group_spend`/`cost_coverage_block`/
      `cost_totals_block`, one `judge_cost_block`, and the billing disk-walk hoisted to the named
      `discover_on_disk_run_rows` beside the grading discovery (the ledger deliberately bills every
      on-disk arm; grading is variant-scoped — now a documented decision, not two private walks).
- [x] The case/model/variant/run discovery loop extracted as `discovered_run_units`, shared by
      `grade`, `build_benchmark_report`, `collect_judge_tasks`, and `contamination_report`.
- [x] One timeout encoding everywhere a runner spawns a process: `timed_out: True` + returncode 124
      (`run-codex`/`run-claude`/`run_subagent_tasks`/`shell_agent_backend`/`suggest-cases`/
      `_run_suite_command`), with `DEFAULT_RUNNER_TIMEOUT_S` replacing the eight `1800` literals.
      Guarded by `tests/test_consolidation_guards.py` TimeoutConventionTests.
- [x] `_locate_section` owns the fence-aware section scan for `section_span`/`list_item_ops`.
- [x] Sibling-TestCase borrowing removed (`make_tasks`, `make_two_model_runs`, `make_graded_repo`
      are module-level); the cross-file and module-level builder copies delegate to
      `tests/helpers.py`. The in-class builders still inside `tests/test_ablations.py` are
      deliberately NOT migrated: their shapes (dangling `skill_paths` for validation-failure
      fixtures, multi-root trees with files outside the copy-set, old-skill pairs) are the
      behavior under test, not drift.
- [x] The PR-named test files are gone: classes moved verbatim into subject files
      (`test_manifest.py`, `test_grading.py`, `test_judging.py`, `test_reporting.py`,
      `test_stats.py`, `test_runners.py`, `test_ablations.py` — the latter also absorbing
      test_skill_benchmark's ablation half and all of test_cbc), and the one strict-subset
      duplicate test (readiness capability-saturation, re-asserted from test_audit_fixes)
      was deleted rather than moved.
- [x] `ABLATION_VARIANT_PREFIX` / `is_ablation_variant()` / `ablation_id_of()` in `ablation_model`
      own the `ablation:<id>` encoding (14 inline `split(":", 1)[1]`/`startswith` sites rewired).

# User journeys the code supports but the docs don't walk

A 2026-07 docs review found a pattern: the machinery for a user journey exists (commands,
report blocks, tests) but no doc walks someone from their actual question to a decision, so
the journey is invisible unless you already know the command names. Each item below is one
such journey: the fix is a short walkthrough doc (or a runnable example), not new machinery.
The mold — question-shaped title, runnable on the demo, real dated output, symptom-by-symptom
reading guide, honesty rules, boundary — is written down in [`docs/README.md`](docs/README.md).

- [x] **"How do I make my skill trigger reliably?"** — `docs/tuning-skill-activation.md`:
      trigger cases in both polarities → `skill-trigger-matrix` per (agent, model) →
      description edits → re-run. Runnable on `examples/demo-skill` (offline `--agent stub`,
      live `RUN_TRIGGER_SMOKE=1`).
- [x] **"Is my skill worth its tokens?"** — [`docs/is-my-skill-worth-its-tokens.md`](docs/is-my-skill-worth-its-tokens.md):
      static footprint (`profile-skill`, offline) vs. runtime lift-per-token/lift-per-dollar
      (`token-overhead`, `cost-summary`, `audit-manifest --runs`), with the keep/trim/cut
      reading guide. Runnable on `examples/demo-skill` (offline shows the `source: "missing"`
      telemetry shape; real dollar numbers cited from the issue #21 suite run).
- [x] **"Did my skill edit regress anything?"** — [`docs/did-my-skill-edit-regress.md`](docs/did-my-skill-edit-regress.md):
      the within-run `ablation_regressions` block (assertion-level, significance-gated) as the
      before/after, and `render-viewer --previous-workspace` diffs across the `iteration-N/`
      convention as the cross-iteration localizer; `old_skill` formalizes the baseline in a real
      repo. Runnable offline on `examples/demo-skill` (real 2026-07-06 confirmed/indeterminate/empty
      diffs). `compare-results` was evaluated and rejected — it tallies a pairwise-preference judge,
      not an assertion diff.
- [x] **"Which model should my skill target?"** — [`docs/which-model-should-my-skill-target.md`](docs/which-model-should-my-skill-target.md):
      multi-model fan-out (`prepare --models`), the `by_model` report block, and `model_analysis`
      per-(case, model) lift, with the real-lift-vs-base-saturation reading guide. Runnable offline
      on `examples/demo-skill` (the model-blind stub yields identical tiers — plumbing proven, real
      divergence needs a live runner).
- [x] **"Why did this run fail?"** — [`docs/why-did-this-run-fail.md`](docs/why-did-this-run-fail.md):
      the `error-analysis` open-coding queue + failure taxonomy, then the run dir
      (`output.md`/`metadata.json`), mapped to a failure class and a manifest-or-skill decision.
      Runnable offline on `examples/demo-skill` (real 2026-07-06 taxonomy/queue over the failing arms).
- [x] **"Can I trust my judge?"** — [`docs/can-i-trust-my-judge.md`](docs/can-i-trust-my-judge.md):
      stability (`judge-robustness` order-flip + negative controls), accuracy vs. human labels
      (`judge-alignment`: kappa over raw agreement, precision/recall), and conclusion-sensitivity
      (`compare-judges` lift diff), with `--judge-runs`/`--judge-panel` as the repetition tools.
      Runnable offline: the demo gained a gate-severity `judge` assertion plus `stub_judge.py`
      (careful and `--lenient` rubber-stamp modes), so the whole loop — including the
      rubber-stamp's kappa-0.0 / control-leak-1.0 / lift-erosion signature — runs with no model
      (real 2026-07-09 output; guarded by `tests/test_example_demo.py`).
- [x] **"How do I gate my skill repo's CI on this?"** — [`docs/gating-ci-on-evals.md`](docs/gating-ci-on-evals.md):
      the two-gate recipe (`report --format junit|github` for regressions +
      `audit-manifest --fail-on-blockers` for manifest trust), a workflow file, and the
      "gate on lift/named regressions, not raw pass count" reading guide. Runnable offline on
      `examples/demo-skill` (real 2026-07-05 report/junit/readiness output).
- [x] **"How do I port my existing evals into the harness?"** — [`docs/porting-existing-evals.md`](docs/porting-existing-evals.md):
      `dataset_files` JSONL + one template case as the mechanical seam, then the additions the
      source framework had no slot for (paired baseline, splits, leakage-safe assertions),
      with `audit-manifest` findings as the post-port punch list and the run layout /
      `import-trace` path for already-recorded outputs. Runnable offline against the demo
      skill (real 2026-07-10 lint/lift/audit output), including the leakage lint firing on a
      genuinely leaky ported row.
- [x] **"How do I upgrade the harness from 0.5.1 to 0.6.0?"** —
      [`docs/upgrading.md`](docs/upgrading.md): preserve the old run tree, inspect
      and apply telemetry migration, repair strict judge/pair/trigger/Jetty inputs, regenerate
      reports, distinguish expected semantic changes from regressions, and roll back from the
      untouched 0.5.1 artifacts.
- [ ] **"Did my skill change HOW the model works, not just whether it passes?"** — the
      machinery shipped with the trace-depth slice: Claude answer runs now stream real
      tool-use traces, the report's `trajectory_diff` block shows paired command/count/skill-load
      deltas per case, and a `per_step` judge grades each completed step (raw records resolved
      through `raw_ref`). The walkthrough should run the demo offline, read a no-lift case
      through `trajectory_diff`, then escalate to `per_step` where outcome assertions saturate.
- [ ] **"Did removing this description actually break discovery?"** — extends
      [`docs/tuning-skill-activation.md`](docs/tuning-skill-activation.md): baseline
      `skill-trigger-matrix` run + `--ablation` run → `skill-benchmark trigger-compare` →
      read the evidence class (confirmed/refuted/indeterminate), the ≥6-query significance
      bound, and blocked-pair reasons before editing the description again.
