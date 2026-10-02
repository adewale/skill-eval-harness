# Upgrading skill-eval-harness

Each section covers one released-version boundary. Follow every section after your installed version; do not skip an intermediate artifact migration.

## 0.6.0 → unreleased (`main`)

These changes merged after the 0.6.0 tag. This section becomes the next release's boundary when it
is tagged; 0.6.0 users follow it before installing from `main`. No manifest or telemetry migration
is needed and saved runs stay readable. What changes is which runs count, which pairs form, how a
few report and audit fields read, which saved trigger reports a comparison accepts, and which
internal Python names still exist. The changelog's [Unreleased](../CHANGELOG.md#unreleased)
section lists every change.

### Runtime dependency

Python 3.10, 3.11, and 3.12 remain supported. The runtime now includes PyYAML plus exact-pinned
`regex==2026.7.19`; the latter supplies one Unicode semantics and a native timeout for every
`rendered-v1` regex. `comparison: "exact"` keeps stdlib `re` behavior.

### Typed boundary tightening

The runtime now parses prepared tasks, pair identities, artifact/event logs, judge tasks, report
attempts, and CLI input into immutable domain values before semantic use. This is intentionally
stricter at programmatic and persisted boundaries:

- schema versions must be exact JSON integers; `true`, `1.0`, and `"1"` are invalid;
- nested assertion, conversation, event, and compatibility argument data is detached from its
  source and recursively frozen;
- report attempts require a unique stable case/model/variant/repetition identity, and each rate has
  its own availability cohort; a complete row count no longer permits a survivor-only metric mean;
- judge process completion, provider-response failure, and verdict parsing remain distinct, so an
  exit-zero protocol failure keeps return code zero but cannot become a complete observation; and
- CLI values are validated before dispatch. Existing handlers still receive the same Namespace
  shape through the named legacy adapter, and meaningful zero values such as `--limit 0` and
  `--max-references 0` remain valid.

Custom Python adapters should build `ProcessInvocationPlan` and use `run_argv_capture(plan)`.
Code that supplied parallel argv/cwd/environment/timeout arguments to that internal helper must
migrate. Keep the old environment for rollback; regenerate stale prepared tasks and benchmark
reports after correcting rejected rows. Do not edit digests, availability, or repetition ids merely
to make old artifacts pass the new constructors.

### Trigger harness identity

Trigger `harness_identity` names a conservative audited module-level inventory instead of every
packaged module. Standalone report, judge, CLI, and unsupported-provider modules are excluded, but
`skill_benchmark.py` remains a monolith shared by trigger and non-trigger orchestration. Any edit to
that file still invalidates trigger identity until those owners are extracted into separate modules.

- Version 2 replaced the overbroad version-1 set. Version-1 trigger reports must be regenerated
  before a new causal comparison; this deliberate incompatibility refuses to guess equivalence.
- Version 3 adds `completion_contracts.py`, `observation_contracts.py` and `content_digests.py`,
  which now decide how a trigger run ends and how its skill tree is hashed. `trigger-compare`
  refuses a version-2 report with a message naming both versions; regenerate the baseline and
  the ablation report together, since a comparison needs both arms from the same harness.

### Run metadata and scoring

