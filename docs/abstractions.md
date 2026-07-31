# Key abstractions

The harness is a pipeline. Each stage hands one well-defined object to the next: a manifest
becomes task rows, task rows become files on disk, files on disk become graded result rows,
and result rows become a report. Because the boundaries are explicit, you can swap the
runner without touching grading, and the default grading path never has to call a model.

This is the **engineering lens** on the terms in [`vocabulary.md`](vocabulary.md): what each object *is* in the code and what it hands downstream. The glossary defines the words; this page shows their shape. Symbols below point at `skill_benchmark.py` at the line where each abstraction is defined.

## The objects, in pipeline order

| Abstraction | Defined by | What it hands downstream |
|---|---|---|
| Manifest | `validate_manifest` | The full test definition for one skill. |
| Case | `iter_cases` | One scenario with a prompt and graders. |
| Variant | `task_variants` | The arm a case runs under; the lift axis. |
| Split | `VALID_SPLITS` | Which cases are visible during iteration. |
| Assertion | `assertion_result` | A single pass/fail check over one run. |
| Prepared task row | `prepared_task_rows` | A runner-neutral unit of work. |
| Run-output contract | `discover_run_bases` | The files a runner leaves on disk. |
| Runner / adapter | `run_codex` | Turns a task row into contract files. |
| Trace normalization | `normalize_trace_records` | Runner-specific events, made uniform. |
| Judge plumbing | `collect_judge_tasks` | Qualitative checks, deferred to a model you supply. |
| Grade result row | `grade_case_variant` | One scored row per case/variant/run. |
| Benchmark report | `build_benchmark_report` | Aggregates, lift, and flags. |

## Manifest

`evals/shared-benchmark.json` is the source of truth. It names the skill, the files under
test (`skill_paths`), the comparison arms (`variants`), the split policy, the `cases`, and
the `ablations`. `validate_manifest` rejects a manifest whose fixtures are missing, whose
regex assertions do not compile, or whose hidden cases lack a private prompt reference.
Optional blocks (`harness`, `jetty`, `run_protocol`) carry interop and runner hints without
changing the core shape.

## Case

A case is one scenario: an `id`, a `split`, a descriptive `kind` (for example `behavior`,
`negative`, `adversarial`, or `trigger`), the `prompt` or a private `prompt_ref`, optional fixture `files`, `expected_behavior` notes, the
`assertions`, and a taxonomy the report slices on (`domain`, `difficulty`, `trigger_type`,
`success_goals`). `iter_cases` filters by the explicitly requested split. The CLI keeps split
outputs distinct; authors/CI still control when hidden splits are run and whether prompts live in
private `prompt_ref` files.

## Variant

A variant is the arm a case runs under: `with_skill`, `without_skill`, an optional pinned
`old_skill`, or `ablation:<id>`. Variants are orthogonal to cases, which is the whole point
of the tool. Lift is the difference between `with_skill` and `without_skill` on the same
case, so the variant axis is where skill effect becomes measurable. `variant_instruction`
writes the per-arm instruction; the baseline must run from a workspace that cannot read the
skill files, or the comparison means nothing.

## Split

`tune`, `holdout`, and `holdback` label the intended evaluation phase. Tune cases are for
iteration; holdout is intended for end-of-round scoring; holdback is intended to remain outside the
skill/docs/eval descriptions until after scoring. The harness filters and reports these labels but
does not provide access control—repositories and CI must keep private material private. `prepare` refuses to emit a hidden case with no
`prompt_ref` unless you pass `--allow-missing-prompts` for dry-run planning.

## Assertion

An assertion is one check. The code-side registries are `TEXT_ASSERTIONS`,
`PROCESS_ASSERTIONS`, `EFFICIENCY_ASSERTIONS`, and `QUALITATIVE_ASSERTIONS`
(`skill_benchmark.py:66-95`):

- **Text** (`contains`, `contains_any`, `contains_all`, `excludes_any`, `regex`,
  `not_regex`, `file_exists`, `json_field_equals`, `golden_output`, `similarity`,
  `structured_output`, `script`): runs against `output.md` and sibling files.
- **Process** (`skill_invoked`, `command_ran`, `command_not_ran`, `command_order`,
  `tool_call`, `tool_count_le`, `no_repeated_command_loop`): runs against normalized trace
  events, and fails closed when the evidence is absent.
- **Efficiency** (`total_tokens_le`, `elapsed_seconds_le`, `command_count_le`): runs against
  metrics.
- **Qualitative** (`judge`, `rubric`, and the `factuality` preset): deferred to a model you
  supply.

