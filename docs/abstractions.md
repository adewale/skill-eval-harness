# Key abstractions

The harness is a pipeline. Each stage hands one well-defined object to the next: a manifest
becomes task rows, task rows become files on disk, files on disk become graded result rows,
and result rows become a report. Because the boundaries are explicit, you can swap the
runner without touching grading, and grading never has to call a model.

Symbols below point at `skill_benchmark.py` at the line where each abstraction is defined.

## The objects, in pipeline order

| Abstraction | Defined at | What it hands downstream |
|---|---|---|
| Manifest | `validate_manifest:193` | The full test definition for one skill. |
| Case | `iter_cases:79` | One scenario with a prompt and graders. |
| Variant | `task_variants:302` | The arm a case runs under; the lift axis. |
| Split | `VALID_SPLITS:27` | Which cases are visible during iteration. |
| Assertion | `assertion_result:1642` | A single pass/fail check over one run. |
| Prepared task row | `prepared_task_rows:314` | A runner-neutral unit of work. |
| Run-output contract | `discover_run_bases:1028` | The files a runner leaves on disk. |
| Runner / adapter | `run_codex:1582` | Turns a task row into contract files. |
| Trace normalization | `normalize_trace_records:1447` | Runner-specific events, made uniform. |
| Judge plumbing | `collect_judge_tasks:1787` | Qualitative checks, deferred to a model you supply. |
| Grade result row | `grade_case_variant:1877` | One scored row per case/variant/run. |
| Benchmark report | `build_benchmark_report:2182` | Aggregates, lift, and flags. |

## Manifest

`evals/shared-benchmark.json` is the source of truth. It names the skill, the files under
test (`skill_paths`), the comparison arms (`variants`), the split policy, the `cases`, and
the `ablations`. `validate_manifest` rejects a manifest whose fixtures are missing, whose
regex assertions do not compile, or whose hidden cases lack a private prompt reference.
Optional blocks (`harness`, `jetty`, `run_protocol`) carry interop and runner hints without
changing the core shape.

## Case

A case is one scenario: an `id`, a `split`, a `kind` (`behavior` or `trigger`), the `prompt`
or a private `prompt_ref`, optional fixture `files`, `expected_behavior` notes, the
`assertions`, and a taxonomy the report slices on (`domain`, `difficulty`, `trigger_type`,
`success_goals`). `iter_cases` filters by split so iteration never sees holdout or holdback
prompts.

## Variant

A variant is the arm a case runs under: `with_skill`, `without_skill`, an optional pinned
`old_skill`, or `ablation:<id>`. Variants are orthogonal to cases, which is the whole point
of the tool. Lift is the difference between `with_skill` and `without_skill` on the same
case, so the variant axis is where skill effect becomes measurable. `variant_instruction`
writes the per-arm instruction; the baseline must run from a workspace that cannot read the
skill files, or the comparison means nothing.

## Split

`tune`, `holdout`, and `holdback` keep iteration honest. Tune cases are visible while you
edit. Holdout cases stay private until end-of-round scoring. Holdback cases stay out of the
skill, the docs, and even the eval descriptions until after scoring, which is how the
harness detects memorization. `prepare` refuses to emit a hidden case with no
`prompt_ref` unless you pass `--allow-missing-prompts` for dry-run planning.

## Assertion

An assertion is one check. The harness sorts them into four families
(`skill_benchmark.py:29-54`):

- **Text** (`contains`, `regex`, `file_exists`, `json_field_equals`, `script`): runs against
  `output.md` and sibling files.
- **Process** (`skill_invoked`, `command_ran`, `command_order`, `tool_count_le`): runs
  against normalized trace events, and fails closed when the evidence is absent.
- **Efficiency** (`total_tokens_le`, `elapsed_seconds_le`, `command_count_le`): runs against
  metrics.
- **Qualitative** (`judge`, `rubric`): deferred to a model you supply.

`assertion_result` returns `{name, type, passed, evidence}`. The shape is binary today: there
is a `passed` boolean and no numeric score except the one a judge may attach. The roadmap's
severity tier changes exactly this shape.

