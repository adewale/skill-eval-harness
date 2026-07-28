# Eval-framework roadmap spec

Status: implemented. [`TODO.md`](../TODO.md) tracks per-item status; the tests live in
`tests/test_confidence_floor.py` and the subject files (`tests/test_grading.py`,
`tests/test_reporting.py`, `tests/test_runners.py`, `tests/test_manifest.py`,
`tests/test_judging.py`, `tests/test_stats.py`), and the migration
path in [`migrating-evals.md`](migrating-evals.md). The prose below is the design as
written before implementation — premises such as "X has no test today" describe that
starting point.

This spec designs the backlog in [`TODO.md`](../TODO.md), section "Eval-framework parity &
ideas." Each item names its goal, the abstractions it uses or changes, the design, and how it
is tested. For what those abstractions are, read [`abstractions.md`](abstractions.md); for how
they connect, read [`architecture.md`](architecture.md).

One invariant governs every item: core grading stays local and deterministic and never calls a
model, and the harness never picks a model for the user. Anything that needs a model lives
behind an opt-in command or the external `--judge-cmd`. Two abstractions absorb most of the
work, so the spec returns to them often: the assertion result shape in `assertion_result`
(`skill_benchmark.py:5709`) and the fan-out in `prepared_task_rows` (`:806`).

## Testing baseline

Every item is exercised through `tests/test_skill_benchmark.py` with
`python3 -m unittest discover tests`. The house rule from CONTRIBUTING.md holds: no live model
or network call in a unit test. Use fixtures and mocked clients, following the `JettyClient`
mock already in the suite. Each item below names the fixtures or mocks it adds.

---

## Confidence floor — the smallest set that makes a reported lift believable

These changes test the harness; they do not eval a skill. The difference is the one in
[`evals-are-not-tests.md`](evals-are-not-tests.md): an eval samples a stochastic model and scores a
difference, so one run is a sample, while a test runs one program against a known answer, so one run
is a verdict. Every item here is a test in that sense: deterministic, local, no model call, settled
in one run. None of them adds a new eval. Together they make executable the guarantees
`evals-are-not-tests.md` states in prose — it claims the harness reports an honest lift, and these
tests are where that claim is checked rather than trusted.

The buckets below extend what the harness can measure. This floor decides whether the number it
already prints can be believed. The harness reports one thing: lift, `with_skill` minus
`without_skill` on the same case (`build_paired_summary` (`:8033`)). That number is believable only
when three preconditions hold, each intended today, none enforced, and each restating a claim from
`evals-are-not-tests.md`:

1. the detectors do not lie, by firing falsely or staying silent falsely (CF.1; "assert the
   *behavior*, not one spelling");
2. the `without_skill` baseline cannot read the skill, or it "masquerades as a skilled run" (CF.2);
3. grading is deterministic, local, and model-free, so a run dir grades the same way every time
   (CF.3 and CF.4; "deterministic local grading … with no model call").

These items are fixtures and tests, not engine changes, which is what keeps the set small, and none
touches the lift/splits/ablations moat. Sequence them before the buckets, because a graded (2.2) or
multi-model (2.1) feature built on unverified detectors only scales an unverified result.

### CF.1 — Detector meta-fixtures (the keystone)
- **Goal:** prove no detector manufactures or erases lift. A false-positive `contains` or a
  false-silent `excludes_any` shifts a `with_skill` or `without_skill` pass rate, inventing or
  hiding the quantity the tool exists to measure.
- **Abstractions used or changed:** none in the engine. Adds
  `tests/fixtures/detectors/<detector_id>/should-fire.*` and `should-pass.*` that exercise
  `assertion_result` (`:5709`) directly. That function has no direct unit test today, and its
  `contains_any`, `excludes_any`, and `not_regex` branches go unexercised even though their types
  are declared in the assertion registries (`TEXT_ASSERTIONS:112`).
- **Design:** one table-driven test loads every pair and asserts the detector fires on `should-fire`
  and stays silent on `should-pass`. A `should-pass` twin is mandatory: it enforces the
  false-positive bar `authoring-evals.md` already sets for skill authors ("check both presence and
  absence"). Each `should-pass` set includes a case where the baseline echoes the prompt, because a
  detector that passes on echoed prompt text is a false oracle; this ties the fixtures to leakage
  lint (`prompt_assertion_leakage_findings` (`:412`)). Every detector bug then becomes a permanent
  fixture pair, and `test_command_assertions_match_command_inputs_not_outputs` is the first.
- **Why it is the keystone:** the fixture pair is also the registration contract. It is what lets a
  harvested or third-party detector be trusted on entry, so it gates the exapted detector library
  (TODO, end of 2026), and it raises the oracle-strength ladder (1.7) so that "strong" means
  fixture-verified, not only deterministic.
- **Testing:** the fixtures are the test. A meta-test asserts every name in `OBJECTIVE_ASSERTIONS`
  (`:140`) has a fixture pair, so a new detector cannot land unverified.

### CF.2 — One cross-runner baseline-isolation invariant
- **Goal:** prove the baseline is skill-free by construction, so a measured lift is the skill's
  effect and not an artifact of a baseline that grepped the skill out of the source tree.
- **Abstractions used or changed:** one invariant over every runner's `without_skill` workspace. It
  folds today's per-runner checks — `test_pi_smoke_workspace_omits_skill_for_without_skill` and the
  Jetty `without_skill` mount test — into a single guard the run-output contract must satisfy.
- **Design:** build each runner's `without_skill` workspace from a fixture manifest and assert the
  skill files are unreachable by read, find, and grep. A new runner such as the subagent runner
  (2.7) inherits the check by registering its workspace builder, so adding a runner cannot quietly
  reintroduce the leak.
- **Testing:** the invariant test, parameterized over the registered runners.

### CF.3 — Re-grade idempotence
- **Goal:** prove the claim the cheap-re-grade workflow rests on — grading reads only from disk and
  is deterministic.
- **Abstractions used or changed:** none. A test over a fixture run set.
- **Design:** grade the same run directory twice and assert a byte-identical `benchmark.json`
  (modulo an explicit timestamp field). Any hidden nondeterminism — dict ordering, set iteration in
  flag computation, a stray clock read — surfaces here rather than as drift between two real runs.
- **Testing:** the idempotence test over an existing fixture run set.

### CF.4 — A guard that the core grade path calls no model and no network
- **Goal:** make the governing invariant — "core grading stays local and deterministic and never
  calls a model" — executable rather than aspirational.
- **Abstractions used or changed:** none. A test-time guard around `grade_case_variant` (`:7217`).
- **Design:** patch `urllib` and `subprocess` to raise, then grade a fixture covering every
  objective family (text, process, efficiency) and assert it completes. The sanctioned exceptions —
  `script` oracles and `judge` plumbing — are excluded from this path by design and keep their own
  opt-in tests (`--allow-scripts`, `--judge-cmd`). The guard fails the day a detector
  reaches for the network or a subprocess inside the grade path.
- **Testing:** the guard test across the objective families.

### Boundary
The set stops here deliberately. Report-level correctness — whether the saturation, no-lift, flaky,
and negative-delta flags (`build_benchmark_report` (`:9033`)) are computed right — sits a layer
above the raw measurement, where the golden-report tests the buckets already plan (1.2, 2.6) cover
it. CF.1–CF.4 are the floor underneath that work: once they pass, a printed lift carries a checked
measurement, and every feature in the buckets builds on a number that has been verified rather than
assumed.

---

## Bucket 1 — near drop-in

### 1.1 Built-in judge presets (`factuality`, `tool_call`, `structured_output`)
- **Goal:** ship graders authors reach for often, so they stop hand-rolling rubrics.
- **Abstractions used or changed:** `tool_call` and `structured_output` are deterministic, so
  they become new types in `TEXT_ASSERTIONS` / `PROCESS_ASSERTIONS` and gain a branch in
  `assertion_result`. `tool_call` reuses `command_events` (`:2999`) and the `command_order`
  logic; `structured_output` extends `json_field_equals` with JSON-Schema validation.
  `factuality` adds no core code: it is a named rubric that `judge_prompt` (`:6042`) renders
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
  in `audit_manifest_report` (`:10974`) comparing the declared judge model against `jetty.model`
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
  `prompt_assertion_leakage_findings` (`:412`) and `fixture_recommendations` (`:10700`) to point
  at the relevant section.
- **Testing:** assert the new hint strings appear for crafted manifests.

### 1.6 `golden_output` assertion
- **Goal:** the strongest deterministic check there is, for cases with a known-correct artifact.
  In `adewale/pythonbyexample` and `adewale/xampler` an example *is* an eval case: an input, an
  expected output, and a check that the output still matches. The harness has no equivalent of
  "this output equals this reference."
- **Abstractions used or changed:** a new type in `TEXT_ASSERTIONS` (`:112`), implemented in
  `assertion_result` (`:5709`). It reads `output.md` (or a named artifact), applies an optional
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
  `script` is `demo` unless marked, judge/live are `live`). `build_benchmark_report` (`:9033`)
  reports, per case, the share of its pass rate carried by `strong` oracles;
  `audit_manifest_report` (`:10974`) warns when a case passes only on weak ones. This extends
  leakage lint (`:164`) from prompts to oracles.
- **Strongest tier — the rendered-artifact oracle:** the top of the ladder is an oracle that
  builds or renders the artifact and inspects the result, not the source text.
  `adewale/swiss-poster-skill`'s `rendered_poster_oracle.py` runs headless Chrome and audits the
  rendered pixels (viewport bounds, contrast, overflow). Mark these `strong`; they are slow, so
  they lean on `timeout_s` and `--allow-scripts`, both already in the `script` contract.
- **Testing:** unit tests for tier defaulting and for the "weak-oracle-only" warning on a crafted
  case.

### 1.8 Graded script oracles
- **Goal:** let a deterministic oracle report degree, not just pass/fail. `adewale/swiss-poster-skill`
  splits "good poster" into seven `script` oracles, but each is forced all-or-nothing by the
  exit-code-only contract: a poster with six of seven drama carriers fails `drama_oracle` exactly
  like one with zero. That is the binary-saturation problem inside a single oracle.
- **Abstractions used or changed:** extend `run_script_assertion` (`:5599`) and
  `assertion_result` (`:5709`) to optionally parse a score from the oracle's stdout — a JSON line
  such as `{"score": 6, "max_score": 7}` (normalized to 0-1) — beside the existing
  `pass_exit_code`. The parsed score flows into the `score`/`severity` channel from 2.2, so a
  script oracle becomes a graded dimension while staying fully deterministic and model-free.
- **Design:** the score channel is additive and opt-in. An oracle that prints no score line keeps
  today's pure pass/fail behavior, so existing manifests are unaffected. `pass_exit_code` still
  decides `passed`; the score only feeds graded lift and the saturation handoff.
- **Testing:** unit tests for an oracle that emits a score line (graded), one that emits none
  (binary, unchanged), and a malformed line (ignored, falls back to exit code). Reuse the
  existing `script`-assertion fixtures under `--allow-scripts`.

### 1.9 Staleness and pruning hygiene
- **Goal:** keep the suite lean. `howtoeval.com` argues 20 high-signal cases beat 200 edge cases,
  and that an eval which has not failed in months is either testing nothing or the agent improved
  past it — either way, question it. This is the inverse of the saturation flag: saturation marks
  a case as too easy *now*; staleness marks a case that has never discriminated *over time*.
- **Abstractions used or changed:** reads the cross-run history from 2.6. A `prune` report (or a
  flag in `build_benchmark_report` (`:9033`)) marks a case a removal candidate when, across the
  last N runs, it never failed and never showed lift (`with_skill` == `without_skill` every time).
  `audit_manifest_report` (`:10974`) lists the candidates; removal stays a human decision.
- **Design:** the harness suggests, never deletes. A case may be kept deliberately as a
  regression guard even when stale; the report says so rather than acting.
- **Depends on:** 2.6 (needs run history to judge "never failed over time").
- **Testing:** a fixture history where one case is always-flat and one discriminates; assert only
  the flat one is flagged, and that a single run never flags anything.

---

## Bucket 2 — natural extension

### 2.1 Multi-model fan-out (priority)
- **Goal:** run the same cases across several models and compare lift per model. No surveyed
  framework does this; vitest-evals, eve, and viteval all push it onto the test runner.
- **Abstractions used or changed:** `prepared_task_rows` (`:806`) gains a `model` dimension
  beside `variant` and `run_number`. Each row carries its target `model`, and `run_dir` gains a
  model segment (`<case>/<model>/<variant>/run-<n>`), kept backward-compatible when one model
  runs. Runners pass the row `model` through; per-run `model` already lands in `metadata.json`,
  so grading needs no change. `build_benchmark_report` (`:9033`) groups `by_variant` within
  `by_model`, and `build_paired_summary` (`:8033`) computes lift per (case, model).
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
  - `assertion_result` (`:5709`) gains an optional `score` (0-1 or a normalized 1-5) and
    `severity` (`critical` / `gate` / `soft`), read from `critical`/`gate`/`soft`/`atLeast` on the
    assertion.
  - **`critical` (absorbing-barrier) tier — valley-dodging.** From the Jetty "valley-dodging"
    post: some failures cannot be averaged away. "Once an agent can reach a state like that, the
    average stops being a number you can trust — sequence is what does the damage." A `critical`
    assertion failing **vetoes the case** and no graded score offsets it; a poster scoring 5/5 on
    drama still fails if it wrote outside the results directory. This sits *above* `gate`: a gate
    lowers a pass rate, a critical failure is excluded from averaging entirely and surfaced on its
    own. These map to the safeguards skills already encode by hand — `adewale/guardrails-skill`'s
    "never write outside the results directory," "do not report success if a check failed."
    Declare them per case as explicit failure modes, not buried in a pass rate.
  - **Anchored `graded_dimensions`** as a `judge` assertion shape:
    `{name, scale: "1-5", rubric: "5 = …observable…; 1 = …observable…"}`. Anchors name what each
    score level looks like, so a judge scores against criteria, not a vibe. `judge_prompt`
    (`:6042`) renders the dimensions; the result carries per-dimension scores in `evidence`.
  - **`dynamic_rubric`** as a second `judge` shape: `{instruction, minimum_criteria}`. The judge
    drafts 3-5 case-specific criteria before grading and must meet at least `minimum_criteria`.
  - `grade_case_variant` (`:7217`) splits totals into critical, gated, and soft. A `critical`
    failure vetoes the case; a `gate` failure lowers the pass rate; a `soft` failure lowers
    neither and fills a `scored` bucket. A `--strict` flag promotes soft to gate.
  - **Statistical lift** in `build_paired_summary` (`:8033`): alongside the raw delta, compute a
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
- **Floor-raising and valley-dodging — a mean hides two things.** Graded scores measure *how much
  better* and feed lift; they are not the headline. Two failure modes must never be averaged into
  them. Per `howtoeval.com`, the first question is *which cases fail*, so the report keeps the
  binary failing-case flags (`with-skill-failed`, negative-delta) primary. Per the valley-dodging
  post, a `critical` failure is worse still: it is excluded from the mean entirely and surfaced on
  its own, because one catastrophe across twenty runs is a catastrophe, not a 95%. A high graded
  mean must never hide a failing case, and never hide an absorbing-barrier hit.
- **Caution — brittle solutions.** Stacking `critical` prohibitions backfires: an agent gets
  obstinate or honors the rule while missing the objective. Pair every `critical` assertion with
  negative / false-positive cases that prove the skill still does the reasonable thing, the same
  way trigger evals guard against over-triggering.
- **Testing:** unit tests for gate-fail, soft-below-threshold, and `--strict`; a `critical`-fail
  test proving the case is vetoed and excluded from the graded mean even when other scores are
  perfect; a graded-dimension judge result merged from a fixture; a `dynamic_rubric` fixture
  asserting the `minimum_criteria` cutoff; a `score_delta` test with a known-significant and a
  known-flat fixture pair; a reference-floor regression test; and a regression test proving an
  unchanged binary manifest yields identical pass rates.

### 2.3 Tool replay
- **Goal:** deterministic re-runs that pay nothing for external dependencies, by recording tool
  inputs and outputs.
- **Abstractions used or changed:** this lives in the runner, not core grading. Recording sits
  beside `write_trace_artifacts` (`:4242`): a `tool-replay.json` keyed per tool, with
  `sanitize` and `version`. Modes (`auto`, `record`, `off`, `strict`) come from an environment
  variable that `run_codex` and the Pi and subagent runners read.
- **Design:** orthogonal to the disk re-grade the harness already does. Replay makes the agent
  run reproducible, where re-grading makes the scoring reproducible. Most useful for the
  subagent runner in 2.7.
- **Testing:** a record-then-replay round trip on a mock runner, asserting identical `output.md`
  and that `strict` errors on an unrecorded tool call.

### 2.4 OpenTelemetry GenAI normalization target
- **Goal:** make the trace adapter boundary a standard rather than a bespoke schema.
- **Abstractions used or changed:** `normalize_trace_record` (`:3771`) and
  `normalize_trace_records` (`:3968`) keep their inputs but emit OTel GenAI semantic-key
  attributes; the `events.json` schema version bumps. Process and efficiency assertions read the
  new keys with backward-compatible fallbacks.
- **Design:** additive schema. An old `events.json` still grades.
- **Testing:** extend the per-source normalization fixtures (codex, pi, jetty) to assert OTel
  keys, plus a backward-compatibility test on a pre-bump `events.json`.

### 2.5 Dataset abstraction
- **Goal:** fan one case template over many rows instead of hand-authoring each case.
- **Abstractions used or changed:** a new optional manifest construct (`datasets`, plus a case
  `template` referencing a dataset id). `iter_cases` (`:311`) materializes template by row into
  concrete cases before fan-out; `validate_manifest` validates rows and runs leakage lint per
  materialized case.
- **Design:** materialization happens early, so prepare, grade, and report stay unchanged.
- **Representativeness guard:** fan-out makes it cheap to balloon a suite, the failure mode 1.9
  guards against. Pair the two: a generated row earns its place by discriminating, and flat rows
  are pruned. The point of templating is coverage of real variation, not case count.
- **Testing:** N rows materialize N cases with stable ids, and leakage lint fires on a leaky
  template.

### 2.6 Cross-run trend tracking
- **Goal:** watch lift, saturation, and token drift over time.
- **Abstractions used or changed:** a consumer of `build_benchmark_report`. Add an append-only
  history store and a `trend` subcommand that diffs successive `benchmark.json` files, reusing
  the `compare_results` (`:9505`) logic.
- **Severity-weighted ranking (from the macro-evals notebook):** when surfacing recurring
  failures across runs, rank them by `prevalence × severity`, not raw count, so a rare but severe
  failure outranks a common trivial one. This is the floor-raising principle made quantitative;
  it reuses the per-assertion severity from 2.2.
- **Testing:** a golden diff over two fixture reports, plus a ranking test where a low-frequency
  high-severity failure outranks a high-frequency low-severity one.

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
  `prepared_task_rows` (`:806`) already omits `review_rubric` from generation payloads unless
  `--include-answer-key`; extend `validate_manifest` (`:585`) to require that a `holdout`/
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
- **Abstractions used or changed:** extend `render_viewer` (`:10220`) with a `serve` mode and
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
- **Representativeness guard:** a suggested case is a candidate, not an addition. `howtoeval.com`'s
  rule holds — do not add a case for every failure unless it represents a real pattern. The loop
  runs both directions: 2.10 proposes hard cases, 1.9 prunes flat ones, so the suite tracks the
  failure surface instead of growing without bound.
- **Testing:** the flag-to-candidate selection is tested deterministically; generation is mocked,
  and a generated case never enters a manifest on its own.

---

## Bucket 3 — bigger lift

### 3.1 Multi-turn / scripted cases
- **Goal:** evaluate conversational skills across a send/respond sequence.
- **Abstractions changed (core contract):** a case gains an optional `turns` list. The
  run-output contract grows from one `output.md` to a turn-indexed transcript, so
  `read_output_base` (`:2852`) and `discover_run_bases` learn the turn layout; runners drive the
  sequence; `grade_case_variant` grades per turn and aggregates.
- **Design:** single-shot stays the default, so existing manifests are untouched.
- **Testing:** a fixture multi-turn run asserting per-turn grading and aggregate, plus a
  backward-compatibility test on a single-`output.md` case.

### 3.2 Per-model lift and pairing analysis
- **Goal:** the heavier reporting half of 2.1: rank models by lift and flag where a model loses
  it.
- **Abstractions used or changed:** extend `build_paired_summary` and `build_slice_summary` for
  the model axis, plus a viewer panel.
- **Slice-lift concentration (from the macro-evals notebook):** compute, per slice, where lift (or
  a failure) concentrates — `slice share ÷ overall share`, the macro-eval `lift` metric one level
  up from per-case lift. `build_slice_summary` (`:8166`) already groups by domain/difficulty/
  trigger/goal, so this is a ratio over groups it already forms, not new plumbing.
- **Testing:** a report test over a two-model by two-variant fixture grid, plus a concentration
  test asserting a failure confined to one slice scores a high ratio there.

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

## Explicitly out of scope

**Macro-eval clustering and pattern discovery** (OpenAI's macro-evals notebook: embed traces ->
UMAP -> HDBSCAN -> TF-IDF labels -> backward-walking "suspect" diagnosis). That layer sits *above*
this harness: it mines thousands of *production* traces for unlabeled failure patterns. The
harness is a pre-production, paired-comparison tool, so building clustering would pull it off the
lift moat toward a different product for a different stage. The spec takes the two transferable
ideas — severity-weighted failure ranking (into 2.6) and slice-lift concentration (into 3.2) — and
leaves the discovery engine out. The notebook's own caution is why: a discovered pattern "is not
automatically a defect; it is where inspection begins," and "causality inference remains
speculative" — the same flag-is-where-you-look discipline the saturation flags already hold.

---

## Sequencing

1. **Cheap wins with no dependency:** **1.6 golden_output**, **1.2 reporters**, **1.3 judge
   guard**. None need the score channel, so they can land first.
2. **The two shared abstractions:** **2.1 multi-model fan-out** (priority, plumbing partway) and
   **2.2 graded scoring and severity**. Everything scored leans on 2.2, so it gates the next
   group.
3. **On top of 2.2's score channel:** **1.1 judge presets**, **1.4 local similarity**,
   **1.8 graded script oracles**, and **1.7 oracle-strength** (its strong-oracle share reads best
   once scores exist). **1.8 must follow 2.2** — it routes the oracle's stdout score into the
   `score`/`severity` channel that 2.2 introduces, so building it earlier would have nowhere to
   put the score.
4. **The runner layer:** **2.4 OTel normalization**, then **2.7 subagent runner**, then
   **2.3 tool replay** (replay needs a runner to host it), then **2.7b held-out rubric** (needs
   both 2.2's graded scores and 2.7's subagent judge).
5. **2.5 datasets** into **3.3 registry**; **2.8 and 2.9 viewer and iteration**; then **2.6 trend**
   and, on top of its history, the prune/grow pair **1.9 staleness** and **2.10 living-eval**.
6. **3.1 multi-turn** and **3.2 per-model analysis** last, since they change the contract and the
   report the most.
7. Bucket 4 items only as opt-in escapes, never blocking the work above.

The order is not arbitrary: it builds the two shared abstractions first (the scored assertion and
the model axis), then attaches everything else to a surface that already exists. The dependencies
are stated per item — 1.8 after 2.2, 1.9 after 2.6, 2.7b after 2.2 and 2.7, 2.3 after a runner,
3.3 after 2.5 — and the rest of the order is preference.

---

## Migration: upgrading existing evals

Several items above change the manifest: the model axis (2.1), graded scoring and the
`graded_dimensions` / `dynamic_rubric` shapes (2.2), `golden_output` (1.6), oracle tiers (1.7),
and graded script oracles (1.8). The author already runs eight manifests
(`examples/adewale-workspace/all-manifests.txt`), and external skills such as
`adewale/swiss-poster-skill` run their own. None of them should need a hand-edit to keep working,
and upgrading to the new features should be something an agent can drive.

### Two principles, borrowed

- **Additive and backward-compatible (LangSmith's incremental adoption).** LangSmith's Vitest/Jest
  integration lets you wrap existing tests rather than rewrite them: assertions still produce a
  `pass`, and a scored evaluator is an opt-in `wrapEvaluator` returning `{key, score}`. The harness
  follows the same rule: every new field is optional with a behavior-preserving default, so a
  `version: 1` manifest grades identically after the upgrade. A binary assertion without a
  `severity` stays a gate; a `script` oracle that prints no score line stays pass/fail.
- **Agent-driven, not a rigid codemod (pi.dev's approach).** pi.dev ships no migration tool; its
  stance is "ask the agent to convert it." The mechanical rewrites can be automated, but the
  judgment calls (which binary assertions deserve graded dimensions, which oracles are `strong`)
  are left to an agent following a guide. We split migration the same way: a command for the
  mechanical part, a guide for the judgment.

### What ships

- **`skill-benchmark migrate <manifest>`**: a command that bumps `version`, applies the mechanical
  rewrites, and writes a diff plus a checklist of judgment calls it deliberately did not make.
  - Mechanical: bump `version` 1 -> 2; stamp default `severity: gate` on objective assertions and
    `soft` on `judge`; default each assertion's `oracle` tier by type; leave a `# TODO: graded?`
    marker beside binary `judge` rubrics.
  - Left for the agent/human: turning a flat `rubric: [...]` into anchored `graded_dimensions`,
    choosing reference-anchor floors, and marking which `script` oracles are demo seams. The
    checklist names each, with a pointer to the relevant spec section.
  - `--check` runs it dry (no writes), mirroring LangSmith's `LANGSMITH_TEST_TRACKING=false`.
- **`docs/migrating-evals.md`**: a versioned, agent-runnable guide. It states, per manifest
  version, what changed, what `migrate` does automatically, and the ordered steps an agent takes to
  finish the judgment calls. An agent points at a repo, runs `migrate --check`, reads the
  checklist, edits the manifest, and re-validates. This is the narrow, justified form of the
  punted "ship-as-skill" idea: a migration runbook, not a general authoring skill.

### Abstractions used or changed

- `version` on the manifest (`validate_manifest:585`) becomes meaningful: `validate` accepts both
  1 and 2, and warns (not errors) on a `version: 1` manifest once 2.2 has landed, pointing at
  `migrate`.
- No grading abstraction changes for migration itself; it is a source-rewrite plus a guide.

### Testing

- A golden round-trip: a `version: 1` fixture manifest through `migrate` to `version: 2`, then
  `validate`, asserting the diff matches and the mechanical defaults are stamped.
- A backward-compatibility test proving the pre-migration manifest still grades to identical pass
  rates under the new code (the same regression test 2.2 requires).
- A `--check` test asserting no file is written and the judgment-call checklist lists every binary
  `judge` rubric and every `script` oracle.