- Native answer runs now record `stop_class`, `stop_reason`, `stop_source`, `requested_model`,
  `served_model`, `served_models`, `served_model_check`, and `effort` in `metadata.json`. Runs
  recorded earlier carry none of them, grade as before, and appear as `unrecorded` in the new
  `run_endings` report block. A backend that exposes no evidence records `unavailable`, and an
  unpinned effort records `applied_by: "backend_default"`; the values are defined in
  [`vocabulary.md`](vocabulary.md#run-artifacts).
- A run tree written by a pre-release build of `main` may carry the earlier spellings
  `unobserved`, `not-requested`, and `backend-default`. Such runs grade the same. `run_endings`
  counts a `stop_class` or `served_model_check` under the old spelling, but counts effort by the
  requested level, so an unpinned run recorded with `applied_by: "backend-default"` counts as
  `backend_default` and still pairs with new runs. Re-run them if you want one spelling in the
  report.
- A pre-release build of `main` read a realistic alias as a served-model `mismatch`, so a run that
  requested `sonnet[1m]`, `claude-sonnet-4-0`, a `-latest` id, a Bedrock id such as
  `us.anthropic.claude-sonnet-4-5-20250929-v1:0`, or a Vertex `@date` id was unscorable. The check
  is stored when the run is recorded, so re-run those runs; new runs read them as `match`, except
  a `-latest` id, which reads `unverifiable` (scored): the harness cannot know which dated
  snapshot the alias resolved to.
- `run-subagent` records completion evidence. Its default Claude backend now runs
  `claude -p --output-format stream-json --verbose` (it ran `--output-format json`), so a
  `max_tokens` stop records `truncated` and is unscorable, as in `run-claude`. An `--agent-cmd`
  reply may add `stop_class`, `stop_reason`, and `served_models`; a reply without them still runs
  and records `unavailable`.
- A run that reports several models credits none of them: `served_model` is `null`, and the check
  reads `mixed` (scored, counted in `run_endings.served_model_mixed`) when the requested model is
  among them, or `mismatch` (unscorable) when it is not. Claude subagent turns are not counted.
- A run whose `stop_class` is `truncated` or `turn_limit`, or whose `served_model_check` is
  `mismatch`, is unscorable ([execution validity](vocabulary.md#run-artifacts);
  `unscorable_reason`: `stopped:truncated`, `stopped:turn_limit`,
  `served_model_mismatch`) and blocks its pair. A new Claude run tree can therefore have fewer
  scorable pairs than an older tree of the same cases; read `unscorable_reason` before reading the
  smaller denominator as a skill change. A refusal is still graded.
- A pair whose arms ran at different effort is blocked as `effort_mismatch`, and a pair where only
  one arm recorded effort as `effort_unrecorded_on_one_arm`. Re-run an old arm rather than pairing
  it with a new one. The ablation confirmation and `token-overhead` block these pairs too. `run-agent --agent gemini|vibe --effort …` now exits before any run, and so does a level the backend's CLI does not accept: `run-claude --effort minimal` names Claude's levels (`low`, `medium`, `high`, `xhigh`, `max`). Claude Code 2.1.288 only warns about `minimal` and runs at its default effort, so such a run recorded `requested: "minimal"` for an effort it never ran at; re-run it at a level Claude accepts.

### Human feedback

- `feedback.json` is `{"schema_version": 2, "entries": [...]}` and every entry is validated on
  load. A file from the first served form still loads, and its `good`/`bad` verdicts are read as
  `pass`/`fail`; the next save rewrites the whole file as schema 2 with `pass`/`fail`. Keep a copy
  if another tool reads the old verdict words.
- An old entry that no longer validates, such as one saved with an empty case id, is moved
  verbatim to `unparsed_entries` on the next save instead of blocking it. `judge-alignment` and
  `error-analysis` ignore those entries, and `judge-alignment` counts them in
  `label_source.skipped.unparsed`. Fix an entry there and move it back to `entries` if it
  should count. An entry whose `variant` is not a real arm (`with_skill`, `without_skill`,
  `old_skill`, or `ablation:<id>`) no longer validates either.
- An entry that names a judge `assertion` with a pass/fail verdict is a `judge-alignment` label,
  so `--labels feedback.json` can replace a separately kept labels file. The legacy
  `{judge_task_id, passed}` file still loads.

### Expected report and audit changes

- Every paired block gains `interval` and `noise_check` (under `observed_*` when pairing is
  incomplete, or when the report is partial for `answer_design_incomplete` or
  `grading_evidence_incomplete`, which also moves `graded` to `observed_graded`).
  `benchmark --min-lift` adds `min_lift` to the noise check.
- A case whose arms both score 0 on every scored pair gains the `floor: fails in both arms` flag
  beside `no objective lift`. The score is the combined one readiness reads, so a judge that
  passes one arm keeps the case off the floor. `saturated/non-discriminating` still marks only
  the ceiling.
- `audit-manifest --runs` reports such a case as `floor-eval` instead of `no-lift-eval`, now
  including regression-intent cases, and `suggest-cases` no longer seeds it.
- Readiness moves a case whose combined score is 0 in both arms out of `base_saturated_cases` into
  `floor_cases`, which carries its own blocker. A regression-intent case at the floor used to count in
  `regression_guards_holding`, which never blocks; it now blocks, so
  `audit-manifest --fail-on-blockers` can start failing on a suite that passed under 0.6.0.
  Audit the case and its assertions rather than removing the regression intent.
- Every paired `significance`, `interval`, and `noise_check` block gains `unit`, the inference unit
  its test counts (`case` for benchmark lift, `replicate_pair` for an ablation regression's
  `significance` and each of its `by_case` tests).
- Ablation pairing diagnostics read `contrast_id: "ablation:<id>"` (0.6.0 wrote `skill_presence`),
  and a missing ablation run blocks as `missing_ablation:<id>` instead of `missing_without_skill`.
  Update any script that filters on those strings.
- Repeated judge runs (`judge --judge-runs N`) now carry an `agreement` block, and an even split
  no longer fails silently: it does not pass and reads `unresolved`, unless the median score
  clears the verdict's explicit threshold.
- `audit-manifest` output gains `eval_health`, `known_answer_check`, `case_sources`, and
  `readiness.blocker_findings`, and may report the new finding kinds listed in the changelog.
  `audit-manifest --runs` on an incomplete benchmark now reports the `benchmark-incomplete`
  blocker, so `--fail-on-blockers` can fail a suite that passed under 0.6.0; a suite with judge
  assertions needs `--judge-results`, which `audit-manifest` now accepts with the other
  [grading options](commands.md#grading-options).
- A manifest may now declare `source`, `reference_answer`, and `reference_answer_ref` on a case.
  Existing manifests are unaffected, but `validate` rejects an unknown `source`, both answer
  fields on one case, an inline `reference_answer` on a `holdout` or `holdback` case, and either
  field on a trigger case.
- `benchmark` output gains `incomplete_reasons`, the root causes behind a `partial` availability. The
  `benchmark-incomplete` readiness blocker names them in its message and evidence, and
  `report --format github` prints them, each once, in its experiment status; a `benchmark.json`
  written by 0.6.0 has no such list, so its status reads just `incomplete`.
- A case with no objective assertion in either arm (gated only by judges) no longer blocks the
  objective pairing as `missing_objective_pass_rate`. Its pairs are left out of it and counted in
  `pairing.not_applicable_pairs`, so `paired_summary` can read `complete` where 0.6.0 read
  `partial`. A pair with a missing or unscorable arm still blocks.
- `contamination` output gains `coverage`, and `--fail-on-contamination` now fails when an answer
  run has no saved output, as well as on a finding. Coverage counts every (case, model, arm, run)
  that run discovery finds, so one model's missing arm is not covered by another model's output.
  A CI job that ran the gate before the runs finished, or over a runs directory missing an arm,
  starts failing; point it at the complete run.
- Paired edit comparison: `benchmark` with an `old_skill` arm selected adds
  `paired_edit_summary`; without that arm the report is unchanged. `--variant` replaces the
  default arms, so pass all three: `--variant with_skill --variant without_skill --variant
  old_skill`. When the report is partial because an arm has no run or an assertion could not be
  graded, the edit's headline is withheld under `observed_*`, as `paired_summary`'s is.
- `skill-pi-trigger-eval` writes the `skill-trigger-matrix` report: the protocol producer is
  `skill-trigger-matrix` with one `pi` adapter, and the report gains `agents` and `matrix`.
  `trigger-compare` no longer accepts the old `skill-pi-trigger-eval` producer. Each row's `ablation` is the
  ablation id; the provenance is the report's `provenance`, as in the matrix. Traces written
  with `--trace-runs` land in a `matrix-*` directory under it. A query whose run crashes is now
  an incomplete row (exit 1) instead of stopping the whole run. Its `--timeout` now defaults to
  240 seconds, the matrix's default, instead of 120; the timeout is part of the protocol, so a
  report left at the old default does not pair with a matrix report. Pass `--timeout 120` to keep
  the old window.
- Pi's `PI_CODING_AGENT_DIR` now sits beside its working directory instead of inside it, so a
  Pi report's protocol requires `pi_home_outside_workdir` and its rows record it.
- The Claude trigger adapter's isolated `CLAUDE_CONFIG_DIR`, with the copied OAuth credentials,
  moved from `.trigger-config/` inside the working directory, where the model's Read and Glob
  could reach it, to a directory beside it. A Claude report's protocol requires
  `claude_config_outside_workdir` and its rows record it.
- The Claude trigger adapter now also isolates its config when authentication comes from the
  environment (`ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`, `CLAUDE_CODE_OAUTH_TOKEN`,
  `ANTHROPIC_BASE_URL`, or the Bedrock and Vertex switches), not only when a credentials file can
  be copied. Under `CLAUDE_CODE_OAUTH_TOKEN` or `ANTHROPIC_AUTH_TOKEN`, the usual CI login, rows
  read `config_isolated: false` and `trigger-compare` blocked every cell
  (`protocol_observation_unsafe`). An isolated run also drops `CLAUDE_CODE_SYNC_SKILLS`, so
  organisation skills no longer compete with the skill under test, and each row lists the other
  skills Claude Code offered the model as `competing_skills`. A rate measured with
  `config_isolated: false` could include personal and organisation skills; re-measure it rather
  than compare it with an isolated one.
- Codex trigger rows record the files seeded into `CODEX_HOME` as `codex_home_files` instead of
  `codex_home_files_copied`. `trigger-compare` read that list as an unsafe protocol observation
  and blocked every Codex cell; the regenerated reports the identity bump already requires pair.
- `aggregate` and `export-anthropic` accept `--strict` and `--embed-cmd`; pass them there too if
  your `benchmark` command uses them, or the numbers will differ.

### Fixes that change saved numbers

- The sign-flip test and the lift interval chose between exact enumeration and sampling on the
  total unit count, so unchanged units (zero deltas, which no sign flip can move) pushed any
  paired block over 14 units onto the sampled path, whose decision uses a Monte Carlo upper
  bound. Six units that all moved by +1 beside nine unchanged ones read `p_value_upper_bound`
  0.061, not significant, while `noise_check` called the eval resolvable. The choice now rests
  on the units that moved, and equal deltas are enumerated as one group, so that block reads the
  exact p = 0.03125 and is significant. A saved report with more than 14 paired units can
  change `significance`, `interval` and `method` (from `...-sampled` to `...-exact`) when
  regraded.
- When every paired delta is equal (every case gained one full run, say), the interval was the
  point `[v, v]`, so `noise_check` read `noise_floor: 0` and `resolvable` for any `--min-lift`. A
  sign-flip test reads only signs and cannot bound a constant sample, so the interval now reads
  `bounded: false` with a `reason`, the noise check reads `unbounded` with the same `reason`, and
  `audit-manifest --runs` reports `underpowered-eval` (mark 4 `concern`). `significance` is
  unchanged.

- Codex trace normalization no longer counts the stream's opening `thread.started` event as a
  file read. `file_reads` was one too high on every Codex run; the count is written when a trace
  is normalized, so a saved run keeps the old count until its `trace.jsonl` is normalized again
  (`import-trace --source codex`).
- A trigger observation now rejects a bare `estimated` cost source, as the answer path already
  did, and accepts the missing-cost block with observed parts that `normalize_cost` writes. A
  saved trigger report with an `estimated` cost row fails re-validation in `trigger-compare`;
  regenerate it.
- Claude trigger detection also counts a `Skill` call that names the directory the skill is
  mounted under (`demo` for `skills/demo/SKILL.md`), which is how Claude Code 2.1.269 invokes
  project skills. A Claude trigger report saved with such a CLI can show should-fire misses that
  were activations; re-run it. Vibe's `skill` tool detection reads the same two names.
- The Claude trigger adapter now reads a stream by the answer parser's rule: exactly one `result`
  record and no session content after it (`assistant`, `user`, `result`, `stream_event`, or a
  record with a `message` object). It took the last `result` and accepted a turn after it, so a
  cell whose stream had two results or a late turn counted as a complete observation; it is now
  incomplete. Any other record after `result` (`system`, `rate_limit_event`, a metadata type a
  later Claude Code adds) is metadata, in answer runs, judges and trigger cells alike; before,
  only `system` was, so a run ending in a `rate_limit_event` graded as an empty answer.
- Skills now mount under their own directory name: `skills/demo/SKILL.md` mounts as `demo`, the
  name a user's install shows, where 0.6.0 used the flattened manifest path
  (`skills_demo_SKILL.md`). Claude Code showed the model that flattened string as the skill's
  name, so trigger rates were measured for a name no user sees, and answer prompts pointed the
  model at `skills/skills_demo_SKILL.md/SKILL.md`. The mount name is part of every skill-tree
  hash, so `skill_tree_hash`, `skill_root_keys`, the planned skill tree and the task digest change
  on every skill-bearing prepared task and run (the demo skill's tree hash moves from
  `6bcbd3be…` to `4bbf2c1f…`); `without_skill` rows are unchanged. `benchmark` over runs
  prepared before the change reads `partial` with `answer_design_incomplete` ("prepared skill
  treatment does not match current manifest"): re-prepare and re-run them. Trigger reports were
  already incomparable across the change, because the trigger protocol fingerprints the harness
  modules. A `--pins` file or `examples/skill-pins.json`-style pin holds the old layout's
  hash: recompute it with `canonical_skill_tree_hash` (this repository's pins were recomputed from
  the pinned commits). Two skill roots that share a directory name (`team-a/review/SKILL.md` and
  `team-b/review/SKILL.md`) now fail `validate`, because an agent would list two skills with one
  name; rename one directory.

### Removed names

These module-level names are gone from the module shown, most because only tests called them,
and the Pi runner's because it now delegates to the trigger matrix. Code that imported them from
the harness modules needs the replacement:

| Removed | Use instead |
|---|---|
| `skill_benchmark.read_metadata_base` | `read_metrics_base` (same body) |
| `skill_benchmark.discover_run_bases` | `discover_case_model_roots` with `discover_run_bases_under` |
| `skill_benchmark.read_output`, `read_metadata` | `read_output_base`, `read_metrics_base` |
| `skill_benchmark.judge_cost_usd` | `judge_cost_block` |
| `skill_benchmark.CLAUDE_USAGE_KEYS` | `telemetry.USAGE_ALIASES` |
| `skill_benchmark.claude_run_metrics` | the `usage` and `cost_usd` fields `claude_cli_invoke` returns |
| `skill_benchmark.TRIGGER_SEMANTIC_MODULES`, `HARNESS_SEMANTIC_MODULES` | `TRIGGER_IDENTITY_MODULES` |
| `skill_benchmark.GEMINI_AUTH_FILES` | `GEMINI_AUTH_FILES_BY_TYPE` |
| `skill_benchmark.load_trace_jsonl` | `parse_trace_jsonl_text` |
| `skill_benchmark.persist_answer_design_value` | `persist_answer_design` |
| `skill_benchmark.register_workspace_builder` | a workspace builder on the backend's `agent_capabilities.BACKENDS` row |
| `ablation_model.Population` | `manifest_contracts.CasePopulation` |
| `ablation_model.Arm.harness_record` | `PreparedTask.harness_record` |
| `run_pi_trigger_eval.run_query`, `run_trigger_matrix.run_cell_query` | `run_trigger_matrix.observe_cell_query(adapter, tree_dir, query, should_trigger, model, timeout)` (for Pi, `PiAdapter()`), then `.as_row()` on the `TriggerObservation` it returns |
| `run_pi_trigger_eval.detect_trigger` (re-export) | `skill_benchmark.detect_trigger` |
| `run_pi_trigger_eval.observe_query`, `copy_skill_to_config`, `pi_trigger_protocol`, `write_trigger_trace_artifacts` | `run_trigger_matrix.run_matrix` with `agents=["pi"]`, or `observe_cell_query(PiAdapter(), ...)` |
| `run_pi_trigger_eval.load_manifest`, `skill_name_from_manifest`, `trigger_query_from_case`, `cases_from_manifest`, `validate_trigger_rows`, `pi_argv`, `pi_invocation_outcome`, `pi_source_config_dir`, `seed_config_dir` | the same names in `run_trigger_matrix` |
| `skill_benchmark.two_sample_permutation_significance`, `_combinations`, `_exact_rate`, `iteration_dirs`, `next_iteration_dir`, `final_answer_from_events`, `text_files_under`, `missing_evidence`, `resolved_task_upload_bytes`, `JETTY_TERMINAL_SUCCESS`, `JETTY_TERMINAL_FAILURE`, `JETTY_PENDING`, `run_pi_trigger_eval.pi_terminal_error`, `pi_invoke_result`, `run_trigger_matrix.matrix_capabilities`, `matrix_failure_row`, `runner_contracts.classify_runner_result`, `agent_capabilities.surface_names`, `DEDICATED_SMOKE_TARGETS`, `report_contracts.diagnostic_rates`, `ablation_model.Provenance.SCHEMA_KEYS`, `ablation_model._LEGACY_FAILURE_MARKER_ORDER` | nothing; they were dead or test-only |

`run_pi_trigger_eval.eval_rows_from_args` moved to `run_trigger_matrix`; `run_pi_trigger_eval`
imports it from there, so the old import still works.

The `iteration-N/` directory convention that `render-viewer --previous-workspace` reads is
unchanged; only the unused helpers went. Four names that a pre-release build of `main` added
were removed before release: `skill_benchmark.FLOOR_FLAG` (use `findings.CaseFlag.FLOOR`),
`experimental_pairs.effort_comparability` (use `ContrastSpec.comparability`),
`completion_contracts.stop_from_finish_reason`, and `human_judgements.judgements_from_document`.

## 0.5.1 → 0.6.0

Most version-1 and version-2 manifests continue to validate without edits. The
upgrade risk is in saved artifacts and custom adapters: 0.6.0 rejects identities,
verdicts, lifecycle states, and telemetry comparisons that 0.5.1 could accept or
coerce.

### Before installing

Keep the old package and run tree available until a regenerated report has been
reviewed. Telemetry migration is atomic per run directory, but a separate copy gives
users a clean rollback and preserves the exact input behind the 0.5.1 report.

```bash
cp -R eval-runs/latest eval-runs/latest-v0.5.1
cp benchmark.json benchmark-v0.5.1.json

python -m venv .venv-0.6
.venv-0.6/bin/python -m pip install skill-eval-harness==0.6.0
```

A fresh virtual environment keeps the old CLI usable while the new report is checked.

### What remains compatible

- Manifest versions 1 and 2 remain valid. There is no manifest version 3.
- `run-codex` and `run-claude` remain compatibility commands over `run-agent`.
- Legacy token and cost fields remain readable beside the schema-v3 telemetry envelope.
- Schema-v1 trace events and the older single-model run-directory layout remain readable.
- Python 3.10, 3.11, and 3.12 remain supported; PyYAML remains the only runtime dependency.

`skill-benchmark migrate` still means **manifest version 1 → 2**. The new
`migrate-telemetry` command upgrades saved `metadata.json` and `metrics.json`; the two
commands solve different migrations.

### Run the upgrade checks

#### 1. Validate the manifest

```bash
.venv-0.6/bin/skill-benchmark validate evals/shared-benchmark.json
```

An unchanged v1/v2 manifest should pass. If it does not, fix the reported field rather
than changing the manifest version.

#### 2. Inspect telemetry migration without writing

```bash
.venv-0.6/bin/skill-benchmark migrate-telemetry \
  --runs eval-runs/latest-v0.5.1 \
  --check \
  --out telemetry-migration-check.json
```

The check reports which run directories would change. It does not alter either
artifact. Legacy numbers are retained as `legacy_unverified`; migration does not invent
a provider, currency conversion, trace, or comparison basis.

Apply the migration to a working copy when the check looks correct:

```bash
cp -R eval-runs/latest-v0.5.1 eval-runs/latest-v0.6
.venv-0.6/bin/skill-benchmark migrate-telemetry \
  --runs eval-runs/latest-v0.6 \
  --out telemetry-migration.json
```

A migrated run has both `metadata.json` and `metrics.json`, each with
`telemetry_schema_version: 3` and the same `telemetry` envelope. Re-running the command
is idempotent.

#### 3. Regenerate reports

Use the migrated copy rather than overwriting the old report:

```bash
.venv-0.6/bin/skill-benchmark benchmark \
  evals/shared-benchmark.json \
  --runs eval-runs/latest-v0.6 \
  --split tune \
  --out benchmark-v0.6.json
```

Compare decisions, eligible sample counts, and blocked reasons. Do not require the two
JSON documents to be structurally identical: 0.6.0 adds telemetry availability and
pairing diagnostics, and some former numeric totals become `null` when the underlying
set is incomplete.

### Inputs that may need repair

#### Prepared task and result identities

Every comparative observation needs one exact identity:

```text
(case_id, model, run_number, population)
```

`run_number` must be a positive integer. Each identity may contain at most one
`with_skill` and one `without_skill` row. Missing or mismatched arms appear as blocked
pairs; duplicate arms are rejected. CLI-generated 0.5.1 task rows normally already
carry `run_number`, but hand-written or post-processed JSONL may not.

Do not repair a mismatch by renumbering one arm until it lines up. Regenerate the
missing observation or leave the pair blocked, because the repetition identity is part
of the measurement.

#### Stored judge verdicts

0.6.0 rejects duplicate task IDs and verdicts whose fields disagree. In particular:

- `passed` must be a JSON boolean;
- numeric fields must be finite;
- a scored verdict needs an explicit threshold;
- `passed` must agree with `score >= threshold`;
- dimension scores must match the declared dimensions and their derived aggregate;
- dynamic criteria need unique names and a feasible `minimum_criteria`.

Deduplicate by `judge_task_id`, then regenerate malformed verdicts with the judge
command. Do not keep whichever duplicate happened to occur last in a file.

#### Trigger eval sets

Each row must be an object with a nonblank string `query` and a JSON boolean
`should_trigger`:

```json
{"query": "Review this pull request before merge.", "should_trigger": true}
```

Strings such as `"false"`, integer `0`, missing fields, and non-list envelopes are now
errors instead of truthy/falsy inputs.

#### Jetty results

A successful Jetty record needs:

- a recognized, non-conflicting success lifecycle;
- a nonblank `trajectory_id`; and
- an `output.md` artifact.

Unknown states, conflicting `status`/`state` fields, duplicate import destinations, and
unsafe run paths fail before import. Normalize a provider-specific state in the adapter;
do not relabel an incomplete trajectory as successful.

#### Custom Codex wrappers

Codex answer runs now use `--output-last-message`, and the harness places `CODEX_HOME`
outside the model workspace. A custom `--codex-cmd` wrapper must accept the arguments the
harness appends. Smoke one prepared task before starting a paid matrix; ambient Codex
rules and user configuration no longer implicitly enter the eval workspace.

### Expected report changes

A changed number is not automatically a regression. Check these intentional semantic
changes first:

- **Missing telemetry stays missing.** A partial set exposes a `known_*` subtotal and
  availability counts instead of a false complete total. Measured zero remains numeric.
- **Comparisons use exact pairs.** Lift, reliability, cost, token, slice, readiness, and
  ablation views exclude missing, mismatched, ineligible, or cross-population arms.
- **Ablation confirmation needs more evidence.** The two-sided paired sign-flip gate
  needs at least six unanimous pairs to confirm (see **Inference unit** in
  [`vocabulary.md`](vocabulary.md#report-signals)). Named assertion coverage must also be
  symmetric across the pair.
- **Failed Pi streams cannot pass as clean negative triggers.** Exit zero does not
  override a provider/protocol failure or a missing terminal event.
- **Trace counts require proven completion.** Started, failed, malformed, and unknown
  lifecycle events no longer count as completed commands or tool calls.

Review the new `pairing`, availability, and blocked-reason fields before deciding that a
skill changed. They often explain a smaller denominator or a missing ratio directly.

### Rollback

Keep `eval-runs/latest-v0.5.1`, `benchmark-v0.5.1.json`, and the old environment until
the 0.6.0 report has been accepted. Rolling back the executable is then just using the
old environment or reinstalling the pinned release:

```bash
uv tool install --force skill-eval-harness==0.5.1
```

Do not convert a migrated tree back by deleting selected telemetry keys. Restore the
saved 0.5.1 tree instead; that preserves the artifact pair exactly as the old report read
it.
