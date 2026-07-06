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
python3 $H benchmark evals/shared-benchmark.json --runs /tmp/model-runs \
  --variant with_skill --variant without_skill --out /tmp/model-bench.json
```

`prepare` emitted **12 rows** — 2 answer cases × 2 variants × 3 models — and stamped
each with a `model` and a model-segmented `run_dir` (`c-review/haiku/with_skill`, …).
`benchmark` discovered that layout on its own: the model segment is recorded in each
run's metadata, so no extra flag is needed to grade the three tiers apart.

Here is the real `model_analysis` block from `/tmp/model-bench.json` (2026-07-06,
Python 3.11):

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
delta three times. This is exactly analogous to the token journey's "runtime pairs 0
/ `None`" shape ([`is-my-skill-worth-its-tokens.md`](is-my-skill-worth-its-tokens.md)):
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

- **Per-model lift is a paired delta and needs repeats.** A single run per (case,
  model) cell is a coin flip, exactly like every other lift number in the harness. Use
  `prepare --runs-per-variant` to replicate before you rank tiers; a marginal ordering
  over n=1 cells is noise. The offline block above shows `significant_at_0_05: false`
  precisely because it is underpowered — read the significance field, not just the
  lift.
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
- **Evidence class:** this is a `raw` paired lift comparison — the same class as the
  benchmark's `paired_summary`, not the provenance-gated `CONFIRMED_CAUSAL` an ablation
  earns. It ranks tiers; it does not causally attribute the lift to any one component.

## Where this stops

This journey ranks the tiers you fanned over, on the cases you have. It does **not**
tell you the *cheapest* tier that still clears your bar — lift per tier says nothing
about dollars per tier; that trade-off is the cost journey,
[`is-my-skill-worth-its-tokens.md`](is-my-skill-worth-its-tokens.md). And it assumes
the skill actually *loaded* on each tier; whether the description routes on a given
model is a separate, prior question answered by
[`tuning-skill-activation.md`](tuning-skill-activation.md). Rank tiers here, price them
there, and confirm they load before you trust either number.
