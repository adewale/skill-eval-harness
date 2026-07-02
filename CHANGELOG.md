# Changelog

All notable public changes are listed here. Release tags are the source of truth for exact code state.

## Unreleased

- Make **token and dollar cost first-class eval telemetry** (issue #21). Every runner path (Pi smoke, Pi trigger — which previously recorded no usage at all — Codex, Claude, subagent, the judge wrapper, and the Jetty importer) writes normalized `usage_normalized`/`cost_normalized` blocks with explicit provenance (`provider_reported` vs `trace_normalized` vs `missing`/`not_applicable`; missing is marked, never zero; provider blocks beat trace-derived ones). `benchmark`/`aggregate` gain a `cost_summary` ledger — operational totals over ALL runs including execution errors, per-variant mean/median/p90, paired cost deltas, ablation marginal cost and cost per confirmed regression, judge spend as a separate line. New `cost-summary` command writes the standalone suite ledger (JSON + markdown) with top spenders and spend-without-signal findings. `suite-run` projects spend before any model call (medians from `--cost-history` ledgers, static fallback) and gates on `--max-estimated-tokens`/`--max-estimated-cost-usd`, failing closed when a dollar cap has no estimate. `token-overhead` adds dollar deltas, lift-per-dollar, and the cost of saturated/no-lift pairs. `audit-manifest --runs` flags expensive saturated/no-lift/judge-only cases, high-spend unstructured ablation arms, and high-footprint low-lift skills above `--expensive-case-usd`.

- Implement the **eval-framework roadmap** (`docs/eval-framework-roadmap-spec.md`, issue #18) end to end. The confidence floor is executable: paired should-fire/should-pass fixtures for every objective detector with a registration meta-test (CF.1), one cross-runner `without_skill` isolation invariant over a workspace-builder registry (CF.2), re-grade idempotence (CF.3), and a no-subprocess/no-network guard on the core grade path (CF.4).

- **Graded measurement (2.2)**: three-tier assertion severity (`critical` vetoes the run and is excluded from every mean; `gate` carries the pass rate; `soft` feeds *only* the per-run graded score and never moves the objective/qualitative/combined pass rate anywhere — declare `severity: "gate"` on a judge assertion to keep it in the pass rate). Anchored `graded_dimensions` and `dynamic_rubric` judge shapes work end to end through the `judge` command (which persists the graded payload and derives its verdict through the same owner the merge uses). Reference-anchor floors, a sign-flip permutation `significance` block beside every paired delta, a paired `graded` channel, and `structurally-pass-but-forgettable`/`critical-failure`/`below-reference-floor` case flags. `--strict` promotes soft to gate. Binary version-1 manifests grade identically (regression-tested).

- **Model as a third axis (2.1, 3.2)**: `prepare --models a,b,c` fans rows per model with a model run-dir segment; grading discovers both layouts; `judge_task_id` carries the model on fanned runs so verdicts merge onto the right model (single-model IDs unchanged); reports gain `by_model`, per-(case, model) pairing, `model_analysis` (lift ranking + lift losers), and slice-lift concentration ratios.

- **New deterministic assertion surface**: `golden_output` (reference-file equality with explicit normalization and diff evidence, 1.6), `similarity` (difflib ratio, thresholded and scored, 1.4; `mode: "embedding"` behind the opt-in `--embed-cmd`, 4.1), `structured_output` (JSON-Schema subset, 1.1), `tool_call` (matches completed shell-command *and* normalized `type: "tool_call"` events, with order/count bounds, 1.1), graded `script` oracles (`{"score", "max_score"}` stdout line, 1.8), oracle-strength tiers with per-case strong-share reporting and a `weak-oracle-only` audit (1.7), and the `factuality` judge preset (1.1).

- **Runner layer**: `run-subagent` (2.7) — an in-process backend seam writing the full run contract, honoring row models and multi-turn sequences, registered into the CF.2 invariant — with keyed, versioned **tool replay** (`--tool-replay record|replay|strict|auto`, 2.3); OTel GenAI semantic-convention attributes on every normalized trace event and metrics block (schema v2; v1 files still grade, 2.4); held-out rubric discipline (2.7b) — a `held-out-rubric-leak` audit finding plus `qualitative_by_visibility` separating held-out from tune-visible scores over judge-carried signal only (deterministic soft objective checks like `similarity` never enter that view).

- **Authoring and scale**: dataset templates fan one case over a row set with per-materialized-case leakage lint (2.5); YAML manifests + JSONL `dataset_files` compile in memory to the same manifest shape (3.3); multi-turn `turns` cases grade per turn against a turn-indexed transcript with the final turn as the answer of record, with per-turn assertions validated by the same validator as case-level ones (3.1).

- **Reporting and lifecycle**: `report --format junit|github` for CI (1.2), where a JUnit `<testcase>` fails on objective *and* qualitative gate/critical failures (soft never flips a case); `render-viewer` artifact embedding, `--serve` with `feedback.json` capture (2.8), and `--previous-workspace` iteration diffs (2.9); `trend` — an append-only history with series, successive diffs, prevalence×severity failure ranking (2.6), and staleness prune candidates (1.9); `suggest-cases` turning saturation flags into harder-case candidates with strictly opt-in generation that never edits a manifest (2.10, 4.2).

- **Judge config slot (1.3)**: manifests declare `judge.model` (the `judge` command's default); `audit-manifest` flags `judge-is-model-under-test` (fatal under `--strict-judge`). Leakage findings and fixture recommendations now point at the authoring guide's rule (1.5).

- **Manifest version 2 + migration**: `validate` accepts versions 1 and 2; `skill-benchmark migrate` stamps the mechanical defaults (severity, oracle tiers, `graded?` markers), prints the diff and the judgment-call checklist (`--check` dry run), and `docs/migrating-evals.md` is the versioned agent runbook ending in an identical-pass-rates re-grade check.

- Docs code references (`name:line`) across TODO/specs are now verified by a unit test against the actual source, so they cannot silently rot again.

- Add a first-class **Claude runner** (`run-claude`) that executes prepared rows through `claude -p --output-format json`, parses the envelope in one place (`parse_claude_cli_json`), and records real `total_cost_usd` + token usage into each run's `metrics.json`. The benchmark report now totals `cost_usd_total` per arm (over scorable runs), so a paired eval reports actual dollars — the thing the Codex/Jetty adapters left the caller to reconstruct. `CLAUDE_FAILURE` joins the runner failure markers so a nonzero exit/timeout is non-scorable like the others.

- Make the **judge model a recorded, multi-valued dimension**. Every judge verdict is stamped with `judge_model`; `judge` can grade natively with `--judge-model` (the Claude adapter, capturing per-verdict cost) or any `--judge-cmd`, both producing one verdict shape. New `compare-judges` takes ≥2 judged reports (`--report name=path`) and flags **judge-sensitivity** — `sign_sensitive` when judges disagree the skill helps, `magnitude_sensitive` when the with−without lift spread exceeds `--magnitude-eps`. A single judge number is not reproducible across judge choice for a subtle skill; this makes that measurable.

- Extend the eval-**readiness** verdict with the signals a static manifest can't see: `objective_only_cases` (a behaviour case with no judge assertion can only measure objective compliance) plus, when run data is supplied, `base_saturated_cases` (measured `with_skill == without_skill` — a blocker: the case measures nothing) and `qualitative_only_cases` (objective flat but combined lifts — the skill's value is qualitative, which an objective-only eval would miss).

- Keep the **answer and discovery populations distinct in the report**. `benchmark` no longer folds `kind:"trigger"` cases into its paired answer pass-rate (a discovery measurement is a different population, graded with opposite polarity by the trigger adapter); it records `skipped_trigger_cases` and stamps `population: "answer"`, so an answer pass-rate can't be lined up against a Pi-trigger `raw_autonomous_trigger_measurement` as if they were the same metric.

- Fix two judge-path defects surfaced by a live multi-model run: the judge-result merge crashed on a `{"passed": true, "score": null}` verdict (an eager `dict.get` default evaluating `None >= 1`) — now a single-owner `judge_verdict_passed` guards it; and judge-task emission now honors the `scorable_run` predicate like every other report view, so the judge no longer spends model calls grading missing/failed runs (24→8 judge calls on a real 4-case run).

- Add an eval-**readiness** verdict to `audit-manifest` (`eval_readiness`): a compact, offline "is this eval worth paying to run?" summary — ablations materialized vs instruction-simulated, **leak-saturated cases** (every positive objective assertion's value already appears in the prompt, so the case can't tell skill from no-skill), adversarial coverage, judge-only cases, and an explicit `blockers` punch list. Surfaced in both the JSON and markdown audit output. `audit-manifest --fail-on-blockers` exits non-zero when any blocker remains, so a skill repo can CI-gate its eval suite at "worth paying to run" the same way it gates tests. It turns the scattered findings into one gate you drive to green before spending model budget.

- Add `docs/ablation-study-walkthrough.md` + `examples/skill-pins.json` — a worked ablation study across ten real skills, pinned to exact commit SHAs and the harness's `canonical_skill_tree_hash` so it reproduces against the evaluated versions **without vendoring** skill content (the repo stores only `repo`+`sha`+`tree_hash` pointers; fetch on demand). Records the replication lesson — re-running the three strongest single-shot signals 5× per arm refuted two of them — and a no-vendoring reproduce recipe whose tree-hash check is verified end-to-end. The slide-maker pin demonstrates the tree-hash catching a branch that advanced past the evaluated commit.

- Add `examples/demo-skill/` — a self-contained, offline end-to-end example and the harness's executable documentation. A tiny synthetic skill with two materialized ablations (`section` + `reference`) and a deterministic stub runner (`stub_runner.py`) that keys off the mounted skill content, so `prepare → run-codex → benchmark` confirms a distinct regression per ablation with no model or API key. `tests/test_example_demo.py` runs it in CI (with_skill 1.0 / without_skill 0.0; each ablation `expected_regression_confirmed` with verified provenance).

- Make `PreparedTask` the sole authority after the JSONL boundary. The Codex runner (`codex_skill_workspace`, `codex_task_prompt`, `run_codex`) and the Jetty exporter (`jetty_task_name`, `safe_task_json`, `build_jetty_payload`) previously took a raw row dict and each re-derived the variant, skill paths, blinding, and upload token. They now take a `PreparedTask`, constructed once via `from_row` at each boundary (the `run_codex` loop and the `export-jetty` payload loop). The model-visible variant in a Jetty task JSON comes from `pt.model_facing_variant()` in one place (a blind materialized arm presents as `with_skill`; every other arm shows its true variant) instead of a per-variant override, so "model-facing variant / upload token / harness truth / skill paths" are owned by one object rather than re-spelled per adapter.

- Enforce the minimum provenance schema at the JSON boundary, not just for in-process construction: `Provenance.from_dict`, `Component.from_dict`, and `InstructionSimulated.from_dict` now raise `ValueError` on a missing, null, or wrong-typed required field (`id`/`mode`/`population`/`skill_hash`/`parent_skill_hash`/well-formed `components` for a Provenance; `class`/`mechanism`/`skill_root`/`target` for a Component) instead of silently constructing a `None`-filled record via `dict.get()`. An empty-but-present components list is still allowed. The regression verifier now catches a malformed recorded provenance and fails that confirmation gracefully (`provenance unverified: … malformed`) rather than crashing the report.

- Fix a mutation-before-validation ordering bug in ablation materialization: `_ensure_ablation_dir` created/cleared/marked the output directory before the containment gate (`_reject_output_root_overlap`) ran inside `materialize()`, so a bad `--ablation-dir`/`--out-dir` pointed inside a source skill root could write a `.skill-ablation-dir` marker into the live skill tree, and a harness-owned directory there could be cleared, before the command rejected it. `materialize-ablations`, `materialize_declared_ablations` (used by `prepare`/`export-jetty`), and `export-jetty`'s tree dirs now validate every declared ablation and run the non-mutating containment gates through one guarded `_ensure_ablation_dir_guarded` owner before anything on disk is touched.

- Drive the migration from instruction-simulated to materialized ablations: `audit-manifest` now emits an `ablation-instruction-simulated` finding for every label-only ablation (one with no `mechanism`/`components`), explaining that the arm is non-blind and raw-measurement-only and naming the structural mechanisms (`section`/`list_item`/`frontmatter_field`/`reference`/`patch`) that would make it a blind, confirmation-gradeable removal. Materialized ablations stay silent.

- Harden the ablation experimental contract: reject nested (ancestor/descendant) skill roots that would leave an unablated duplicate; confine frontmatter `patch` hunks to fields of the declared kind (discovery vs runtime); make `list_item`/`preprocess` honor Markdown structure (code-fence/inline-code/heading-level aware); restrict discovery (trigger-population) ablations to the autonomous-trigger adapter (`prepare` no longer emits them for forced-load runners); exclude infrastructure failures (nonzero exit/timeout/synthetic failure body) from scoring and confirmation; confirm a regression per cited case using the combined (objective+qualitative) score rather than OR-ing an objective drop across cases; verify exact recorded provenance on every measured run and require the canonical parent-tree hash to match on both arms before confirming; and give Jetty uploads variant-neutral names plus a runbook that never reveals the ablation arm.

- Make ablation runners sound: one canonical tree builder for every skill-bearing arm so `with_skill` and the ablation arm have an identical file surface (Pi smoke, `run-codex`, and Jetty all copy every root via the same copier). `run-codex` now executes in an isolated per-task workspace (the original repo skill is unreachable; `without_skill` mounts no skill). Full ablation provenance (`skill_hash`, components) is persisted in each runner's metadata. README routing/mechanism docs corrected.

- Add materialized skill ablations: `skill-benchmark materialize-ablations` writes real, altered skill trees (removal-only) for `manifest.ablations` that declare a `mechanism` or `components` + `target`. Mechanisms: `frontmatter_field`, fence-aware `section`, `list_item`, deletion-only `patch`, `reference` (pointer/content/both), `script`, `asset`; with multi-component composition and net-deletion / disjointness / layer-cohesion / required-field gates, unique slug ids, and path-traversal/containment checks. Manifests with no removal declaration keep instruction-simulated behavior unchanged.
- Wire materialized ablations through the runners and reporting: the Pi smoke runner mounts the altered tree, the Pi trigger runner gains `--ablation` to mount a materialized (e.g. weakened-description) skill, and `export-jetty` uploads the tree recursively with relative paths preserved (no basename flattening). `prepare`/`export-jetty` gain `--ablation-dir` (required when an ablation declares a removal); answer-population ablation rows route to non-trigger cases (discovery ablations go to the trigger adapter, not the generic runners) and carry `{mode, population}` provenance; and the benchmark report adds an `ablation_regressions` block distinguishing "score regressed" (aggregate) from assertion-level "expected regression confirmed".
- Complete the spec: `validate --check-ablations` dry-runs the apply-time gates (materializes to a temp dir, writes nothing); an opt-in `invalid_skill: true` ablation mode permits required-field removal and is reported separately; materialization records per-component `removed_bytes` + an `isolation_warnings` warning when one component removes >60% of a file; and `audit-manifest` flags ablation hygiene (dangling reference, unknown expected_regression case/assertion, missing expected_regressions).
- Add a `preprocess` removal mechanism (inline `` !`command` `` and ```` ```! ```` blocks) and complete the materialization provenance (`skill_hash`, per-component `removed_bytes`). Add an end-to-end Pi smoke execution test (stubbed `pi` binary) proving the materialized ablated skill is what actually runs, plus coverage for the script/asset/reference-both/multi-root paths and symlink-escape containment.
- Add `docs/skill-ablation-spec.md` (materialized-ablation design) and index it from the README.
- Add `docs/vocabulary.md` glossary and `docs/evals-are-not-tests.md` conceptual page; link both from the README documentation map. Docs only; no behavior changes.

## v0.4.2 — 2026-06-12

- Tighten README and trace-aware spec language after the v0.4.1 runner release; no behavior changes.
- Add trace-runner lessons on no-skill workspace isolation, trace-backed process assertions, runner-shape normalization, and post-release spec tense.
- Add a `good-repo` effectiveness audit, package keywords/classifiers, and project URLs.
- Add `skill-benchmark token-overhead` for static skill footprint, paired runtime token deltas, and objective lift per 1k extra tokens.

## v0.4.1 — 2026-06-11

- Add variant-scoped assertions with `variants` / `only_variants` / `except_variants`.
- Ground trace import in live Pi and Codex event shapes: Pi `message_end` usage aliases and Codex `item.completed` / `command_execution` JSONL.
- Add a shared trace artifact writer and use it for `import-trace`, `run-codex`, Jetty result import, Pi smoke runs, and optional Pi trigger traces.
- Harden the Adewale Pi smoke runner by executing in an isolated temporary workspace with only allowed files for each variant.
- Add `skill-pi-trigger-eval --trace-runs` for per-query trace artifacts.

## v0.4.0 — 2026-06-11

- Add GitHub Actions CI for Python 3.10, 3.11, and 3.12.
- Add contributor guidance, PR template, and issue forms.
- Tighten README quick start with expected output landmarks and repo-surface links.
- Add a trace-aware eval proposal covering runner trace artifacts, process/efficiency assertions, Codex adapter support, richer reporting, dataset audits, structured judges, and skill-profile checks.
- Implement the first trace-aware harness slice: `import-trace`, `run-codex`, process/efficiency assertion types, telemetry-aware benchmark summaries, paired deltas/normalized gain, taxonomy slice summaries, taxonomy audit warnings, and `profile-skill`.

## v0.3.0 — 2026-06-10

- Add opt-in `script` assertions gated by `--allow-scripts`.
- Add prompt/assertion leakage lint with non-fatal default warnings and `--strict-leakage` for failure mode.
- Add `skill-benchmark judge` with `--judge-cmd`, repeated judge runs, transcript writing, and JSON result parsing.
- Add ablation variant support in the Adewale Pi smoke runner.

## v0.2.0 — 2026-06-10

- Add Jetty adapter phase one: `export-jetty`, `run-jetty`, `import-jetty-results`, and dry-run support.
- Ground Jetty request shape in Jetty docs and `jettyio/jettyio-skills` conventions.
- Add mocked upload/submit/poll coverage and hidden-prompt placeholder safety.

## v0.1.1 — 2026-06-09

- Switch public installation instructions to `uv` / `uvx`.

## v0.1.0 — 2026-06-09

- Initial standalone shared skill eval harness.
- Add manifest validation, answer-key-safe task preparation, deterministic grading, benchmarking, and reports.
