# Eval-framework roadmap spec

Design for the backlog in [`TODO.md`](../TODO.md) (section "Eval-framework parity &
ideas"). Each item lists its **goal**, the **abstractions it uses or changes** (with the
real symbols in `skill_benchmark.py`), the **design**, and **how it is tested**.

The non-negotiable invariant for every item: **core grading stays local and
deterministic and never calls a model**, and the harness never chooses a model on the
user's behalf. Anything needing a model lives behind an opt-in command or the external
`--judge-cmd`.

---

## 0. The abstractions this spec touches

These are the load-bearing concepts. New work should reuse them, not duplicate them.

| Abstraction | Where it lives | Role |
|---|---|---|
| **Manifest** | `validate_manifest` (`:193`) | Source of truth: `skill_name`, `skill_paths`, `variants`, `split_policy`, `cases`, `ablations`, optional `harness`/`jetty`/`run_protocol`. |
| **Case** | `iter_cases` (`:79`), `case_prompt` (`:88`) | One scenario: `id`, `split`, `kind`, `prompt`/`prompt_ref`, `files`, `expected_behavior`, `assertions`, taxonomy (`domain`/`difficulty`/`trigger_type`/`success_goals`). |
| **Variant** | `DEFAULT_VARIANTS` (`:28`), `task_variants` (`:302`), `variant_instruction` (`:265`) | Comparison arm: `with_skill` / `without_skill` / `old_skill` / `ablation:<id>`. Orthogonal to cases. |
| **Split** | `VALID_SPLITS` (`:27`) | `tune` / `holdout` / `holdback` discipline. |
| **Assertion families** | `TEXT_ASSERTIONS` / `PROCESS_ASSERTIONS` / `EFFICIENCY_ASSERTIONS` / `QUALITATIVE_ASSERTIONS` (`:29–54`) | Graders. Objective ones run in `assertion_result` (`:1642`) / `process_or_efficiency_assertion_result` (`:1208`). |
| **Prepared task row** | `prepared_task_rows` (`:314`) | Runner-neutral unit: `case_id × variant × run_number` + `instruction`, `prompt`, `input_files`, `skill_paths`, `run_dir`. Answer-key-safe by default. |
| **Run-output contract** | `discover_run_bases` (`:1028`), `read_output_base` (`:1061`), `read_metadata_base` (`:1083`) | File boundary: `runs/<case>/<variant>/[run-N/]output.md` + optional `metadata.json`, `trace.jsonl`, `events.json`, `metrics.json`, `environment.json`. |
| **Runner / adapter** | `run_codex` (`:1582`), `JettyClient` (`:663`), `import_trace` (`:1532`), `examples/adewale-workspace/run_pi_smoke.py` | Produces the contract. Harness is runner-agnostic. |
| **Trace normalization** | `normalize_trace_record` (`:1373`), `normalize_trace_records` (`:1447`), `write_trace_artifacts` (`:1495`) | Raw runner events → schema-versioned `events.json` + `metrics.json`, source-labelled. |
| **Judge plumbing** | `collect_judge_tasks` (`:1787`), `judge_prompt` (`:1767`), `run_one_judge_task` (`:1801`), `merge_repeated_judge_rows` (`:1847`), `load_judge_results` (`:1721`) | Deferred qualitative grading via BYO `--judge-cmd`, keyed by `judge_task_id` (`:1717`). |
| **Grade result row** | `grade_case_variant` (`:1877`) | Per case/variant/run row: objective/process/efficiency/qualitative/combined counts + rates, `missing_output`, `metadata`. |
| **Benchmark report** | `build_benchmark_report` (`:2182`), `build_paired_summary` (`:2119`), `build_slice_summary` (`:2156`) | By-variant stats, paired lift/delta, slice breakdowns, case flags, token overhead. |
| **Hygiene** | `prompt_assertion_leakage_findings` (`:164`), `audit_manifest_report` (`:2870`), `fixture_recommendations` (`:2847`) | Leakage lint, manifest audit, fixture nudges. |
| **Viewer / exports** | `render_viewer` (`:2506`), `export_anthropic` (`:2401`), `aggregate` (`:2296`), `compare_results` (`:2475`) | Reporting surfaces. |

