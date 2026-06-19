# Eval-framework roadmap spec

This spec designs the backlog in [`TODO.md`](../TODO.md), section "Eval-framework parity &
ideas." Each item names its goal, the abstractions it uses or changes, the design, and how it
is tested. For what those abstractions are, read [`abstractions.md`](abstractions.md); for how
they connect, read [`architecture.md`](architecture.md).

One invariant governs every item: core grading stays local and deterministic and never calls a
model, and the harness never picks a model for the user. Anything that needs a model lives
behind an opt-in command or the external `--judge-cmd`. Two abstractions absorb most of the
work, so the spec returns to them often: the assertion result shape in `assertion_result`
(`skill_benchmark.py:1642`) and the fan-out in `prepared_task_rows` (`:314`).

## Testing baseline

Every item is exercised through `tests/test_skill_benchmark.py` with
`python3 -m unittest discover tests`. The house rule from CONTRIBUTING.md holds: no live model
or network call in a unit test. Use fixtures and mocked clients, following the `JettyClient`
mock already in the suite. Each item below names the fixtures or mocks it adds.

---

## Bucket 1 — near drop-in

### 1.1 Built-in judge presets (`factuality`, `tool_call`, `structured_output`)
- **Goal:** ship graders authors reach for often, so they stop hand-rolling rubrics.
- **Abstractions used or changed:** `tool_call` and `structured_output` are deterministic, so
  they become new types in `TEXT_ASSERTIONS` / `PROCESS_ASSERTIONS` and gain a branch in
  `assertion_result`. `tool_call` reuses `command_events` (`:1155`) and the `command_order`
  logic; `structured_output` extends `json_field_equals` with JSON-Schema validation.
  `factuality` adds no core code: it is a named rubric that `judge_prompt` (`:1767`) renders
  and still runs through `--judge-cmd`.
- **Design:** a preset expands to either a deterministic objective assertion or a `judge`
  assertion with a canned rubric and threshold. No new execution path.
- **Testing:** unit tests per deterministic preset across pass, fail, and missing-evidence. For
  `factuality`, a fixture `judge-results.jsonl` drives the existing merge path. No live model.

### 1.2 GitHub Actions reporter / JUnit XML
- **Goal:** put per-case pass/fail and lift on the pull request.
- **Abstractions used or changed:** a consumer of `build_benchmark_report` output. Add a
  `report` subcommand (or `--format junit|github` on `benchmark`) that serializes `results`,
  `case_flags`, and the paired summary. Grading is untouched.
- **Design:** JUnit writes one `<testcase>` per case/variant/run, with `evidence` on failures.
  GitHub writes a job-summary markdown plus annotations keyed to `case_id`.
- **Testing:** golden-file test on the XML and markdown from a fixed `benchmark.json`.

### 1.3 Judge config slot and the "judge is not the model under test" guard
- **Goal:** make an existing convention enforceable.
- **Abstractions used or changed:** read an optional `judge` block in the manifest; add a check
  in `audit_manifest_report` (`:2870`) comparing the declared judge model against `jetty.model`
  or the run metadata `model`.
- **Design:** warn by default, error under `--strict-judge`.
- **Testing:** unit tests for matching and differing model ids.

### 1.4 Local `similarity` scorer
- **Goal:** the deterministic middle between `regex` and a judge.
- **Abstractions used or changed:** a new type in `TEXT_ASSERTIONS`, implemented in
  `assertion_result`. It compares `output.md` to an `expected` string with
  `difflib.SequenceMatcher` and a threshold, and emits a `score` (see 2.2 for where scores
  surface).
- **Testing:** unit tests across threshold boundaries. No new dependency.

### 1.5 Workflow guide (done) and enforceable hints
- **Status:** `docs/authoring-evals.md` shipped, alongside `architecture.md` and
  `abstractions.md`.
- **Follow-on:** surface the guide's rules where they are checkable. Extend the messaging in
  `prompt_assertion_leakage_findings` (`:164`) and `fixture_recommendations` (`:2847`) to point
  at the relevant section.
- **Testing:** assert the new hint strings appear for crafted manifests.

### 1.6 `golden_output` assertion
- **Goal:** the strongest deterministic check there is, for cases with a known-correct artifact.
  In `adewale/pythonbyexample` and `adewale/xampler` an example *is* an eval case: an input, an
  expected output, and a check that the output still matches. The harness has no equivalent of
  "this output equals this reference."
- **Abstractions used or changed:** a new type in `TEXT_ASSERTIONS` (`:29`), implemented in
  `assertion_result` (`:1642`). It reads `output.md` (or a named artifact), applies an optional
  normalization (trim, collapse whitespace, or a named normalizer), and compares to a reference
  file under the manifest dir. Evidence is a unified diff on mismatch.
