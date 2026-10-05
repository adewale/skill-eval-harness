# Which model should my skill target?

A skill does not buy the same lift on every model. The same guidance that turns a
weak model's 0.2 pass rate into 0.9 can buy *nothing* on a strong one, because the
strong base model already passes those cases without it. The skill hasn't gotten
worse; the model has left it no room to help. So the naive question ("what's the
best model for my skill?") has no
single answer; the measurable version is **on which model tier does my skill still
buy real, significant lift, and where has the base model already saturated the cases
so there is no lift left to see.** You answer it by fanning the same paired cases
across model tiers and reading lift *per tier*, not pooled.

The machinery is a third axis on the run fan-out. `prepare --models` fans every
`(case, variant)` row once per model; grading pairs `with_skill` against
`without_skill` **within each model**, and the report carries the result three ways:
`by_model` (the model-by-variant grid), `paired_summary.by_model` (each tier's own
lift + significance), and `model_analysis` (the tiers ranked by lift, with the ones
that *lose* lift named).

## Run the fan-out offline — and see why the stub can't pick a tier for you

The bundled demo ([`examples/demo-skill/`](../examples/demo-skill/)) runs the whole
three-model loop with no API key. From `examples/demo-skill`:

```bash
H=../../skill_benchmark.py
python3 $H prepare evals/shared-benchmark.json --split tune \
  --models haiku,sonnet,opus --out /tmp/model-tasks.jsonl
python3 $H run-codex --tasks /tmp/model-tasks.jsonl --runs /tmp/model-runs \
  --codex-cmd "python3 $(pwd)/stub_runner.py"
python3 $H judge evals/shared-benchmark.json --runs /tmp/model-runs \
  --variant with_skill --variant without_skill \
  --judge-cmd "python3 $(pwd)/stub_judge.py" --out /tmp/model-judge.jsonl
python3 $H benchmark evals/shared-benchmark.json --runs /tmp/model-runs \
  --variant with_skill --variant without_skill \
  --judge-results /tmp/model-judge.jsonl --out /tmp/model-bench.json
```

The `judge` step grades `c-review`'s `actionable-review` assertion with the demo's offline
stub judge. Without it the report is partial and every `ranking` entry's `lift`,
`with_skill`, and `without_skill` read `null`.

`prepare` emitted **12 rows** — 2 answer cases × 2 variants × 3 models — and stamped
each with a `model` and a model-segmented `run_dir` (`c-review/haiku/with_skill`, …).
`benchmark` discovered that layout on its own: the model segment is recorded in each
run's metadata, so no extra flag is needed to grade the three tiers apart.

Here is the real `model_analysis` block from `/tmp/model-bench.json` (2026-07-06,
Python 3.11; re-run with the judge step 2026-09-29, unchanged):

```json
"ranking": [
  {"model": "haiku",  "lift": 1.0, "with_skill": 1.0, "without_skill": 0.0, "significant_at_0_05": false},
  {"model": "opus",   "lift": 1.0, "with_skill": 1.0, "without_skill": 0.0, "significant_at_0_05": false},
  {"model": "sonnet", "lift": 1.0, "with_skill": 1.0, "without_skill": 0.0, "significant_at_0_05": false}
],
"lift_losers": []
```

**Every tier reports the identical lift of 1.0.** Nothing broke; an offline stub can
only produce that one number. `stub_runner.py` answers by reading the skill tree
the harness mounted. It is deterministic and **model-blind**: it never reads the
`model` label the row carries. So all three model rows run through the same stub and
produce the same output, and `by_model` / `paired_summary.by_model` show the same
delta three times. This is exactly analogous to the token journey's "twelve runtime
pairs, every delta `—`" shape ([`is-my-skill-worth-its-tokens.md`](is-my-skill-worth-its-tokens.md)):
the offline run proves the *plumbing* — that rows fan per model, the model axis
threads through grading into the report, and `ranking` / `lift_losers` populate — but
it cannot prove *divergence*, because the thing that makes tiers differ (a real model
that varies by capability) isn't in the loop. **The divergence only appears on a real
runner.** Do not edit the stub to fake per-model differences; an illustration that
lies about the shape teaches the wrong thing.

One more honest detail in that block: `significant_at_0_05` is `false` even at a lift
of 1.0. With only 2 paired cases the sign-flip permutation test can't reach p ≤ 0.05
(`paired_summary.by_model.haiku.significance` reports `p_value: 0.5`). A per-model lift
is still a paired delta over a handful of cases — the tier ranking is only as
trustworthy as the case count and repeat count behind each cell.

## Reading the ranking, symptom by symptom

On a **real** multi-model run the tiers stop agreeing, and where they disagree is
what tells you which tier to target. Read `model_analysis.ranking` top-to-bottom
against `case_flags`:

- **High, significant lift on a weak tier; lift ≈ 0 on a strong tier, and that
  tier's cases carry the `saturated/non-discriminating` flag** → the skill is *for*
  the weaker tier. The strong tier's base model already clears these cases
  (`with_skill` and `without_skill` both near 1.0), so there is no lift left to
  measure. **Action:** target the tier where the lift is real; do not claim value on
  the saturated tier — you can't see it there, and it may not exist. This is the
  2026-06-09 lesson "Strong models make many evals saturated" (`LESSONS_LEARNED.md`)
  read one tier at a time: `with_skill=1.0` next to `without_skill=1.0` is weak
  evidence of lift, not proof of a good skill.
- **A model in `lift_losers`** (non-positive lift on that tier while the pooled lift
  is positive) → the skill made that tier *worse*. That is a hard reason **not** to
  ship for it. Open `paired_summary.by_model.<model>.negative_delta_cases` and read
  what regressed before you target that tier at all.
- **Uniform, significant lift across every tier** (what the offline demo *shape*
  shows, minus the significance) → the skill helps everywhere; target for breadth.
- **A tier passes `without_skill` on some cases but not others** → the eval, not the
  skill, sets your ceiling on that tier. The `no objective lift` case flag names which
  cases have gone flat there; harder fixtures may reveal lift the easy cases hide.

A live worked example of the underlying effect already lives in the harness:
[`tuning-skill-activation.md`](tuning-skill-activation.md)'s Haiku cell, where the
identical skill that routed Sonnet and Opus 3/3 loaded on only 1 of 3 Haiku runs. That
is the *activation* face of the same truth this journey measures on the *answer* face:
the weakest tier you support sets the bound, and a skill's value is a property of
`skill × model`, never of the skill alone.

## What keeps the measurement honest

- **Stronger tiers should score higher in each arm, not in lift.** The blog post
  [Automating eval design and hillclimbing with
  Claude](https://claude.dev/blog/automating-eval-design-and-hillclimbing/) lists
  "Performance improves with stronger models and more thinking" as a property of a
  well-designed eval. In a paired eval that expectation applies to each arm's absolute
  pass rate in `by_model`: a stronger tier's `with_skill` and `without_skill` rates
  should each sit at or above a weaker tier's. Lift can shrink on a stronger tier
  because its base model needs the skill less, and that is not an inversion. The
  inversion to chase is a stronger tier scoring *lower* than a weaker one in the same
  arm: when that happens, the same post says, "ambiguous tasks or a miscalibrated
  grader often are hobbling performance," so open the cases before you rank. This check
  is a per-arm diagnostic, not one of the harness's eval-health marks
  ([why](comparing-with-claude-api-evals.md#five-marks-of-a-lift-eval)).
- **Pin effort before comparing tiers.** Unless you pass `--effort` to `run-claude`,
  `run-codex`, or `run-agent`, each tier runs at its CLI's default effort, and those
  defaults differ by model and CLI version: per the claude-api skill, Claude Opus
  5.5's API default is `medium` while Claude Opus 5's is `high`. A tier comparison
  at defaults can then measure an effort gap as a model gap. Every run records
  `effort.applied_by: "backend_default"` in that case, and a multi-model report whose
  runs all used defaults says so in `run_endings.notes` ("every run used its backend's
  default effort; defaults differ by model, so pin --effort before reading a
  cross-model comparison"); the offline fan-out above carries exactly that note. Gemini
  and Vibe have no known effort control, so `--effort` is refused on them before any
  run starts.
- **Per-model lift is a paired delta, and its test counts cases.** A single run per
  (case, model) cell is a coin flip, so `prepare --runs-per-variant` steadies each
  cell's rate before you rank tiers. Repeats do not add cases, though: each tier's
  significance test runs over its cases, and no tier can reach p ≤ 0.05 until at least
  6 of them move the same way (see **Inference unit** in
  [`vocabulary.md`](vocabulary.md#report-signals)). To make a tier ranking significant,
  add cases. The offline block above shows `significant_at_0_05: false` precisely
  because it is underpowered — read the significance field, not just the lift.
- **An offline stub cannot tell you which model to pick.** It is model-blind by
  construction, so its identical-across-tiers ranking proves the pipeline and nothing
  about model choice. Only a runner that actually varies by capability
  (`run-claude` across the haiku/sonnet/opus aliases, or another real adapter) makes
  the tiers diverge.
- **Strong-model saturation is an *eval* limitation as much as a skill fact.** Zero
  lift on a strong tier can mean "the skill is unneeded there" or "these cases are too
  easy to expose the lift." The `saturated/non-discriminating` and `no objective lift`
  flags exist to keep those apart — harder cases can move a saturated tier off the
  ceiling and reveal lift that was always there.
- **Evidence class:** this is an unconfirmed paired lift comparison — the same standing
  as the benchmark's `paired_summary`, not the provenance-gated `CONFIRMED_CAUSAL` an
  ablation earns. (`raw_measurement` proper is the single-arm trigger-path label.) It
  ranks tiers; it does not causally attribute the lift to any one component.

## Where this stops

This journey ranks the tiers you fanned over, on the cases you have. It does **not**
tell you the *cheapest* tier that still clears your bar — lift per tier says nothing
about dollars per tier, and the cost ledger does not price tiers either: `cost-summary`
groups spend by case, variant, runner, and ablation, never by model. To compare,
read each tier's `by_model.<model>.with_skill` pass rate beside that tier's spend,
summed from the `cost_normalized` block each run's `metadata.json` records next to its
`model`, or from a separate `cost-summary` over a runs directory that holds one tier.
The harness reports both numbers and never picks the model for you; the per-skill
cost questions are in
[`is-my-skill-worth-its-tokens.md`](is-my-skill-worth-its-tokens.md). And it assumes
the skill actually *loaded* on each tier; whether the description routes on a given
model is a separate, prior question answered by
[`tuning-skill-activation.md`](tuning-skill-activation.md). Rank tiers here, price them
from their runs' spend, and confirm they load before you trust either number.