**Testing baseline.** All work is exercised through `tests/test_skill_benchmark.py` with
`python3 -m unittest discover tests`. The house rule (CONTRIBUTING.md): **no live model or
network calls in unit tests** — use fixtures and mocked clients (the `JettyClient` mock
pattern is the template). Every item below names the fixtures/mocks it adds.

---

## Bucket 1 — near drop-in

### 1.1 Built-in judge presets (`factuality`, `tool_call`, `structured_output`)
- **Goal:** ship batteries-included graders so authors stop hand-rolling rubrics; match
  the vitest-evals preset set.
- **Abstractions used/changed:**
  - `tool_call` and `structured_output` are **deterministic** → add as new types in
    `TEXT_ASSERTIONS` / `PROCESS_ASSERTIONS` and implement in `assertion_result` /
    `process_or_efficiency_assertion_result`. `tool_call` reuses `command_events` (`:1155`)
    and the `command_order` logic; `structured_output` extends `json_field_equals` with
    JSON-Schema validation against `output.md`-adjacent artifacts.
  - `factuality` is **model-backed** → it is a *preset rubric*, not new core code. Ship it
    as a named rubric template that `judge_prompt` (`:1767`) renders; it still runs through
    `--judge-cmd`. No model is invoked by the harness.
- **Design:** presets are sugar over existing machinery; no new execution path. A preset
  expands to either an objective assertion (deterministic) or a `judge` assertion with a
  canned rubric + `threshold`.
- **Testing:** unit tests in `tests/` for each deterministic preset (pass/fail/missing
  evidence). For `factuality`, a fixture `judge-results.jsonl` proves the merge path via
  the existing `load_judge_results` test; no live model.

### 1.2 GitHub Actions reporter / JUnit XML
- **Goal:** surface per-case pass/fail + lift on the PR.
- **Abstractions used/changed:** pure consumer of `build_benchmark_report` output. Add a
  `report` subcommand (or `--format junit|github` on `benchmark`) that serializes
  `results` + `case_flags` + paired summary. No grading changes.
- **Design:** JUnit = one `<testcase>` per case/variant/run, failures carry `evidence`.
  GitHub = job-summary markdown + annotations keyed to `case_id`.
- **Testing:** golden-file test asserting the XML/markdown shape from a fixed
  `benchmark.json`.

### 1.3 Judge config slot + "judge ≠ model under test" guard
- **Goal:** make the existing convention enforceable.
- **Abstractions used/changed:** read an optional `judge` block in the manifest
  (`validate_manifest`); add a check in `audit_manifest_report` / `validate` comparing the
  declared judge model against `jetty.model` / run metadata `model`.
- **Design:** warning by default, hard error under a `--strict-judge` flag.
- **Testing:** unit tests for matching vs. differing model ids.

### 1.4 Local `similarity` scorer
- **Goal:** the deterministic middle between `regex` and a judge.
- **Abstractions used/changed:** new type in `TEXT_ASSERTIONS`, implemented in
  `assertion_result`; compares `output.md` text to an `expected` string via a stdlib
  ratio (`difflib.SequenceMatcher`) with a `threshold`. Emits a `score` in the result row
  (see 2.2 for where scores surface).
- **Testing:** unit tests across threshold boundaries; no dependencies.

### 1.5 Workflow guide (done) + enforceable hints
- **Status:** `docs/authoring-evals.md` shipped.
- **Follow-on:** surface its rules where checkable — extend
  `prompt_assertion_leakage_findings` messaging and `fixture_recommendations` to point at
  the relevant guide section. **Testing:** assert the new hint strings appear for crafted
  manifests.

---

## Bucket 2 — natural extension

### 2.1 Multi-model fan-out (priority)
- **Goal:** run the same cases across N models and compare lift per model — a capability
  no surveyed framework has.
- **Abstractions used/changed:**
  - `prepared_task_rows` (`:314`): add a `model` dimension to the fan-out, parallel to
    `variant`/`run_number`. Each row carries its target `model`; `run_dir` gains a model
    segment (`<case>/<model>/<variant>/run-<n>`), kept backward compatible when a single
    model is used.
  - Runners (`run_codex`, Jetty export) pass the row `model` through; per-run `model`
    already lands in `metadata.json`, so grading needs no change.
  - `build_benchmark_report` (`:2182`): group `by_variant` *within* `by_model`, and extend
    `build_paired_summary` (`:2119`) to compute lift per (case, model).