- **Design:** normalization is the whole game, so it is explicit and per-assertion, never
  implicit. Default is exact bytes; `normalize: "text"` collapses whitespace.
- **Testing:** unit tests for exact match, normalized match, and a mismatch whose evidence
  contains the diff.

### 1.7 Oracle-strength labeling + hygiene
- **Goal:** stop a case from looking solid when it passes only on weak checks. `adewale/xampler`
  labels every verifier "best no-lies" (real, byte-compared), "deliberate demo seam" (a marked
  stand-in), or "remote" (opt-in, real resources). The harness already spans this range but does
  not name it.
- **Abstractions used or changed:** an optional `oracle` tier on an assertion
  (`strong` / `demo` / `live`), defaulting by type (deterministic text/process are `strong`,
  `script` is `demo` unless marked, judge/live are `live`). `build_benchmark_report` (`:2182`)
  reports, per case, the share of its pass rate carried by `strong` oracles;
  `audit_manifest_report` (`:2870`) warns when a case passes only on weak ones. This extends
  leakage lint (`:164`) from prompts to oracles.
- **Testing:** unit tests for tier defaulting and for the "weak-oracle-only" warning on a crafted
  case.

---

## Bucket 2 — natural extension

### 2.1 Multi-model fan-out (priority)
- **Goal:** run the same cases across several models and compare lift per model. No surveyed
  framework does this; vitest-evals, eve, and viteval all push it onto the test runner.
- **Abstractions used or changed:** `prepared_task_rows` (`:314`) gains a `model` dimension
  beside `variant` and `run_number`. Each row carries its target `model`, and `run_dir` gains a
  model segment (`<case>/<model>/<variant>/run-<n>`), kept backward compatible when one model
  runs. Runners pass the row `model` through; per-run `model` already lands in `metadata.json`,
  so grading needs no change. `build_benchmark_report` (`:2182`) groups `by_variant` within
  `by_model`, and `build_paired_summary` (`:2119`) computes lift per (case, model).
- **Design:** model is a third axis, not a new variant. Variants stay orthogonal, giving a
  model-by-variant grid. CLI: `--models a,b,c` on `prepare`.
- **Testing:** a fan-out test asserting row count equals cases × variants × runs × models with
  correct `run_dir`s, and a report test over fixture runs under two models asserting per-model
  lift.

### 2.2 Graded scoring, severity, and statistical lift
- **Goal:** measure how much better or worse, not only pass or fail. A binary check saturates:
  once `with_skill` and `without_skill` both pass, a zero delta reads as no-regression evidence,
  not as proof of improvement. Graded scores keep measuring after the binary ceiling.
- **Reference implementation:** `adewale/anti-slop-writing` already runs this model in
  production. Its `evals/rewrite-evals.json` carries `graded_dimensions` and `dynamic_rubric`;
  `scripts/score_delta.py` gates on a statistical delta; reference-anchor cases set a
  no-regression floor. This spec ports those shapes into the harness rather than inventing new
  ones.
- **Abstractions used or changed:**
  - `assertion_result` (`:1642`) gains an optional `score` (0-1 or a normalized 1-5) and
    `severity` (`gate` or `soft`), read from `gate`/`soft`/`atLeast` on the assertion.
  - **Anchored `graded_dimensions`** as a `judge` assertion shape:
    `{name, scale: "1-5", rubric: "5 = …observable…; 1 = …observable…"}`. Anchors name what each
    score level looks like, so a judge scores against criteria, not a vibe. `judge_prompt`
    (`:1767`) renders the dimensions; the result carries per-dimension scores in `evidence`.
  - **`dynamic_rubric`** as a second `judge` shape: `{instruction, minimum_criteria}`. The judge
    drafts 3-5 case-specific criteria before grading and must meet at least `minimum_criteria`.
  - `grade_case_variant` (`:1877`) splits totals into gated and soft. A soft failure does not
    lower the pass rate; it fills a `scored` bucket. A `--strict` flag promotes soft to gate.
  - **Statistical lift** in `build_paired_summary` (`:2119`): alongside the raw delta, compute a
    significance test over the per-case graded scores (paired bootstrap or sign-flip
    permutation, mirroring `score_delta.py`), so lift is tested, not eyeballed.
  - **Reference-anchor floor:** an optional `reference_score` / `reference_graded_score` on a
    case sets a floor; scoring below it on any dimension is flagged as a regression.
