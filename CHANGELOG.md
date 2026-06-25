# Changelog

All notable public changes are listed here. Release tags are the source of truth for exact code state.

## Unreleased

- Make ablation runners sound: one canonical tree builder for every skill-bearing arm so `with_skill` and the ablation arm have an identical file surface (Pi smoke, `run-codex`, and Jetty all copy every root via the same copier). `run-codex` now executes in an isolated per-task workspace (the original repo skill is unreachable; `without_skill` mounts no skill). Full ablation provenance (`skill_hash`, components) is persisted in each runner's metadata. README routing/mechanism docs corrected.

- Add materialized skill ablations: `skill-benchmark materialize-ablations` writes real, altered skill trees (removal-only) for `manifest.ablations` that declare a `mechanism` or `components` + `target`. Mechanisms: `frontmatter_field`, fence-aware `section`, `anchor`, `list_item`, deletion-only `patch`, `reference` (pointer/content/both), `script`, `asset`; with multi-component composition and net-deletion / disjointness / layer-cohesion / required-field gates, unique slug ids, and path-traversal/containment checks. Manifests with no removal declaration keep instruction-simulated behavior unchanged.
- Wire materialized ablations through the runners and reporting: the Pi smoke runner mounts the altered tree, the Pi trigger runner gains `--ablation` to mount a materialized (e.g. weakened-description) skill, and `export-jetty` uploads the tree recursively with relative paths preserved (no basename flattening). `prepare`/`export-jetty` gain `--ablation-dir`; ablation rows route by case population (discovery→trigger cases, answer→non-trigger) and carry `{mode, population}` provenance; and the benchmark report adds an `ablation_regressions` block distinguishing "score regressed" (aggregate) from assertion-level "expected regression confirmed".
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
