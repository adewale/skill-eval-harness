# Authoring evals: a workflow guide

The [README](../README.md) is the reference: every command, field, and assertion type. This
guide is the path from an empty repo to an eval you trust. The two differ on purpose. You
read the README to look something up; you read this to learn the order of operations.

Three rules carry the rest:

1. **Measure lift, not vibes.** The question is always what changed when the skill ran that
   did not change without it. That is why every case runs as a `with_skill` /
   `without_skill` pair.
2. **Deterministic graders first, judges second.** Use a model only when a keyword, regex,
   file, or script check cannot express the property you care about.
3. **Do not let the eval leak the answer.** A case that the no-skill baseline passes by
   echoing the prompt proves nothing about the skill.

## The loop

```
define "done"  ->  write prompts  ->  run paired  ->  grade  ->  read the report  ->  iterate
   success          no                any             deterministic    lift?            tune ->
   goals            assertions        runner,         first,           saturated?       holdout ->
                    yet               no Jetty        judge            flaky?           holdback
                                      needed          second
```

Most weak evals come from writing the checks before seeing a single real run. Resist it.

## Step 0 — Define "done" before writing anything

Pick the success goals the skill owns. The harness stores these per case in `success_goals`:

| Goal | The question it answers | Typical graders |
|---|---|---|
| `outcome` | Did it produce the right result? | `contains*`, `regex`, `file_exists`, `json_field_equals`, `golden_output`, `similarity`, `structured_output`, `script` |
| `process` | Did it work the right way? | `skill_invoked`, `command_ran`, `command_order`, `tool_call`, `tool_count_le` |
| `style` | Is it phrased and structured well? | `judge` / `rubric` / `factuality` as yes/no claims; anchored `graded_dimensions` for ordinal properties |
| `efficiency` | Did it stay within budget? | `total_tokens_le`, `elapsed_seconds_le`, `command_count_le` |

Keep the definition small and must-pass. Encode the behaviors whose regression would
embarrass you, not every preference. A subjective skill (writing, design) may carry only a
`judge` rubric and no objective assertion, and that is fine.

Two knobs shape *how* a check counts, and both default to today's behavior so you can ignore
them until you need them. **Severity** (`critical` / `gate` / `soft`) decides what a failure
does: a `gate` (the default for objective checks) lowers the pass rate; a `soft` result (the
default for `judge`/`similarity`) feeds only a per-run **graded score** — the "how much
better" channel — and never moves a pass rate; a `critical` failure vetoes the run outright
(see Step 4). The graded score is where a saturated binary case can still show lift, via a
`similarity` threshold, a graded `script` oracle (`{"score", "max_score"}` on stdout), or a
`judge` with anchored `graded_dimensions` / a `dynamic_rubric`.

## Step 1 — Write prompts only