- **Design:** default severity keeps current behavior (objective is a gate; `judge`,
  `similarity`, and graded dimensions are soft), so existing manifests score identically. This
  also gives the saturation flags a next move: when a binary case saturates, the report points
  to graded dimensions plus the statistical gate instead of stopping at the flag. Add a
  `structurally-pass-but-forgettable` flag (objective saturated, graded score low) — the
  signal `adewale/slide-maker` names as "competent but forgettable work."
- **Testing:** unit tests for gate-fail, soft-below-threshold, and `--strict`; a graded-dimension
  judge result merged from a fixture; a `dynamic_rubric` fixture asserting the `minimum_criteria`
  cutoff; a `score_delta` test with a known-significant and a known-flat fixture pair; a
  reference-floor regression test; and a regression test proving an unchanged binary manifest
  yields identical pass rates.

### 2.3 Tool replay
- **Goal:** deterministic re-runs that pay nothing for external dependencies, by recording tool
  inputs and outputs.
- **Abstractions used or changed:** this lives in the runner, not core grading. Recording sits
  beside `write_trace_artifacts` (`:1495`): a `tool-replay.json` keyed per tool, with
  `sanitize` and `version`. Modes (`auto`, `record`, `off`, `strict`) come from an environment
  variable that `run_codex` and the Pi and subagent runners read.
- **Design:** orthogonal to the disk re-grade the harness already does. Replay makes the agent
  run reproducible, where re-grading makes the scoring reproducible. Most useful for the
  subagent runner in 2.7.
- **Testing:** a record-then-replay round trip on a mock runner, asserting identical `output.md`
  and that `strict` errors on an unrecorded tool call.

### 2.4 OpenTelemetry GenAI normalization target
- **Goal:** make the trace adapter boundary a standard rather than a bespoke schema.
- **Abstractions used or changed:** `normalize_trace_record` (`:1373`) and
  `normalize_trace_records` (`:1447`) keep their inputs but emit OTel GenAI semantic-key
  attributes; the `events.json` schema version bumps. Process and efficiency assertions read the
  new keys with back-compat fallbacks.
- **Design:** additive schema. An old `events.json` still grades.
- **Testing:** extend the per-source normalization fixtures (codex, pi, jetty) to assert OTel
  keys, plus a back-compat test on a pre-bump `events.json`.

### 2.5 Dataset abstraction
- **Goal:** fan one case template over many rows instead of hand-authoring each case.
- **Abstractions used or changed:** a new optional manifest construct (`datasets`, plus a case
  `template` referencing a dataset id). `iter_cases` (`:79`) materializes template by row into
  concrete cases before fan-out; `validate_manifest` validates rows and runs leakage lint per
  materialized case.
- **Design:** materialization happens early, so prepare, grade, and report stay unchanged.
- **Testing:** N rows materialize N cases with stable ids, and leakage lint fires on a leaky
  template.

### 2.6 Cross-run trend tracking
- **Goal:** watch lift, saturation, and token drift over time.
- **Abstractions used or changed:** a consumer of `build_benchmark_report`. Add an append-only
  history store and a `trend` subcommand that diffs successive `benchmark.json` files, reusing
  the `compare_results` (`:2475`) logic.
- **Testing:** a golden diff over two fixture reports.

### 2.7 Built-in subagent runner
- **Goal:** a runner that needs no external CLI, dispatching a task to a Claude Code or Agent SDK
  subagent and writing the contract. This is the analogue of vitest-evals' OpenAI-Agents harness.
- **Abstractions used or changed:** a new runner consuming `prepared_task_rows` and writing the
  run-output contract plus, optionally, `trace.jsonl` for `normalize_trace_records`. It mirrors
  `run_codex` in structure and isolates per-variant workspaces as `run_pi_smoke.py` does.
- **Design:** the difference from vitest-evals is the boundary. Their harness returns a typed
  value in process; ours writes files. The runner adapts the typed agent return into `output.md`,
  `metadata.json`, and `events.json`.
- **Testing:** drive the runner against a mock subagent function, asserting the contract files
  exist and the `without_skill` workspace holds no skill files.

### 2.7b Held-out rubric discipline
- **Goal:** withhold grading criteria from generation, not only prompts, so a skill cannot teach
  to a rubric it never sees. `adewale/slide-maker` does this today: after structural gates pass,
  it dispatches a subagent with a rubric whose "criteria [are] deliberately absent from
  generation rules."
- **Abstractions used or changed:** mostly a discipline made first-class, not new machinery.
  `prepared_task_rows` (`:314`) already omits `review_rubric` from generation payloads unless
  `--include-answer-key`; extend `validate_manifest` (`:193`) to require that a `holdout`/
  `holdback` case's rubric stays out of the skill and public eval text, and pair it with the
  subagent judge from 2.7 and the graded scoring from 2.2. Track which rubrics were held out so
  the report can separate held-out scores from tune-visible ones.
