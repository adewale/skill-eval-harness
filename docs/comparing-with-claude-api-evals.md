# Should I use `/claude-api build-eval` and `/claude-api hillclimb`, or this harness?

Anthropic's claude-api skill ships two eval sub-commands, introduced in
[Automating eval design and hillclimbing with Claude](https://claude.dev/blog/automating-eval-design-and-hillclimbing/)
(Lance Martin, 2026-09-28). `/claude-api build-eval` interviews you and builds an eval
inside your codebase; `/claude-api hillclimb` improves your app against that eval one
change at a time, with a held-out split to catch overfitting. Both are procedures Claude
follows from guides in the skill (`build-eval.md`, `eval-hillclimb.md`,
`cost-hillclimb.md`, and the shared `eval-audit.md` checklist), so each run leaves a
runner written in your repo's idiom and a report page behind. They cover any
Claude-powered app: the system prompt, a skill, tool descriptions, model and effort, or
the harness code. This harness is narrower and fixed: a CLI and a manifest format that
measure one thing, whether a skill causes a lift, with no interview and no loop.
**The guides own designing an eval with a person and climbing it; the harness owns
proving the lift came from the skill and that the eval could have seen it.** Use the
guides to decide what to measure and to run the change-and-check loop. Use the harness
when the artifact being tuned is a skill and the number has to survive a no-skill
control, hidden splits, and a significance test at small n.

The one runnable part below is offline, on the bundled demo skill.

## What the two commands do

### `/claude-api build-eval`

- **An interview with two sign-offs.** Claude asks which flow to measure, reads its entry
  point, and then stops twice for an explicit yes: on the inputs, shown in full as a page,
  and on the grading method. Every other decision is a question with a recommended
  default.
- **Cases sourced in a fixed order.** Production transcripts first (after asking about
  retention and PII), then bug reports and tickets, then five to ten cases you write by
  hand, and last, cases synthesized from the codebase, anchored on three to five real
  examples. Fifteen to a hundred inputs for a first eval.
- **The cheapest grader that fits.** A programmatic check when the output space is
  constrained (for an agent, the end state of a disposable workspace). Second, a pairwise
  blind comparison: A/B order randomized per case, `tie` and `both_bad` allowed, and the
  baseline's outputs frozen as the reference if you will hillclimb. Then a pointwise
  rubric written as checkable claims rather than a 1-to-5 scale, and a human spot-check
  when even a rubric is hard to write. You pick the judge model; it should not be the
  model under test.
- **A pilot you grade.** Claude grades a handful of cases, shows them with their
  transcripts, and asks: "Would you have scored any of these differently?" One yes sends
  the rubric back for another iteration.
- **Oracle and null before the first paid run.** The reference answers and an empty or
  constant output go through the whole runner and grader and must score about 100% and 0%.
  The judge must fail an empty string, "I don't know", and a confident answer to the wrong
  question. An induced API error must land as an error, not a zero.
- **A runner that keeps plumbing out of the score.** Attempts that never produced a
  scorable output go to an `errors.jsonl` sidecar with a failure class (harness or serving
  error, timeout, served-model mismatch), never into `results.jsonl`. Rows carry
  `stop_reason`, and `status: truncated` when the answer hit `max_tokens`; the report
  counts those and leaves them out of the means. Refusals are graded as their own metric.
  The runner reads `model` from each response and fails the attempt when it differs from
  the one requested. A harness-integrity gate hashes the runner plus
  `_state.json.harness_paths` and refuses to run after an edit until you re-approve with
  `--approve-harness`.

### `/claude-api hillclimb`

- **Step 0.5: prove the eval can be climbed.** Three numbers side by side: the noise floor
  (the paired-difference 95% CI half-width, roughly `1/sqrt(n·reps)` for a pass rate, so
  25 cases × 2 repeats is about ±14 points), the headroom (ceiling minus baseline), and the
  smallest improvement you would ship. If the floor exceeds either, the loop stops before
  round 1 and offers more repeats or cases. The same step disables the mechanism to see
  the score drop, recomputes the headline from raw rows, spot-checks baseline failures,
  and reads the served model from a response.
- **A random split.** Train and test, drawn at random and stratified by `tags[0]`, never
  by baseline score. The analyzer reads only train transcripts. Small sets skip the split
  and label scores directional; at around 150+ cases an optional validation slice picks
  the winner each round and test is held until the end.
- **One change per round, kept or reverted whole.** A fresh analyzer reads the train
  transcripts and proposes one change aimed at a failure's root, large enough to show
  above the noise floor, describing the failing behavior rather than pasting the failing
  content into the prompt. Train up with test flat is read as overfitting and
  reverted, as is a round that regresses on train.
- **Stall bucketing.** After two or three rounds inside the noise band, one round makes no
  edit and sorts every remaining train failure into artifact gap, grader disagreement,
  harness or infra, structural, or variance, and each bucket gets its own fix.
- **A final report that can say no.** Test score at baseline and at the winner, each with
  a confidence interval. If the delta is within noise, the report says so and recommends
  not merging. It adds a per-round table, a failure taxonomy when zeros have mixed causes,
  and before/after transcript pairs.

When the goal is cost at equal quality, the loop follows `cost-hillclimb.md`: check cache
health, audit the prompt and request parameters, then walk model × effort as a staircase.
The walk enters at the highest cost-plausible tier at low effort, steps down a tier on a
pass and one effort notch right on a fail, and prunes any cell projected above the
incumbent's cost. The prompt is climbed only on the frozen model, one cell down-left is
re-probed, and a registered joint confirm at n ≥ 3 produces the headline. A change is
adopted only when three gates registered before round 1 all pass: a quality band on
held-out cases, a cost margin, and a mechanism gate. The mechanism gate requires the
predicted mechanism (fewer tool calls, say) to show in the measurements, so a cost win
with the wrong mechanism is a rejection.

## What each does that the other doesn't

### Where the harness goes further

| | `/claude-api` guides | This harness |
|---|---|---|
| Control arm | The baseline is the unmodified app; variants are later versions of it | Every case runs `with_skill` and `without_skill` on exact `(case, model, repetition)` pairs, so a lift is attributed to the skill; `ablation:<id>` arms attribute it to one component |
| Hidden cases | Train and test (optional validation); test is scored every round | `tune` / `holdout` / `holdback`; holdback prompts stay private (`prompt_ref`) until after scoring |
| Re-grading | Re-run the judge on stored transcripts | Deterministic grading calls no model and no network (CF.4), and a re-grade is byte-identical apart from `generated_at` (CF.3) |
| Leakage | A checklist item: read the prompts for the expected answer | `validate` warns when a contains-style assertion value (at least `--leakage-min-chars`, default 4) appears literally in its own prompt; `--strict-leakage` fails on it |
| Judge calibration | Agreement on a few dozen human labels; well below ~90% means iterate the judge | `judge-alignment` reports Cohen's kappa, precision, recall, and F1, so a judge that passes everything on a mostly-passing set reads kappa 0.0 rather than high agreement ([`can-i-trust-my-judge.md`](can-i-trust-my-judge.md)) |
| Small-n statistics | Noise floor ≈ `1/sqrt(n·reps)` | Exact paired sign-flip over per-case deltas (exact up to 14 cases, seeded sampling above), the interval that inverts it, and `noise_check.smallest_achievable_p` |

### Where the guides go further

| | `/claude-api` guides | This harness |
|---|---|---|
| Human sign-off | Inputs and grading each need an explicit yes; the pilot asks whether you would have scored any case differently | No interview; `render-viewer --serve` collects verdicts after a run into `feedback.json` |
| Pipeline oracle and null | Reference answers and an empty or constant output go through the suite's own runner and grader before the first paid run | Detector fixtures (CF.1) prove each assertion type fires and passes on known inputs; nothing pushes a suite's references or a null answer through end to end |
| Failure classes | Every failed attempt, in whatever runner the guide builds: refusal, harness or serving error, timeout, genuine failure | `stop_class` and served model are read from Claude runs, served model only from Gemini; Codex, Vibe, subagent, and Jetty runs record `unavailable` |
| Per-case auditor | One cheap model call per case flags ambiguous, suspect gold, answerable from memory, grader too strict or too lenient, cheatable | `audit-manifest` lints the manifest; no per-case model audit |
| Judge prompt | Tells the judge not to reward length, treats candidate text as untrusted data, and tests three known negatives | `judge-robustness` runs two negative controls (empty, master-key) and an order flip; the prompt guards are roadmap 5.5 ([#98](https://github.com/adewale/skill-eval-harness/issues/98)), not implemented |
| Cache parity | Flags variants whose cache-read share differs, because warm against cold cache skews cost and latency | Records cache-read and cache-write tokens per run; no parity check across arms |
| Split | Drawn at random, stratified by `tags[0]`; train and test means must agree within noise | Each case's split is set by the author; a split helper is roadmap 5.9, not implemented |
| The loop | Analyze, apply one change, run, keep or revert, bucket failures on a stall, report with CIs | None; the harness measures one revision per run (`old_skill` compares two) |

## Two numbers that mean different things in each

### The noise floor

The guides size an eval with `1/sqrt(n·reps)`, which counts every repeat as an
independent observation. Repeats of one case share its prompt, fixtures, and difficulty,
so they move together. The harness makes the case the unit: it averages each case's
repeats into one delta and runs an exact sign-flip test over those deltas, so the formula
and the test disagree most when there are few cases and many repeats.

The demo shows the gap with no model. `stub_runner.py` answers from the skill it finds
mounted, so `with_skill` passes and `without_skill` fails on every run:

```bash
cd examples/demo-skill
H=../../skill_benchmark.py
S=/tmp/noise-demo; rm -rf "$S"; mkdir -p "$S"
python3 $H prepare evals/shared-benchmark.json --split tune --runs-per-variant 4 \
  --out "$S/tasks.jsonl"
python3 $H run-codex --tasks "$S/tasks.jsonl" --runs "$S/runs" \
  --codex-cmd "python3 $(pwd)/stub_runner.py"
python3 $H judge evals/shared-benchmark.json --runs "$S/runs" \
  --variant with_skill --variant without_skill \
  --judge-cmd "python3 $(pwd)/stub_judge.py" --out "$S/judge.jsonl"
python3 $H benchmark evals/shared-benchmark.json --runs "$S/runs" \
  --judge-results "$S/judge.jsonl" --min-lift 0.1 --out "$S/bench.json"
python3 -c 'import json,sys; p=json.load(open(sys.argv[1]))["paired_summary"]; print(json.dumps({k: p[k] for k in ("absolute_delta","significance","interval","noise_check")}, indent=1))' "$S/bench.json"
```

Real output (2026-09-30, Python 3.11, offline stub):

```json
{
 "absolute_delta": 1.0,
 "significance": {
  "method": "sign-flip-exact",
  "n": 2,
  "observed_mean_delta": 1.0,
  "p_value": 0.5,
  "p_value_upper_bound": 0.5,
  "significant_at_0_05": false,
  "unit": "case"
 },
 "interval": {
  "confidence": 0.95,
  "n": 2,
  "method": "sign-flip-inversion-exact",
  "lower": null,
  "upper": null,
  "bounded": false,
  "reason": "2 paired case(s) cannot exclude any lift at 95%; the test needs at least 6 cases that differ between arms",
  "unit": "case"
 },
 "noise_check": {
  "verdict": "too-few-cases-moved",
  "cases": 2,
  "cases_moved": 2,
  "cases_needed_for_alpha": 6,
  "alpha": 0.05,
  "smallest_achievable_p": 0.5,
  "noise_floor": null,
  "headroom": 1.0,
  "min_lift": 0.1,
  "unit": "case"
 }
}
```

All 8 pairs (2 cases × 4 repeats) moved from fail to pass, and the report still declines
the claim. Every block names its `unit`, `case`, because the test counts cases rather than
runs. Two cases allow four sign patterns, so p cannot go below 0.5, and
`noise_check.verdict` names that limit before any interval is drawn. The rule of thumb
puts the noise floor at `1/sqrt(2 × 4)`, about ±35 points, and would call a 100-point lift
resolved. The stub is deterministic, so its four repeats are four copies of one answer;
the formula counts them as four observations anyway. With a real model the repeats differ
and carry some information, but repeats of one case still move together, so the formula
still overstates how many independent observations the eval has.

The post's own held-out result is the realistic version. On 14 held-out tickets at 3
repeats, the final configuration scored 38/42 (90.5%) against the original setup's 33/42
(78.6%): 5 more passing runs. However those 5 runs are spread across tickets, an exact
paired sign-flip test cannot go below p = 0.0625 (2/2^5); the harness needs at least 6
cases moving the same way before p ≤ 0.05 is reachable. The guide's own formula gives about
±15 points for 14 × 3 (`1/sqrt(42)`) against an observed gain of 11.9, so both methods put
the quality gain inside the noise. The claim that eval can carry is the cost one: about one
fifth of the original cost at parity, which needs quality to hold rather than to rise
past the noise.

### The test set

The `eval-hillclimb` guide's default split scores test every round, picks the winning
round by its test score, and reports that score as the headline. A score used to pick the
winner is no longer an unbiased estimate of it; `cost-hillclimb.md` says so directly:
"The split whose score picks winners each round is a selection set, even if the guide
calls it 'test'", and notes that sequential selections, "each made on the data that
chose it, overstate the combined win." The guide's three-way form for large sets fixes
this, and the harness's splits already have that shape:

| Hillclimb split | Read by the analyzer | Picks the winner | Harness split |
|---|---|---|---|
| train | every round | no | `tune` |
| validation (optional, ~150+ cases) | never | every round | `holdout` |
| test, in the three-way form | never | no; scored once at the end | `holdback` |

Under the default two-way split, a hillclimb's "test" plays the harness's `holdout` role
and nothing plays `holdback`, so the reported delta is a selection-set number.

## Using them together

### The harness as the eval a hillclimb climbs

For a skill, the pieces line up: the skill is the artifact the hillclimb tunes,
`without_skill` is the baseline, and each skill revision's `with_skill` arm is a variant.
The hillclimb guide reads a fixed on-disk layout (`_state.json`, `baseline/`, `vN/`, each
variant with `results.jsonl` and `traces/`). An `export-hillclimb` command that writes a
harness run into that layout is roadmap 5.11 and **not implemented**; the
[spec entry](eval-framework-roadmap-spec.md#bucket-5--eval-health-from-the-claude-api-build-eval-and-hillclimb-comparison)
holds the arm and split mapping it would use. Until it exists, run the harness at each
round yourself and read its outputs at the step that asks for them.

### Which harness output answers each guide step

| Guide step | What it asks | Read in the harness |
|---|---|---|
| hillclimb Step 0.5: noise floor vs headroom vs smallest change | Can the eval show the win at all? | `paired_summary.noise_check`: `verdict`, `noise_floor`, `headroom`, and `min_lift` from `benchmark --min-lift`; `projected_cases`, when present, estimates the case count that would resolve it |
| hillclimb Step 0.5: prove the mechanism is wired | Does the score drop without it? | `without_skill` is the skill-off run on every case; `ablation:<id>` with the `ablation_regressions` block for one component ([`ablation-study-walkthrough.md`](ablation-study-walkthrough.md)) |
| eval-audit §2 and the Step 4.5 harness bucket: infra vs model failures | Which zeros are plumbing? | `run_endings` per variant: `stop_class` counts, `refused_runs`, `cut_off_runs`, `served_model_mismatches`, and `notes`; `unscorable_reason` on result rows |
| eval-audit §2: the served model is the one requested | Did the right model answer? | `served_model_check` on every run ([values](vocabulary.md#run-artifacts)); a `mismatch` is excluded from scoring, and a `mixed` run is scored but counted in `run_endings.served_model_mixed` |
| eval-audit §1 difficulty headroom; the post's "fails every run" tell | Is a case too easy, or broken? | Case flags `saturated/non-discriminating` (ceiling: both arms 1.0) and `floor: fails in both arms`; `audit-manifest --runs` emits `floor-eval`, and readiness lists `floor_cases` |
| build-eval pilot and eval-audit judge calibration | Does the judge agree with a person? | `render-viewer --serve` writes `feedback.json`; `judge-alignment --labels feedback.json` scores the judge against it |
| eval-audit: judge tested on known negatives | Does the judge reject junk? | `judge-robustness` (empty and master-key controls, order flip) |
| Step 4.5 stall bucketing | Why do the remaining cases fail? | `error-analysis --feedback feedback.json`: the review queue and failure taxonomy ([`why-did-this-run-fail.md`](why-did-this-run-fail.md)) |
| Step 5 report with CIs | Is the delta outside noise? | `paired_summary.interval` (`bounded: false` means no shift can be excluded) and `significance` |
| The post's low-variance mark: effort applied consistently | Did both arms run the same config? | `effort` on every run (`--effort` on `run-claude`, `run-codex`, `run-agent`); pairing blocks `effort_mismatch`, and `run_endings.notes` warns when a multi-model report ran every arm at `backend_default` effort |

## What keeps the comparison honest

- **Only shipped harness features are credited.** Anything not on this branch carries its
  roadmap number and "not implemented": the judge prompt guards (5.5), the split helper
  (5.9), and `export-hillclimb` (5.11).
- **The guides are described from their text.** The steps above come from the guides as
  shipped in the claude-api skill, and the post's numbers are its own; neither command was
  run for this doc.
- **The demo proves arithmetic, not a lift.** The stub is model-blind and deterministic, so
  its output shows what `noise_check` reports for two cases, and nothing about the demo
  skill's value.

## Where this stops

This doc does not tell you how to write the eval; [`authoring-evals.md`](authoring-evals.md)
does that for a harness manifest, and `/claude-api build-eval` does it interactively for
any app. It also does not cover surfaces the harness has no arm for: a system prompt, tool
descriptions, or harness code are things the guides can climb and the harness cannot
compare. For a skill's description, the tuning loop is
[`tuning-skill-activation.md`](tuning-skill-activation.md); comparing two descriptions
head to head needs a `swap:<id>` variant, which is not built (see [`TODO.md`](../TODO.md)).