Author cases as prompts with `expected_behavior` notes and no `assertions` yet. Draw them from
these sources in this order, which is the order the `/claude-api build-eval` guide in the
claude-api skill uses (the reasoning is in
[Automating eval design and hillclimbing with Claude](https://claude.dev/blog/automating-eval-design-and-hillclimbing/)):

1. **Real sessions and requests** where the skill should have helped. Before a transcript becomes
   a committed fixture, check whether a retention policy will force you to delete it and whether
   it holds PII that cannot sit in the repo.
2. **Bug reports and manual fixes.** A correction you made by hand after the skill ran is a case
   the skill failed.
3. **Five to ten hand-written seeds.** These lean toward what is memorable to you rather than
   what is frequent, so treat them as seeds, not the whole set.
4. **Synthesized variants of those seeds**, never cases invented from the skill's text with
   nothing real to anchor them.

Record where each case came from in its `source` field: `production`, `bug-report`,
`hand-written`, `synthesized`, or `imported` for a case carried over from another suite.
`audit-manifest` counts the sources, and a suite that records none, or only synthesized cases,
gets a finding against eval-health mark 1 (realistic cases).

A case earns its place when you can say why it is hard, and the reason is one a domain expert
would name. "Today's model fails it" does not count: a set picked that way measures one model's
failure fingerprint, and its lift shrinks when the next model arrives.

Size the suite in cases, not runs. The paired significance test treats each case as one unit
(its repeats are averaged into one rate per arm first), and a case whose lift is zero cannot
move the test. With k cases that moved, the smallest achievable p is 2/2^k, so fewer than 6
moved cases can never reach p ≤ 0.05 (2/2^5 = 0.0625). Aim for 15 or more cases so that at least
6 can move. Repeats sharpen each case's rate; they do not add cases. After a run,
`paired_summary.noise_check` reports `cases_moved`, `smallest_achievable_p`, and a `verdict`;
`paired_summary.interval` gives the 95% range for the lift (unbounded with 5 or fewer cases); and
`benchmark --min-lift 0.1` checks whether the eval's noise floor is below the smallest lift you
would act on.

Mix the kinds on purpose:

- **Positive** (`pos-*`): the skill should fire and help.
- **Negative** (`neg-*`): the skill should not fire, or should decline.
- **Trigger** (`kind: "trigger"`): does the model load the skill from its description on its
  own? Keep these apart from answer-quality cases, because they test the description, not the
  answer.

Prefer fixture-backed cases (`files: [...]`) over inline-only prompts. A real diff, README, or
repo tells you more than a prompt the model can answer from general knowledge.

**Prompts-first has one exception: deterministic artifacts.** When a case has a single
known-correct output (a file that must equal a reference, a value that must match), write the
check first, the way `pythonbyexample` and `xampler` do: an example is an input, an expected
output, and a check that the two still match. Use a `golden_output` or `json_field_equals`
assertion up front. Reserve prompts-first for open-ended outputs, where you cannot know the
right check until you have seen what the model actually produces.

## Step 2 — Scaffold a minimal manifest and validate

The smallest useful `evals/shared-benchmark.json`:

```json
{
  "version": 1,
  "skill_name": "my-skill",
  "harness": { "name": "skill-eval-harness", "url": "https://github.com/adewale/skill-eval-harness", "version": ">=0.6.0" },
  "skill_paths": ["skills/my-skill/SKILL.md"],
  "variants": ["with_skill", "without_skill"],
  "split_policy": {
    "tune": "Visible cases used during iteration.",
    "holdout": "Hidden cases scored at end-of-round or merge.",
    "holdback": "Examples withheld from skill/docs/evals until after scoring."
  },
  "cases": [
    {
      "id": "pos-first-case",
      "split": "tune",
      "kind": "behavior",
      "success_goals": ["outcome"],
      "prompt": "…the real user prompt…",
      "files": ["fixtures/first-case/input.md"],
      "expected_behavior": ["What a good answer must do."],
      "assertions": []
    }
  ],
  "ablations": []
}
```

```bash
skill-benchmark validate evals/shared-benchmark.json
```

`validate` checks shape, fixture paths, regex syntax, script paths, and hidden-prompt refs. It
also warns when an assertion value appears verbatim in the prompt. That warning is the
leakage lint, and you want to act on it.

`version: 1` above is fine to start with. `version: 2` is the same manifest with the severity
and oracle-tier defaults made explicit; `skill-benchmark migrate` upgrades a version-1 manifest
and prints the diff plus a checklist of judgment calls (see [`migrating-evals.md`](migrating-evals.md)).
A version-1 manifest keeps grading identically, so there is no rush.

## Step 3 — Run the pair (no Jetty required)

`prepare` emits answer-key-safe task rows; a runner turns each row into the run-output contract
on disk. Jetty is one optional adapter. Pi, Codex, a subagent, or a person all satisfy the
same contract.

```bash
skill-benchmark prepare evals/shared-benchmark.json --split tune --runs-per-variant 3 --out /tmp/tasks.jsonl

# Example with the bundled Codex runner:
skill-benchmark run-codex --tasks /tmp/tasks.jsonl --runs eval-runs/latest
```

Each task must land as:

```
eval-runs/latest/<case_id>/<variant>/run-<n>/output.md
eval-runs/latest/<case_id>/<variant>/run-<n>/metadata.json   # optional but worth capturing
```

Isolate the baseline. `without_skill` must not be able to read the skill files. Run each
variant from its own workspace and copy skill files only for `with_skill`, because a disabled
flag is not a boundary when the runner can `grep` the skill out of the source tree.

## Step 4 — Add assertions, now that you have runs

Open the outputs and write the smallest assertions that capture the behavior, in this order:

1. **Deterministic objective** (`contains_any`, `regex`, `file_exists`, `json_field_equals`;
   plus `golden_output` for a reference-equal artifact, `similarity` for a thresholded ratio,
   `structured_output` for a JSON-schema shape). Test behavior, not one phrasing, so that
   `Decision: BLOCK` and `**Decision: BLOCK.**` both pass.
2. **Process / efficiency**, but only when the runner emits trace evidence (`events.json`,
   `metrics.json`). These fail closed without evidence by design. Scope them per variant:
   `skill_invoked=true` for `with_skill`, `false` for `without_skill`.
3. **`script` oracle**, when a keyword check is too weak. Opt in with `--allow-scripts`; print a
   `{"score", "max_score"}` line to make it a graded oracle. A script defaults to the `demo`
   oracle tier, and a case graded only by `demo`/`live` oracles trips `weak-oracle-only` in
   `audit-manifest`; once you have verified that a script checks the real end state (it builds or
   renders the result and inspects it), mark it `"oracle": "strong"`.
4. **`judge` / `rubric`** last, for qualitative properties. The harness defers these and picks
   no model; you supply `--judge-cmd` (or `--judge-model`). Write the rubric as checkable claims:
   several yes/no judge assertions (soft by default), each checking one property ("names the
   missing test", "does not invent an API parameter"), rather than one judge asked how good the
   answer is. The run records the fraction met as `soft_passed` / `soft_total`; `graded_score`
   averages only verdicts that include a `score` (optional in the plain judge contract), so have
   the judge return `score` 1 or 0 if you want the fraction in the paired `graded` channel. Keep
   anchored 1-5 `graded_dimensions` for properties that are ordinal, where a 3 sits between a 2
   and a 4. When a property is fuzzy and the question is which arm did better, compare the arms
   directly: `compare-tasks` exports each run's `with_skill` and `without_skill` outputs as a
   blind A/B pair in random order, and `compare-results` maps your judge's answers back to arms
   and counts wins (it runs no significance test).

If `validate` warns that a value is in the prompt, replace the keyword with a scoped regex, a
fixture-backed check, a script oracle, or a judge.

Before trusting the assertions, grade two answers whose verdict you already know: a reference
answer written by hand and an answer that only restates the prompt. The reference must pass every
gate and the echo must fail. A failing reference means an assertion rejects a correct answer; a
passing echo means an assertion leaks.

Where a case has a deterministic known answer, declare it and let `audit-manifest` run that check
on every audit. Put it inline as `reference_answer` on a `tune` case; a `holdout` or `holdback`
case must name a private file with `reference_answer_ref` instead, because a known answer is an
answer key. The audit's known-answer check runs the case's gate text checks (`contains`,
`contains_any`, `contains_all`, `excludes_any`, `regex`, `not_regex`) on the reference answer and on
the prompt echoed back, and reports `reference-answer-fails` or `null-answer-passes` against
eval-health mark 2. The reference answer never reaches a runner.

For checks the audit cannot run on a string (files, scripts, JSON fields), grade the pair by hand:
put them in a scratch run layout (`<case_id>/with_skill/run-1/output.md` for the reference,
`<case_id>/without_skill/run-1/output.md` for the echo) and run `benchmark` on it. Do not use an
empty file as the bad answer, because an empty output is recorded as `missing_output` and never
scored. Process assertions fail closed without trace evidence, so neither check covers them.

Then read about five graded failures in each arm of a real run. If more than about one in ten
are assertion errors rather than real misses, fix the assertions before a full run. Misgrades on
`without_skill` matter most, because a correct baseline answer marked wrong inflates lift.

Two patterns from the hardest grading domains are worth copying. First, **check both presence
and absence**: a strong oracle confirms the good traits are there *and* the bad ones are not.
`adewale/swiss-poster-skill`'s `drama_oracle` passes only when every required carrier is present
and no forbidden pattern (SVG-only, AI-palette gradients, soft-SaaS styling) appears — the same
slop-detection instinct as `anti-slop-writing`. Use `excludes_any` / `not_regex`, or a script
oracle that fails on a forbidden match, to catch output that claims the style but lacks the
substance. Second, **the strongest oracle inspects the rendered artifact, not the source text**:
`swiss-poster-skill`'s `rendered_poster_oracle.py` runs headless Chrome and audits the rendered
pixels (overflow, contrast). When source text can lie about the result, render it and check what
actually came out.

**Name the catastrophic failures separately.** Some failures cannot be averaged away — writing
outside the results directory, reporting success after a check failed, leaking a secret. One such
failure across twenty runs is a catastrophe, not a 95% pass rate (the "valley-dodging" point).
Mark these `severity: "critical"` — an `excludes_any` / `not_regex` for the forbidden state, or a
`script` oracle that exits non-zero on it, with `"severity": "critical"` set. A critical failure
vetoes the run, collapses its rates to 0.0, and is surfaced on its own (a `critical-failure`
flag), so no graded mean elsewhere can bury it. `adewale/guardrails-skill` encodes exactly these
fences ("never write outside the results directory," "do not report success if a check failed").
One caution: a hard prohibition that is too broad makes a skill obstinate, so pair each with a
negative case proving the skill still does the reasonable thing.

## Step 5 — Benchmark and read the report

```bash
skill-benchmark benchmark evals/shared-benchmark.json --runs eval-runs/latest --split tune --out benchmark.json
skill-benchmark render-viewer --benchmark benchmark.json --runs eval-runs/latest --out review.html
```

Do an analyst pass before touching the skill. Read the flags, because the headline pass rate
hides the signal:

- **Lift**: `with_skill` minus `without_skill` per case. No lift means the case does not
  discriminate, and the cause decides the fix. At the ceiling (both arms pass), the case is too
  easy: add a harder fixture or an artifact-level check. At the floor (both arms fail every
  scored run, flagged `floor: fails in both arms` and reported by `audit-manifest --runs` as
  `floor-eval`), suspect the case or its assertion first: read the outputs and check whether any
  correct answer could pass.
- **Saturated** (`saturated/non-discriminating`): both arms pass every scored run, so the
  case cannot show lift ([`vocabulary.md`](vocabulary.md#report-signals) separates this flag
  from a with-skill ceiling and from a base-saturated case).
- **Flaky**: repeated runs disagree. Investigate before trusting the number.
- **With-skill-failed**: the skill made things worse. This is the highest-priority flag.
- **Missing output**: not measured, which differs from measured-and-failed. Excluded from
  lift and saturation.

Then rate the eval itself before you trust any lift it reports. `audit-manifest` over the same
runs rates the five eval-health marks; pass the judge verdicts too, or a suite with judge
assertions reads as incomplete and marks 3–5 stay `unavailable`. On the bundled demo, after the
[demo README](../examples/demo-skill/README.md)'s prepare, run, and judge steps:

```bash
cd examples/demo-skill
skill-benchmark audit-manifest evals/shared-benchmark.json --runs /tmp/demo-runs \
  --judge-results /tmp/demo-judge.jsonl --format markdown --out /tmp/demo-audit.md
```

The Eval health section of `/tmp/demo-audit.md` (real output, 2026-09-30, six runs per arm):

```text
## Eval health

| Mark | Question | Status | Findings |
|---:|---|---|---|
| 1 | Are the cases realistic, and does the skill load the way real use loads it? | concern | missing-positive-evals, missing-negative-evals, missing-adversarial-evals, missing-trigger-no-trigger-cases, case-source-unrecorded |
| 2 | Is the grader right on known answers? | ok | — |
| 3 | Does the baseline arm have room to move, with no case failing in both arms? | ok | — |
| 4 | Is the noise smaller than the smallest lift worth acting on? | concern | underpowered-eval |
| 5 | Do the arms differ only in the skill? | ok | — |
- mark 1: activation is forced: the task tells the agent to use the skill, while real use relies on discovery (issue #48)
- mark 2: no case declares reference_answer, so no known-good answer was graded
```

Read it mark by mark. Mark 1 is a concern because the demo has too few cases of each polarity
and records no `source`. Mark 2 is `ok` only on the null-answer half: its note says no case
declares a `reference_answer`, so the known-good half was never graded. Mark 4 is the
`underpowered-eval` finding: two cases can never reach p ≤ 0.05 however many repeats run, so the
fix is more cases. A mark that reads `unavailable` had no evidence, which is not a pass. The five
marks, and why they differ from the hillclimbing post's four, are in
[`comparing-with-claude-api-evals.md`](comparing-with-claude-api-evals.md#five-marks-of-a-lift-eval).

## Step 6 — Iterate, and respect the splits

Failures drive coverage. Every manual fix you make while developing the skill is a candidate
eval case, so add it.

| Split | When it runs | Prompt storage |
|---|---|---|
| `tune` | While editing skill and evals | inline `prompt` is fine |
| `holdout` | End-of-round or merge scoring | private `prompt_ref` |
| `holdback` | Withheld from skill/docs/evals until after scoring | private `prompt_ref` + ignored answer keys |

Tune saturation is not release proof. Hold the claim of release quality until hidden prompts,
private answer keys, and real fixtures are filled and scored on `holdout` and `holdback`.

## Diagnosing a failure

When `with_skill` fails a case, resist patching the symptom. Work the trace the way you would a
production incident:

1. **Find the seam.** Read the run's `events.json` and `output.md`, locate the last step that went
   right, and the first that went wrong. The break between them is where the skill lost the thread.
2. **Classify the failure.** Most fall into a few types: the skill never loaded (trigger gap),
   it loaded but ignored a fixture (context loss), a command failed or was skipped (tool failure),
   it answered confidently past its evidence (overconfidence), or the eval itself is wrong (eval
   defect: an ambiguous case, an assertion that rejects a correct answer, or an answer cut off at
   a length limit). Claude runs record a cut-off answer as `stop_class: truncated` in
   `metadata.json` and the harness excludes it from scoring; the other two you find by reading
   the case beside the output. The type points at the fix, and an eval defect is fixed in the
   eval, not the skill.
3. **Fix the pattern, not the incident.** If the description under-triggered, widen the
   description, not this one prompt. If a reference was ignored, fix how the skill points at
   references. A fix that only satisfies one case usually just moves the failure.
4. **Add a regression case — then prune, within `tune` only.** Add a case only if the failure
   represents a real pattern, not a one-off. A suite is a memory of bugs you refuse to
   reintroduce, but a `tune` case that never fails again and never shows lift is dead weight;
   drop it. Never pick or drop `holdout` cases by their measured lift, or because `without_skill`
   fails them: part of any case's measured lift is chance, so the cases kept for a large lift
   regress toward the mean on the next run, and a holdout curated that way overstates lift.
   Twenty cases that discriminate beat two hundred that always pass.

The path matters as much as the answer. A case can produce the right final text for the wrong
reason, so grade the trajectory (`skill_invoked`, `command_order`) alongside the output, and treat
a right answer reached the wrong way as a finding, not a pass.

## Pitfalls that cost us rounds

- **Leaky keyword assertions**: the no-skill baseline passes by echoing the task.
- **All-saturated**: decide which saturation you are targeting (with-skill passes, or no-skill
  also passes) before optimizing, because the two call for opposite actions.
- **Missing outputs counted as failures**: false no-lift flags. Mark them `missing_output`.
- **Unbounded smoke runs**: cap thinking and require a bounded answer; capture timeouts as
  artifacts instead of aborting the round. A capped answer that gets cut off is recorded, not
  graded: Claude runs write `stop_class: truncated` and the run is excluded from scoring. Other
  runners record `unavailable`, so on those read failing outputs for answers that end
  mid-sentence.
- **Trigger cases written as meta-prompts**: run the real user prompt, and detect skill loading
  from the copied skill path, not from a name in the output.
- **Ablation benefit claimed from the manifest alone**: an ablation is evidence only after its
  `ablation:<id>` rows have run and been benchmarked.

The shortest version of this whole guide: write the prompt, run both arms, look at what the
model actually did, and only then write the check that would have caught the difference.
[`LESSONS_LEARNED.md`](../LESSONS_LEARNED.md) records the round where each pitfall above bit us.