- **Design:** model is a new axis, not a new variant — variants stay orthogonal (a model ×
  variant grid). CLI: `--models a,b,c` on `prepare`.
- **Testing:** fan-out test asserting row count = cases × variants × runs × models and
  correct `run_dir`s; report test with fixture runs under two models asserting per-model
  paired lift.

### 2.2 Numeric scores + gate/soft severity tier
- **Goal:** replace binary pass/fail with scored, severity-tagged assertions; gate on
  split.
- **Abstractions used/changed:**
  - `assertion_result` (`:1642`) returns `{name, type, passed, evidence}` today — add
    optional `score` and `severity` (`gate` | `soft`). Read `gate`/`soft`/`atLeast` off the
    assertion dict.
  - `grade_case_variant` (`:1877`): split totals into gated vs. soft; soft failures do not
    reduce the pass rate but populate a `scored` bucket. Add a `--strict` flag that
    promotes soft → gate.
  - `build_benchmark_report` exposes mean soft scores alongside pass rates.
- **Design:** default severity preserves current behavior (objective = gate; `judge` /
  `similarity` = soft) so existing manifests are unaffected.
- **Testing:** unit tests for gate-fail vs. soft-below-threshold vs. `--strict`; a
  regression test proving an unchanged manifest yields identical pass rates.

### 2.3 Tool replay
- **Goal:** deterministic, dependency-free re-runs by recording tool inputs+outputs.
- **Abstractions used/changed:** **runner-side**, not core grading. Recording slots into
  the runner alongside `write_trace_artifacts` (`:1495`): a `tool-replay.json` keyed by a
  per-tool `key`, with `sanitize` and `version`. Modes via `VITEST_EVALS`-style env
  (`auto`/`record`/`off`/`strict`) consumed by `run_codex` and the Pi/subagent runners.
- **Design:** orthogonal to the disk re-grade we already do; this makes the *agent run*
  reproducible. Most valuable for the subagent runner (2.7).
- **Testing:** record→replay round-trip on a mock runner; assert identical `output.md`
  and that `strict` errors on an unrecorded tool call.

### 2.4 OpenTelemetry GenAI normalization target
- **Goal:** make the trace adapter boundary a standard, not bespoke.
- **Abstractions used/changed:** `normalize_trace_record` (`:1373`) /
  `normalize_trace_records` (`:1447`) keep their inputs but emit OTel GenAI semantic-key
  attributes; `events.json` schema version bumps. Process/efficiency assertions
  (`process_or_efficiency_assertion_result`) read the new keys with back-compat fallbacks.
- **Design:** additive schema; old `events.json` still grades.
- **Testing:** extend the existing per-source normalization fixtures (codex/pi/jetty) to
  assert OTel keys; a back-compat test on a pre-bump `events.json`.

### 2.5 Dataset abstraction
- **Goal:** fan one case template over many rows instead of hand-authoring each case.
- **Abstractions used/changed:** new optional manifest construct (`datasets` + a case
  `template` referencing a dataset id). `iter_cases` (`:79`) materializes template×row →
  concrete cases before fan-out; `validate_manifest` validates rows and runs leakage lint
  per materialized case.
- **Design:** materialization happens early so every downstream stage (prepare/grade/
  report) is unchanged.
- **Testing:** test that N rows materialize N cases with stable ids; leakage lint fires on
  a leaky template.

### 2.6 Cross-run trend tracking
- **Goal:** watch lift/saturation/token drift over time.
- **Abstractions used/changed:** consumer of `build_benchmark_report`. Add an append-only
  history store and a `trend` subcommand that diffs successive `benchmark.json`s (reuse
  `compare_results` (`:2475`) logic).
- **Testing:** golden diff over two fixture reports.

### 2.7 Built-in subagent runner
- **Goal:** a runner needing no external CLI — dispatch a task to a Claude Code / Agent SDK
  subagent and emit the contract. Analogue of vitest-evals' OpenAI-Agents harness.
- **Abstractions used/changed:** new runner that consumes `prepared_task_rows` and writes
  the run-output contract + (optionally) `trace.jsonl` for `normalize_trace_records`.
  Mirrors `run_codex` structurally. Per-variant workspace isolation as in `run_pi_smoke.py`.
- **Design:** the difference from vitest-evals is in/out: their harness returns a typed
  value; ours writes files. The runner adapts the typed agent return into `output.md` +
  `metadata.json` + `events.json`.
