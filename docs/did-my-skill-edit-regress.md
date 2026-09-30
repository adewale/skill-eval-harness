# Did my skill edit regress anything?

You changed a line in `SKILL.md`, trimmed a reference, retitled a heading. Now you want to
know whether anything you already pass got worse. The obvious move is to `diff` the two
outputs: run before, run after, eyeball it. But a skill's output is a *sample*, not a fixed
string, so one before/after diff is a coin toss dressed up as evidence. The regression worth
acting on has a sharper shape: a **named assertion flips pass→fail with provenance**, the
same case failing a check it used to pass. So pin a baseline, re-run the arms you already
grade, and read which *named* checks moved.

Two honest ways to pin that baseline, carrying different evidence:

- **Within one run**, hold a baseline arm beside the edited one and let the benchmark diff
  them. In a real repo that baseline is `old_skill` (the previous revision, pinned in the
  manifest); on the demo it is an **ablation arm** — a skill with one piece removed, which
  is exactly the shape of a regressing edit. The `ablation_regressions` block reads that
  diff at the level of a named assertion and gates it on a significance test.
- **Across iterations**, keep each run in its own `iteration-N/` workspace and diff
  report-to-report with `render-viewer --previous-workspace`: per-variant and per-case
  deltas plus flag churn between two `benchmark.json`, the descriptive localizer.
  Because the two iterations ran on different days, that diff also carries whatever
  changed between them besides your edit: the CLI version, the model snapshot behind
  an alias, the agent's config. This repo's demo trigger cell shows how large that can
  be. Haiku loaded the skill on 1 of 3 runs in July; [PR
  #85](https://github.com/adewale/skill-eval-harness/pull/85) re-ran the July
  description text twelve weeks later (as the `weaker-description` ablation, which
  strips the `when_to_use` line added since) under a newer Claude Code CLI, a new
  skill mount name, and an isolated config dir, and Haiku loaded it 3 of 3. The
  `/claude-api hillclimb` guide in the claude-api skill reports the same effect:
  "identical-code drift of a few points between measurement days is common". When
  you need the edit's effect alone, grade an `old_skill` arm beside `with_skill` in
  the same run ([set-up below](#where-this-stops)) and keep the iteration diff for
  localizing.

One command *sounds* like the tool for this and isn't: `compare-results`. It tallies a
pairwise-preference judge (mapping a judge's `A`/`B`/`TIE` winners onto `primary`/`baseline`
roles from a truth file), so it answers "which output did a judge prefer," not "did my
assertions regress." Use the two blocks above.

## The offline loop, arm 1: an ablation stands in for a bad edit

Run every arm through the deterministic stub — no model, no key. From
[`examples/demo-skill/`](../examples/demo-skill/):

```bash
cd examples/demo-skill
HARNESS=../../skill_benchmark.py
V="--variant with_skill --variant without_skill \
   --variant ablation:no-severity --variant ablation:no-checklist"
python3 $HARNESS prepare evals/shared-benchmark.json --split tune \
  --include-ablations --ablation-dir /tmp/demo-abl --runs-per-variant 6 \
  --out /tmp/demo-tasks.jsonl
python3 $HARNESS run-codex --tasks /tmp/demo-tasks.jsonl --runs /tmp/demo-runs \
  --codex-cmd "python3 $(pwd)/stub_runner.py"
python3 $HARNESS judge evals/shared-benchmark.json --runs /tmp/demo-runs $V \
  --judge-cmd "python3 $(pwd)/stub_judge.py" --out /tmp/demo-judge.jsonl
python3 $HARNESS benchmark evals/shared-benchmark.json --runs /tmp/demo-runs $V \
  --judge-results /tmp/demo-judge.jsonl --out /tmp/demo-bench.json
```

The `judge` step is not optional. `c-review` declares a judge assertion
(`actionable-review`), and until its verdicts are merged the report is partial: every
ablation regression reads `evidence_class: "indeterminate"` with the note `grading
evidence is incomplete`, however many repeats you ran. `stub_judge.py` is the demo's
offline judge.

The `no-severity` ablation removes the `## Severity rules` section — the same regression a
careless edit to `SKILL.md` would cause. Read `ablation_regressions` in the report
(trimmed to the `no-severity` entry; deterministic offline stub, 6 matched pairs):

```json
{
  "id": "no-severity",
  "status": "measured",
  "provenance_verified": true,
  "regressions": [
    {
      "summary": "without the severity rule the reviewer stops labeling findings",
      "cases": ["c-review"],
      "assertions": ["severity-label"],
      "score_regressed": true,
      "evidence": [
        {"case": "c-review", "assertion": "severity-label",
         "with_skill_rate": 1.0, "ablation_rate": 0.0,
         "paired_observations": 6}
      ],
      "confirmed_cases": ["c-review"],
      "significance": {
        "method": "per-case-model-paired-sign-flip",
        "significant_at_0_05": true, "min_p_value": 0.03125
      },
      "evidence_class": "confirmed_causal",
      "expected_regression_confirmed": true
    }
  ]
}
```

One named assertion — `severity-label` — went from `with_skill_rate: 1.0` to
`ablation_rate: 0.0` on case `c-review`, and the verdict is `expected_regression_confirmed:
true`. That is a regression, stated as the thing that broke.

## The offline loop, arm 2: two iterations, diffed report-to-report

Now the loop as you actually run it in a repo: grade what you have, edit, re-grade into the
*next* iteration directory, diff against the last. Keep each run in one workspace so
`--previous-workspace` can find its `benchmark.json`.

```bash
WS=/tmp/demo-iter                 # holds iteration-1/, iteration-2/
# iteration 1 — the skill you have today
mkdir -p $WS/iteration-1
python3 $HARNESS prepare evals/shared-benchmark.json --split tune \
  --runs-per-variant 4 --out $WS/iteration-1/tasks.jsonl
python3 $HARNESS run-codex --tasks $WS/iteration-1/tasks.jsonl \
  --runs $WS/iteration-1/runs --codex-cmd "python3 $(pwd)/stub_runner.py"
python3 $HARNESS judge evals/shared-benchmark.json --runs $WS/iteration-1/runs \
  --variant with_skill --variant without_skill \
  --judge-cmd "python3 $(pwd)/stub_judge.py" --out $WS/iteration-1/judge.jsonl
python3 $HARNESS benchmark evals/shared-benchmark.json --runs $WS/iteration-1/runs \
  --variant with_skill --variant without_skill \
  --judge-results $WS/iteration-1/judge.jsonl --out $WS/iteration-1/benchmark.json

# THE EDIT: delete the "## Severity rules" section from skills/demo/SKILL.md
#   (edit in place, then `git checkout` it when you are done to keep the demo pristine)

# iteration 2 — the same mkdir and four commands into $WS/iteration-2, then diff:
python3 $HARNESS render-viewer --benchmark $WS/iteration-2/benchmark.json \
  --previous-workspace $WS/iteration-1 --out $WS/iteration-2/review.html
```

Skip the `judge` step and both reports are partial, so the diff panel reads
`"availability": "partial"` with every headline field `null`.

The rendered review carries a **Diff vs previous workspace** panel. Its JSON (2026-09-29,
offline stub; `without_skill` deltas, all 0.0, elided):

```json
{
  "availability": "complete",
  "variant_deltas": {
    "with_skill": {
      "mean_objective_pass_rate": {"before": 1.0, "after": 0.25, "delta": -0.75},
      "mean_combined_pass_rate": {"before": 1.0, "after": 0.16666666666666666, "delta": -0.8333}
    }
  },
  "case_deltas": [
    {"case_id": "c-adversarial", "variant": "with_skill", "before": 1.0, "after": 0.0, "delta": -1.0},
    {"case_id": "c-review",      "variant": "with_skill", "before": 1.0, "after": 0.5, "delta": -0.5}
  ],
  "new_flags": [
    "c-adversarial::floor: fails in both arms",
    "c-adversarial::no objective lift",
    "c-adversarial::with-skill failure",
    "c-review::with-skill failure"
  ],
  "resolved_flags": []
}
```

The headline `variant_deltas.with_skill` fell 1.0 → 0.25, and `case_deltas` localizes it:
`c-adversarial` collapsed (−1.0, its only assertion was `severity-label`) while `c-review`
half-fell (−0.5, it lost `severity-label` but kept `cite-checklist`). `new_flags` names the
newly-failing cases; `c-adversarial` also gains `floor: fails in both arms` because its
`with_skill` arm now scores 0.0, the same as the baseline.

A **safe** edit — reword the intro prose, touch no load-bearing section — produces the
empty diff instead (real output, same command against a cosmetic-only iteration):
`variant_deltas.with_skill.mean_objective_pass_rate` reads `{"before": 1.0, "after": 1.0,
"delta": 0.0}`, and `"case_deltas": []`, `"new_flags": []`, `"resolved_flags": []`.

## Reading the diff, symptom by symptom

- **A named assertion flips pass→fail across the arms/iterations** → a real regression, and
  you already know which piece caused it. In the `ablation_regressions` block above,
  `severity-label` on `c-review` went 1.0 → 0.0 with `expected_regression_confirmed: true`.
  *Action:* the removed component (`## Severity rules`) is load-bearing for that assertion —
  restore it, or if the edit was intentional, update the assertion and record why.
- **The top-line is flat but a different case now fails (a swap)** → the aggregate lies; the
  per-case view catches it. `render-viewer` emits `case_deltas` keyed per
  `(case_id, variant)` *and* `variant_deltas` keyed only on the per-variant mean — two
  blocks on purpose. An edit that helped `c-adversarial` by exactly as much as it hurt
  `c-review` would leave `variant_deltas.with_skill` ≈ 0 (a green headline) while
  `case_deltas` still listed both moves, as the two-entry `case_deltas` array above already
  shows it can. *Action:* read `case_deltas`, never the headline alone — a flat mean is not
  "no change," it is "no *net* change."
- **No entries move** — `case_deltas: []`, `new_flags: []` — → the edit was safe *on the
  cases you have*, within the resolution your repeats allow. Each case's rate rests on
  four runs here, so on a real model an edit that breaks 1 run in 10 can leave every
  case at 1.0 in both iterations. Each report states its own resolution:
  `paired_summary.interval` is the range of with-vs-without lift the data cannot rule
  out, and `paired_summary.noise_check` names what limits it. On the demo's two cases the interval reads `"bounded": false` and the
  verdict `too-few-cases-moved`. *Action:* ship it with both qualifiers in mind: the
  diff guards only the cases in your suite (see the boundary below), and only at the
  resolution those reports state.

## What keeps the measurement honest

- **A single-shot diff is noise.** Rerun the ablation arm with one run per arm and the same
  block reads `evidence_class: "indeterminate"`, `expected_regression_confirmed: null`, with
  the note *"regression observed but not significant per case across replicates (min
  p=1.0); p <= 0.05 needs at least 6 matched replicate pairs that move the same way (the
  smallest reachable p with 6 is 0.03125)"* (re-run 2026-09-30). The confirmed verdict
  above only appears at six unanimous repetition pairs; the unit it counts is the
  **Inference unit** entry in [`vocabulary.md`](vocabulary.md#report-signals). A regression
  is a *named assertion flipping with provenance and significance*, not a score that
  wobbled once.
- **The demo diff is exact only because the stub is deterministic.**
  Its six matched replicates are identical, so the paired sign-flip test clears on perfect separation. A
  real model's runs carry variance; there you need *genuine* repeats before a one-run diff
  earns trust, and the significance gate is what stops an unreplicated dip from counting.
- **Two evidence classes, do not conflate them.** The `ablation_regressions` confirmation is
  `confirmed_causal` — verified provenance (a materialized, blind tree) *and* a significant
  per-case drop. The `--previous-workspace` diff is a *descriptive* delta with no
  significance gate: treat its `case_deltas` as a **localizer** for which case to look at,
  then confirm the named regression with repeats. Keep the variant name stable across
  iterations, too — the diff keys on `(case_id, variant)`, so a rename reads as
  removed-then-added instead of changed.

## Where this stops

This journey detects that an edit regressed *something you already test*. It does not prove
*which component* is load-bearing in general — confirming that a removal is causal, blind,
and significant is the [ablation study](ablation-study-walkthrough.md), which is what the
`ablation:no-severity` arm here borrows. And it cannot catch a regression on a case you do
not have; if the edit broke a behavior no assertion covers, add the case first
([authoring-evals.md](authoring-evals.md)) — then this loop will guard it.

To formalize the same before/after in a real repo, the ablation stand-in becomes an explicit
`old_skill` arm: populate `manifest.old_skill_paths` with the previous revision and pass
`prepare --include-old-skill`, and the `old_skill` variant grades your last-shipped skill
beside `with_skill` in one report (per the 2026-06-09 `old_skill` lesson in
[`LESSONS_LEARNED.md`](../LESSONS_LEARNED.md), it is opt-in precisely so a benchmark never
compares against a baseline that does not exist). The `iteration-N/` convention plus
`render-viewer --previous-workspace` keeps the report-to-report history across edits. Gate a
PR on the result with [gating-ci-on-evals.md](gating-ci-on-evals.md); an eval is not a test
([evals-are-not-tests.md](evals-are-not-tests.md)), so gate on the confirmed named
regression, not the raw pass count.