- **Design:** held-out rubric scoring is where graded lift earns its keep — a deck that passes
  every structural gate but scores low on a held-out rubric is the "forgettable" case 2.2 flags.
- **Testing:** assert a held-out rubric never appears in a generation payload, and that a held-out
  judge result merges and is reported separately from tune-visible scores.

### 2.8 Interactive served report and richer artifacts
- **Goal:** capture feedback in the browser and render image, PDF, and xlsx artifacts, beyond the
  static `render_viewer`.
- **Abstractions used or changed:** extend `render_viewer` (`:2506`) with a `serve` mode and
  artifact encoders. Anthropic's `eval-viewer/generate_review.py` is the blueprint, including
  `feedback.json` persistence and a `--previous-workspace` diff.
- **Testing:** unit-test the artifact embedding and categorization and the feedback round trip;
  assert HTML by landmark strings, as today.

### 2.9 Iteration-over-time workflow
- **Goal:** `iteration-N/` directories, a previous-workspace diff, and `feedback.json`.
- **Abstractions used or changed:** a convention over `--runs` roots plus the served viewer from
  2.8, reusing `compare_results` for the diff. Grading is untouched.
- **Testing:** a diff test across two iteration fixtures.

### 2.10 "Living eval" loop on saturation
- **Goal:** when a case saturates, propose a harder case.
- **Abstractions used or changed:** detection already exists in the saturation and no-lift flags
  of `build_benchmark_report`. Add an opt-in `suggest-cases` command that reads the flags and
  emits candidate prompts, with the generation step behind an external model command.
- **Testing:** the flag-to-candidate selection is tested deterministically; generation is mocked,
  and a generated case never enters a manifest on its own.

---

## Bucket 3 — bigger lift

### 3.1 Multi-turn / scripted cases
- **Goal:** evaluate conversational skills across a send/respond sequence.
- **Abstractions changed (core contract):** a case gains an optional `turns` list. The
  run-output contract grows from one `output.md` to a turn-indexed transcript, so
  `read_output_base` (`:1061`) and `discover_run_bases` learn the turn layout; runners drive the
  sequence; `grade_case_variant` grades per turn and aggregates.
- **Design:** single-shot stays the default, so existing manifests are untouched.
- **Testing:** a fixture multi-turn run asserting per-turn grading and aggregate, plus a
  back-compat test on a single-`output.md` case.

### 3.2 Per-model lift and pairing analysis
- **Goal:** the heavier reporting half of 2.1: rank models by lift and flag where a model loses
  it.
- **Abstractions used or changed:** extend `build_paired_summary` and `build_slice_summary` for
  the model axis, plus a viewer panel.
- **Testing:** a report test over a two-model by two-variant fixture grid.

### 3.3 No-code template/registry eval definitions
- **Goal:** author in YAML and JSONL without editing the JSON manifest by hand.
- **Abstractions used or changed:** a loader that compiles a template plus a dataset (2.5) into a
  manifest in memory before `validate_manifest`. Everything downstream is unchanged.
- **Testing:** a compile-then-validate golden test that confirms leakage lint still runs.

---

## Bucket 4 — adopt with care

These need a model, so they stay out of core grading.

### 4.1 Embedding-backed `similarity`
- The surface of 1.4, but with embeddings. Implement it like a `script` assertion: skipped unless
  an explicit opt-in flag and an external command are present.
- **Testing:** mock the embedding command and assert skip-without-opt-in.

### 4.2 Auto-generation of harder cases
- The generation step behind 2.10. A separate opt-in command whose output is candidate prompts a
  person reviews before they enter a manifest.
- **Testing:** a mocked generator, asserting no manifest is mutated automatically.

---

## Sequencing

1. **2.1 multi-model fan-out** (priority, plumbing partway) and **2.2 scores and severity**,
   because the soft-scoring several later items lean on lands here.
2. **1.1 judge presets**, **1.4 local similarity**, **1.2 reporters**: cheap wins on top of 2.2.
3. **2.4 OTel normalization**, then **2.7 subagent runner**, then **2.3 tool replay**, in that
   order, because replay needs a runner to host it.
4. **2.5 datasets** into **3.3 registry**; **2.8 and 2.9 viewer and iteration**; **2.6 trend**.
5. **3.1 multi-turn** and **3.2 per-model analysis** last, since they change the contract and the
   report the most.
6. Bucket 4 items only as opt-in escapes, never blocking the work above.

The order is not arbitrary: it builds the two shared abstractions first (the scored assertion and
the model axis), then everything else attaches to a surface that already exists.