`assertion_result` returns `{name, type, passed, evidence, score}`. Every result carries a
`score` (binary detectors mirror `passed` as `1.0`/`0.0`; `similarity`, `golden_output`, and a
graded `script` oracle set a real value), and `grade_case_variant` stamps a `severity`
(`critical`/`gate`/`soft`) and an `oracle` tier (`strong`/`demo`/`live`) on each.

Severity decides how a result counts: a `critical` failure vetoes the run and is excluded from
every mean; a `gate` carries the pass rate; a `soft` result feeds only the graded score. The
graded shape (roadmap 2.2, ported from `adewale/anti-slop-writing`) adds two `judge` assertion
forms:

```jsonc
// anchored dimension: the judge scores against named, observable anchors
{ "type": "judge", "graded_dimensions": [
  { "name": "specificity", "scale": "1-5",
    "rubric": "5 = names the failure mode and the mechanism; 1 = generic restatement" }
] }

// dynamic rubric: the judge drafts case-specific criteria, then grades against them
{ "type": "judge", "dynamic_rubric": { "instruction": "draft 3-5 criteria from the brief",
                                       "minimum_criteria": 3 } }
```

A graded assertion answers "how much better," where the binary `passed` answers only "right or
wrong." That distinction is the reason a saturated binary case can still show graded lift. An
optional `reference_score`/`reference_graded_score` on a case sets a no-regression floor, and
`build_paired_summary` reports a paired `graded` channel and a sign-flip significance test
beside the raw lift.

## Prepared task row

`prepared_task_rows` fans `cases × variants × models × runs_per_variant` into runner-neutral
rows (the `model` axis, from `prepare --models`, adds a run-dir segment only when two or more
models run, so single-model layouts are unchanged). Each row carries the `prompt`, the absolute
`input_files`, the `skill_paths`, the `instruction` for its arm, and the `run_dir` it must
write to. Generation rows omit `expected_behavior` and rubrics unless you pass
`--include-answer-key`, so a runner cannot accidentally feed the answer key to the model under
test.

There are two in-process types. `PreparedTaskDraft` may hold partial planning data but is never
executable. `PreparedTaskDraft.validate()` / `PreparedTask.from_row()` constructs a strict
`PreparedTask`: identifiers and paths are validated, split and execution variant are closed,
repetition is positive, `run_dir` is safe and relative, and `without_skill` cannot carry skill
paths. Every native runner and Jetty exporter requires the executable type.

The prepared rows also form a persisted `answer-design.json`: the exact expected
case/model/repetition identities plus a digest of manifest inputs, referenced oracles, and
variant instructions. Each produced run repeats the design, task, and instruction digests.
Report builders re-derive the current eval contract and require exact design coverage before
publishing paired or aggregate headlines; surviving rows from an incomplete design are retained
only as explicitly observed diagnostics.

## Run-output contract

The contract is the file boundary between any runner and the harness:

```
runs/<case_id>/<variant>/[run-<n>/]output.md
runs/<case_id>/<variant>/[run-<n>/]metadata.json     # optional
runs/<case_id>/<variant>/[run-<n>/]trace.jsonl        # optional, raw
runs/<case_id>/<variant>/[run-<n>/]events.json        # optional, normalized
runs/<case_id>/<variant>/[run-<n>/]metrics.json       # optional, normalized
runs/<case_id>/<variant>/[run-<n>/]artifact-commit.json # harness-written commit marker
```

`discover_run_bases` and `read_output_base` read this layout. A runner that writes these
files is a valid runner, whether it is Pi, Codex, Jetty, a subagent, or a person with a text
editor. Harness-owned schema-v1 writers commit their required files and SHA-256 inventory by
writing `artifact-commit.json` last; a missing or stale marker makes such a declared artifact
set incomplete. Legacy or externally written runs that do not declare that contract version
remain readable through the compatibility boundary. This boundary is the main extension seam
in the codebase.

## Runner / adapter

An **answer runner** consumes prepared task rows and produces the run-output contract. The repo
ships Pi answer smoke (`examples/adewale-workspace/run_pi_smoke.py`), Codex (`run_codex:7694`), Claude (`run_claude:7862`, capturing real
per-run cost), Mistral Vibe (`run-agent --agent vibe`, using isolated `VIBE_HOME` outside the workdir), the in-process
subagent runner (`run_subagent:10282`, which hosts record/replay tool I/O via `ToolReplayStore`),
Jetty (`JettyClient:3660` and the export/run/import commands), and any runner that writes the
contract directly. Each answer runner registers a workspace builder so one cross-runner invariant
proves its `without_skill` arm is skill-free (CF.2). Autonomous trigger runners are separate: they
read trigger cases from the manifest directly, never consume answer task rows, and emit trigger
observations plus optional traces rather than answer grades.

