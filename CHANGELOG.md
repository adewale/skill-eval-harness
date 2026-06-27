# Changelog

All notable public changes are listed here. Release tags are the source of truth for exact code state.

## Unreleased

- Add `docs/ablation-study-walkthrough.md` + `examples/skill-pins.json` — a worked ablation study across ten real skills, pinned to exact commit SHAs and the harness's `canonical_skill_tree_hash` so it reproduces against the evaluated versions **without vendoring** skill content (the repo stores only `repo`+`sha`+`tree_hash` pointers; fetch on demand). Records the replication lesson — re-running the three strongest single-shot signals 5× per arm refuted two of them — and a no-vendoring reproduce recipe whose tree-hash check is verified end-to-end. The slide-maker pin demonstrates the tree-hash catching a branch that advanced past the evaluated commit.

- Add `examples/demo-skill/` — a self-contained, offline end-to-end example and the harness's executable documentation. A tiny synthetic skill with two materialized ablations (`section` + `reference`) and a deterministic stub runner (`stub_runner.py`) that keys off the mounted skill content, so `prepare → run-codex → benchmark` confirms a distinct regression per ablation with no model or API key. `tests/test_example_demo.py` runs it in CI (with_skill 1.0 / without_skill 0.0; each ablation `expected_regression_confirmed` with verified provenance).

- Make `PreparedTask` the sole authority after the JSONL boundary. The Codex runner (`codex_skill_workspace`, `codex_task_prompt`, `run_codex`) and the Jetty exporter (`jetty_task_name`, `safe_task_json`, `build_jetty_payload`) previously took a raw row dict and each re-derived the variant, skill paths, blinding, and upload token. They now take a `PreparedTask`, constructed once via `from_row` at each boundary (the `run_codex` loop and the `export-jetty` payload loop). The model-visible variant in a Jetty task JSON comes from `pt.model_facing_variant()` in one place (a blind materialized arm presents as `with_skill`; every other arm shows its true variant) instead of a per-variant override, so "model-facing variant / upload token / harness truth / skill paths" are owned by one object rather than re-spelled per adapter.

- Enforce the minimum provenance schema at the JSON boundary, not just for in-process construction: `Provenance.from_dict`, `Component.from_dict`, and `InstructionSimulated.from_dict` now raise `ValueError` on a missing, null, or wrong-typed required field (`id`/`mode`/`population`/`skill_hash`/`parent_skill_hash`/well-formed `components` for a Provenance; `class`/`mechanism`/`skill_root`/`target` for a Component) instead of silently constructing a `None`-filled record via `dict.get()`. An empty-but-present components list is still allowed. The regression verifier now catches a malformed recorded provenance and fails that confirmation gracefully (`provenance unverified: … malformed`) rather than crashing the report.

- Fix a mutation-before-validation ordering bug in ablation materialization: `_ensure_ablation_dir` created/cleared/marked the output directory before the containment gate (`_reject_output_root_overlap`) ran inside `materialize()`, so a bad `--ablation-dir`/`--out-dir` pointed inside a source skill root could write a `.skill-ablation-dir` marker into the live skill tree, and a harness-owned directory there could be cleared, before the command rejected it. `materialize-ablations`, `materialize_declared_ablations` (used by `prepare`/`export-jetty`), and `export-jetty`'s tree dirs now validate every declared ablation and run the non-mutating containment gates through one guarded `_ensure_ablation_dir_guarded` owner before anything on disk is touched.

- Drive the migration from instruction-simulated to materialized ablations: `audit-manifest` now emits an `ablation-instruction-simulated` finding for every label-only ablation (one with no `mechanism`/`components`), explaining that the arm is non-blind and raw-measurement-only and naming the structural mechanisms (`section`/`list_item`/`frontmatter_field`/`reference`/`patch`) that would make it a blind, confirmation-gradeable removal. Materialized ablations stay silent.

- Remove the `anchor` ablation mechanism (the author-placed `<!-- ablation:ID:start/end -->` marker convention) and the marker-stripping it required. It was an escape hatch for ablating non-structural spans; in practice no skill used it, and the markers risked leaking the experiment into the model-visible skill. Use a structural mechanism (`section`, `frontmatter_field`, `list_item`) or a deletion-only `patch` to target an arbitrary span instead.

- Harden the ablation experimental contract: reject nested (ancestor/descendant) skill roots that would leave an unablated duplicate; confine frontmatter `patch` hunks to fields of the declared kind (discovery vs runtime); make `anchor`/`list_item`/`preprocess` honor Markdown structure (code-fence/inline-code/heading-level aware); restrict discovery (trigger-population) ablations to the autonomous-trigger adapter (`prepare` no longer emits them for forced-load runners); exclude infrastructure failures (nonzero exit/timeout/synthetic failure body) from scoring and confirmation; confirm a regression per cited case using the combined (objective+qualitative) score rather than OR-ing an objective drop across cases; verify exact recorded provenance on every measured run and require the canonical parent-tree hash to match on both arms before confirming; and give Jetty uploads variant-neutral names plus a runbook that never reveals the ablation arm.

- Make ablation runners sound: one canonical tree builder for every skill-bearing arm so `with_skill` and the ablation arm have an identical file surface (Pi smoke, `run-codex`, and Jetty all copy every root via the same copier). `run-codex` now executes in an isolated per-task workspace (the original repo skill is unreachable; `without_skill` mounts no skill). Full ablation provenance (`skill_hash`, components) is persisted in each runner's metadata. README routing/mechanism docs corrected.

- Add materialized skill ablations: `skill-benchmark materialize-ablations` writes real, altered skill trees (removal-only) for `manifest.ablations` that declare a `mechanism` or `components` + `target`. Mechanisms: `frontmatter_field`, fence-aware `section`, `anchor`, `list_item`, deletion-only `patch`, `reference` (pointer/content/both), `script`, `asset`; with multi-component composition and net-deletion / disjointness / layer-cohesion / required-field gates, unique slug ids, and path-traversal/containment checks. Manifests with no removal declaration keep instruction-simulated behavior unchanged.
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