- **Testing:** drive the runner against a mock subagent fn (no live agent); assert contract
  files + that `without_skill` workspace lacks skill files.

### 2.8 Interactive served report + richer artifacts
- **Goal:** feedback capture and image/pdf/xlsx rendering over static `render_viewer`.
- **Abstractions used/changed:** extend `render_viewer` (`:2506`) with a `serve` mode and
  artifact encoders; `eval-viewer/generate_review.py` (Anthropic) is the blueprint
  (`feedback.json` persistence, `--previous-workspace` diff).
- **Testing:** unit test the artifact-embedding/categorization and the feedback
  round-trip; HTML generation asserted by landmark strings (as today).

### 2.9 Iteration-over-time workflow
- **Goal:** `iteration-N/` dirs + previous-workspace diff + `feedback.json`.
- **Abstractions used/changed:** convention over `--runs` roots + the served viewer (2.8);
  reuse `compare_results` for the diff. No grading change.
- **Testing:** diff test across two iteration fixtures.

### 2.10 "Living eval" loop on saturation
- **Goal:** when a case saturates, propose a harder case.
- **Abstractions used/changed:** detection already exists (saturation/no-lift flags in
  `build_benchmark_report`). Add an **opt-in** `suggest-cases` command that reads flags and
  emits *candidate* prompts (generation behind an external model command, never core).
- **Testing:** the flag→candidate selection logic is tested deterministically; generation
  itself is mocked. Generated cases never auto-enter a manifest.

---

## Bucket 3 — bigger lift

### 3.1 Multi-turn / scripted cases
- **Goal:** conversational skills (send/respond sequences).
- **Abstractions changed (core contract):** a case gains an optional `turns` list; the
  run-output contract extends from one `output.md` to a turn-indexed transcript
  (`read_output_base` (`:1061`) and `discover_run_bases` learn turn layout); runners drive
  the sequence; `grade_case_variant` grades per-turn and aggregates.
- **Design:** keep single-shot the default; multi-turn is opt-in so existing manifests are
  untouched.
- **Testing:** fixture multi-turn run; assert per-turn grading and aggregate; back-compat
  test on a single-`output.md` case.

### 3.2 Per-model lift/pairing analysis
- **Goal:** the heavier reporting half of 2.1 — rank models by lift, flag where a model
  loses lift.
- **Abstractions used/changed:** extend `build_paired_summary` / `build_slice_summary` for
  the model axis; new viewer panel.
- **Testing:** report test over a 2-model × 2-variant fixture grid.

### 3.3 No-code template/registry eval definitions
- **Goal:** YAML+JSONL authoring without editing the JSON manifest by hand.
- **Abstractions used/changed:** a loader that compiles a template + dataset (2.5) into a
  manifest in memory before `validate_manifest`. Everything downstream is unchanged.
- **Testing:** compile-then-validate golden test; assert leakage lint still runs.

---

## Bucket 4 — adopt with care

These need a model and therefore stay **out of core grading**.

### 4.1 Embedding-backed `similarity`
- Same surface as 1.4 but with embeddings. Implemented like `script` assertions: skipped
  unless an explicit opt-in flag + external command is provided. **Testing:** mock the
  embedding command; assert skip-without-opt-in.

### 4.2 Auto-generation of harder cases (living-eval generation)
- The generation step behind 2.10. Separate opt-in command; output is *candidate* prompts
  a human reviews before they enter a manifest. **Testing:** mocked generator; assert no
  manifest is mutated automatically.

---

## Suggested sequencing

1. **2.1 multi-model fan-out** (priority; plumbing partway) + **2.2 scores/severity**
   (unlocks soft scoring that several later items rely on).
2. **1.1 judge presets**, **1.4 local similarity**, **1.2 reporters** — cheap wins on top
   of (2.2).
3. **2.4 OTel normalization** + **2.7 subagent runner** + **2.3 tool replay** — the runner
   layer, in that order (replay depends on a runner to host it).
4. **2.5 datasets** → **3.3 registry**, **2.8/2.9 viewer + iteration**, **2.6 trend**.
5. **3.1 multi-turn** and **3.2 per-model analysis** last (largest contract/report
   changes).
6. Bucket 4 items only as opt-in escapes, never blocking the above.