Native answer backends return the frozen `Completed | TimedOut | SpawnFailed | ProviderFailed`
union from `runner_contracts.py`. `OutcomeContext` validates provider, telemetry, and elapsed-time
fields; `write_runner_outcome` exhaustively writes the disk contract. A backend therefore cannot
independently set timeout, return code, answer, and failure into a contradictory bag. The harness
calls no model during default grading; it reads what the runner left behind. The explicit
`--allow-scripts` and `--embed-cmd` modes may invoke caller-supplied external oracle subprocesses.

## Trace normalization

Runners disagree on event shape. Codex emits `command_execution` and `turn.completed`; Pi
emits `message_end` usage aliases; Jetty emits trajectory records. `normalize_trace_record`
and `normalize_trace_records` collapse these into one schema-versioned `events.json` plus
`metrics.json`, tagged with the source. `trace_contracts.EventState` first classifies each event as
completed, in-progress, failed, or unknown; only completed operations contribute command/tool/file
counts. Process and efficiency assertions read the normalized form, never the raw prose, because
inferring tool use from answer text is how false evidence gets in.

## Judge plumbing

Qualitative assertions defer. `collect_judge_tasks` gathers every `judge`/`rubric` assertion
across runs and keys each by `judge_task_id` (`case::variant::run-n::assertion`, with a `model`
segment on a multi-model run). `grade --judge-tasks` can serialize that queue, while the
`judge` command reconstructs the same tasks from the manifest and run directory rather than
reading the optional queue file. `judge_prompt` renders the case, expected behavior, rubric,
and candidate output into a prompt — including the anchored dimensions or dynamic-rubric
instruction for a graded assertion; `run_one_judge_task` pipes it to the `--judge-cmd` you
supply or to a native `--judge-backend` (`claude`, `codex`, or `vibe`) plus `--judge-model`;
`merge_repeated_judge_rows` majority-votes pass/fail and medians scores across repeats. The
harness picks no model. At the result boundary, `judge_verdict.py` parses one strict variant:
boolean, scored, dimension-scored, dynamic-rubric, or consensus. Pass is derived from the typed
payload; duplicate IDs and contradictory score/threshold/pass rows are rejected. The serialized
row still carries `{judge_task_id, verdict_kind, passed, score, evidence}` — plus
`dimension_scores`/`criteria` for a graded verdict and normalized `usage_normalized`/
`cost_normalized` for judge spend — merged back whenever `grade` or `benchmark` receives
`--judge-results`. Both task and result carry `judge_input_sha256`, binding the verdict to the
exact rendered prompt, candidate output, and evidence. A stale or mismatched result is rejected
or re-queued even when its `judge_task_id` still matches.

## Grade result row

`grade_case_variant` produces one row per case/variant/run. It separates objective, process,
efficiency, and qualitative counts, computes each pass rate, marks `missing_output` when a run
never produced text, and carries the run `metadata`. Deferred judge assertions leave a task
behind rather than a verdict. Grading reads from disk and calls no model, which is what makes
a default re-grade cheap and deterministic; opt-in script and embedding oracles are the explicit
external-process exceptions.

## Benchmark report

`build_benchmark_report` invokes the shared grader for each discovered run, then
turns those in-memory result rows into the artifact you read. It does not consume the output
of the `grade` command. Before arithmetic,
`experimental_pairs.py` constructs exact `(case, model, repetition, population)` identities and
requires one eligible arm of each kind. `build_paired_summary` computes per-case lift
(`with_skill` minus `without_skill`, normalized gain, and a flag when the skill hurts) only from
those pairs; missing/ineligible arms remain in `pairing` diagnostics and duplicate arms fail.
`build_slice_summary` breaks results down
by domain, difficulty, trigger type, and success goal. Case flags mark saturated, no-lift,
flaky, and with-skill-failed cases. These flags, the leakage lint
(`prompt_assertion_leakage_findings:667`), and the split discipline are the part of the tool
no surveyed eval framework copies.

## What changes when you extend the tool

Two abstractions absorbed most of the roadmap, and they remain the seams to reach for next.
The numeric `score` and the `severity` tier live in the **assertion result shape**
(`assertion_result`) and the totals in `grade_case_variant`; the `model` sweep is a third axis
in the fan-out (`prepared_task_rows`) with a grouping in `build_benchmark_report`
(`by_model`/`model_analysis`). Both are cross-cutting: a change to either touches every place
that *aggregates* a run (each pass-rate/report view) or *identifies* one (`run_dir`, run
discovery, `judge_task_id`), so extend by auditing those consumers, not just the definition
site. Touch these carefully and most other features fall into place around them.