The planned graded shape (roadmap 2.2, ported from `adewale/anti-slop-writing`) adds an optional
`score` and `severity` (`gate` or `soft`), plus two `judge` assertion forms:

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
wrong." That distinction is the reason a saturated binary case can still show graded lift.

## Prepared task row

`prepared_task_rows` fans `cases × variants × runs_per_variant` into runner-neutral rows.
Each row carries the `prompt`, the absolute `input_files`, the `skill_paths`, the
`instruction` for its arm, and the `run_dir` it must write to. Generation rows omit
`expected_behavior` and rubrics unless you pass `--include-answer-key`, so a runner cannot
accidentally feed the answer key to the model under test.

## Run-output contract

The contract is the file boundary between any runner and the harness:

```
runs/<case_id>/<variant>/[run-<n>/]output.md
runs/<case_id>/<variant>/[run-<n>/]metadata.json     # optional
runs/<case_id>/<variant>/[run-<n>/]trace.jsonl        # optional, raw
runs/<case_id>/<variant>/[run-<n>/]events.json        # optional, normalized
runs/<case_id>/<variant>/[run-<n>/]metrics.json       # optional, normalized
```

`discover_run_bases` and `read_output_base` read this layout. A runner that writes these
files is a valid runner, whether it is Pi, Codex, Jetty, a subagent, or a person with a text
editor. This boundary is the main extension seam in the codebase.

## Runner / adapter

A runner consumes task rows and produces the contract. The repo ships four paths plus a
generic one: Pi smoke (`examples/adewale-workspace/run_pi_smoke.py`), Pi trigger
(`run_pi_trigger_eval.py`), Codex (`run_codex:1582`), Jetty (`JettyClient:663` and the
export/run/import commands), and any runner that writes the contract directly. The harness
calls no model itself; it reads what the runner left behind.

## Trace normalization

Runners disagree on event shape. Codex emits `command_execution` and `turn.completed`; Pi
emits `message_end` usage aliases; Jetty emits trajectory records. `normalize_trace_record`
and `normalize_trace_records` collapse these into one schema-versioned `events.json` plus
`metrics.json`, tagged with the source. Process and efficiency assertions read the normalized
form, never the raw prose, because inferring tool use from answer text is how false evidence
gets in.

## Judge plumbing

Qualitative assertions defer. `collect_judge_tasks` gathers every `judge`/`rubric` assertion
across runs and keys each by `judge_task_id` (`case::variant::run-n::assertion`).
`judge_prompt` renders the case, expected behavior, rubric, and candidate output into a
prompt; `run_one_judge_task` pipes it to the `--judge-cmd` you supply; `merge_repeated_judge_rows`
majority-votes pass/fail and medians scores across repeats. The harness picks no model. A
judge result is `{judge_task_id, passed, score, evidence}`, merged back at grade time.

## Grade result row

`grade_case_variant` produces one row per case/variant/run. It separates objective, process,
efficiency, and qualitative counts, computes each pass rate, marks `missing_output` when a run
never produced text, and carries the run `metadata`. Deferred judge assertions leave a task
behind rather than a verdict. Grading reads from disk and calls no model, which is what makes
a re-grade cheap and deterministic.

## Benchmark report

`build_benchmark_report` turns result rows into the artifact you read.
`build_paired_summary` computes per-case lift (`with_skill` minus `without_skill`,
normalized gain, and a flag when the skill hurts). `build_slice_summary` breaks results down
by domain, difficulty, trigger type, and success goal. Case flags mark saturated, no-lift,
flaky, and with-skill-failed cases. These flags, the leakage lint
(`prompt_assertion_leakage_findings:164`), and the split discipline are the part of the tool
no surveyed eval framework copies.

## What changes when you extend the tool

Two abstractions absorb most of the roadmap. Adding a numeric score or a severity tier means
changing the **assertion result shape** in `assertion_result` and the totals in
`grade_case_variant`. Adding a model sweep means adding a `model` axis to the fan-out in
`prepared_task_rows` and a grouping in `build_benchmark_report`. Touch those two carefully and
most other features fall into place around them.
