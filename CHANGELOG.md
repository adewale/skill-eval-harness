# Changelog

All notable public changes are listed here. Release tags are the source of truth for exact code state.

## Unreleased

- Tighten README and trace-aware spec language after the v0.4.1 runner release; no behavior changes.

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
