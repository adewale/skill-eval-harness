# Did removing this description actually break discovery?

You trimmed a skill's discovery text — dropped the `when_to_use` hints, cut a
sentence from the `description` — and want to know whether the skill still loads
when it should. Comparing two trigger-matrix reports by eye ("24/24 before, 6/24
after") is not the answer. Each report is a single-arm raw measurement. Two runs
can differ by chance, measure different skill revisions, or ask different
questions, and a handful of queries cannot tell a real regression from noise.

The measurable substitute is an **evidence class**. `skill-benchmark
trigger-compare` pairs a baseline matrix run with an `--ablation` run of the same
canonical skill revision. It reduces each authored query to one pass-rate delta,
sign-flip-tests those deltas, and routes the verdict through the same causal gate
the answer path's ablations use. You get `confirmed_causal`, `refuted`, or
`indeterminate`, and each says what to do next. This journey assumes you already
measure activation ([`tuning-skill-activation.md`](tuning-skill-activation.md)).

## Run it on the bundled demo

The demo skill ([`examples/demo-skill/`](../examples/demo-skill/)) declares a
discovery ablation, `weaker-description`, that removes the `when_to_use` trigger
hints from `SKILL.md`. The offline stub agent routes on the discovery text of the
skill actually mounted — `description` plus `when_to_use` — and fires when a
query shares two content words with it. So the ablation is measurable with no
model. Start with the manifest's own trigger cases: one should-fire and one
should-not-fire.

```bash
cd examples/demo-skill
M=evals/shared-benchmark.json
S=/tmp/discovery    # any unique scratch dir
rm -rf "$S"; mkdir -p "$S"

skill-trigger-matrix $M --agent stub --runs-per-query 3 --out "$S/base.json"
skill-trigger-matrix $M --agent stub --runs-per-query 3 \
  --ablation weaker-description --out "$S/abl.json"
skill-benchmark trigger-compare --baseline "$S/base.json" --ablation "$S/abl.json" \
  --out "$S/compare.json"
```

The comparison (2026-09-26, offline stub; trimmed to the verdict fields):

```json
{
  "evidence_class": "refuted",
  "summary": {"comparable": 2, "comparable_cells": 2, "blocked": 0, "regressed": 0,
              "availability": "complete", "mean_pass_delta": 0.0},
  "note": null
}
```

**Refuted, on the queries you ran.** Provenance verified, both queries comparable,
no drop, so the gate says `refuted`. But look at what was asked. The one
should-fire query ("Review this proposed change and label how serious each finding
is…") shares five words with the `description` alone. Removing `when_to_use` could
never have changed its outcome. `refuted` means *no regression on these queries*.
It does not mean *`when_to_use` is dead weight*.

## Ask in your users' words

The words `when_to_use` adds that the `description` lacks include "inspect",
"diff", "code", "serious", and "asked". Real requests use them: "Can you inspect
this diff?" Start with three such queries in both arms. The harness rejects a
comparison whose two runs used different query sets, so pass the same
`--eval-set` to both:

```bash
cat > "$S/three.json" <<'EOF'
{"queries": [
  {"id": "wtu-inspect-diff", "query": "Can you inspect this diff?", "should_trigger": true},
  {"id": "wtu-serious-code", "query": "How serious is this bug in my code?", "should_trigger": true},
  {"id": "wtu-inspect-code", "query": "Please inspect the code in this diff.", "should_trigger": true}
]}
EOF
skill-trigger-matrix $M --agent stub --runs-per-query 3 --eval-set "$S/three.json" \
  --out "$S/base-three.json"
skill-trigger-matrix $M --agent stub --runs-per-query 3 --eval-set "$S/three.json" \
  --ablation weaker-description --out "$S/abl-three.json"
skill-benchmark trigger-compare --baseline "$S/base-three.json" \
  --ablation "$S/abl-three.json" --out "$S/compare-three.json"
```

```json
{
  "evidence_class": "indeterminate",
  "summary": {"comparable": 3, "comparable_cells": 3, "blocked": 0, "regressed": 3,
              "availability": "complete", "mean_pass_delta": -1.0},
  "note": "regression observed but not significant across queries (p=0.25, mean delta=-1.0); >= 6 consistently regressed queries are needed to confirm"
}
```

All three regressed, every run, and the verdict is still `indeterminate`. The
inference unit is the authored query. Under the null hypothesis each query's delta
is equally likely to point either way, so three unanimous drops carry
p = 2/2³ = 0.25. Repeats do not help: running each query more often sharpens its
own rate but adds no queries. Only more *queries* narrow the uncertainty.

The demo ships a fuller eval set, `evals/trigger-eval-set.json`: two should-fire
queries in the `description`'s words, six in `when_to_use`'s words, and three
should-not-fire queries. Run the same three commands with `--eval-set
evals/trigger-eval-set.json`. The matrix tables already show the story
(2026-09-26):

```text
baseline:  stub     default           24/24              9/9     33/33
ablated:   stub     default            6/24              9/9     15/33
```

The comparison turns that into evidence:

```json
{
  "evidence_class": "confirmed_causal",
  "summary": {"comparable": 11, "comparable_cells": 11, "blocked": 0, "regressed": 6,
              "availability": "complete", "mean_pass_delta": -0.5454545454545454},
  "note": null
}
```

Six queries regressed: `wtu-inspect-diff`, `wtu-serious-problems`,
`wtu-serious-code`, `wtu-asked-check`, `wtu-code-diff-serious`, and
`wtu-inspect-code`, all from `regressed_queries`. They are exactly the six phrased
only in `when_to_use`'s words. The other five queries moved zero, which adds
nothing to the test, so six unanimous drops give p = 2/2⁶ = 0.03125. That clears
0.05 in the regression direction. On this agent, removing `when_to_use` broke
discovery for requests phrased the way those six are.

## Reading the evidence class, symptom by symptom

- **`confirmed_causal`.** The removal broke discovery on the queries listed in
  `regressed_queries`. Restore the field, or move the phrases those queries depend
  on into the `description`, and re-run both arms before shipping.
- **`indeterminate` with a "not significant" note.** Drops were observed, but too
  few queries carry them. Author more queries in the phrasing that regressed. Six
  consistently regressed queries is the floor for p ≤ 0.05; raising
  `--runs-per-query` does not move it.
- **`indeterminate` with a "provenance unverified" note.** The two runs are not a
  valid pair. Different skill revisions, a different query set, or a mismatched
  protocol all land here. Re-run both arms from one checkout with the same flags.
- **`refuted`.** No drop on these queries. Before calling the removed text dead
  weight, check that some queries were phrased in *its* words — the manifest-only
  run above was refuted for exactly that reason.
- **Blocked pairs in `paired.blocked`.** A query is missing an arm, or one arm's
  observations are incomplete (a crash or timeout). The report names the reason.
  Fix and re-run the cell; the comparison never averages around it.
- **A should-not-fire query regresses.** Pass rates, not trigger rates, carry the
  verdict. A negative query regresses by firing, so an edit that made the skill
  greedier shows up here too.

## What keeps the measurement honest

- **Same revision, same questions.** The comparison checks that the ablation's
  parent skill hash matches the baseline's skill tree hash, and that both runs used
  one protocol. A mismatch cannot confirm anything.
- **The unit is the authored query.** Repetitions of a query, and the models in a
  cell, are aggregated into one delta per query before testing, so a large matrix
  cannot manufacture significance from a few questions.
- **Significance counts only in the regression direction.** A two-sided test can be
  significant because an ablation *improved* most queries. That can never promote a
  lone drop to `confirmed_causal`.
- **Only complete observations count.** Incomplete cells block their pair rather than
  entering a mean.
- **The stub is a deterministic stand-in.** It proves the plumbing and the reading,
  and it routes on exactly the text you removed. Real agents route
  probabilistically, which is why the live runs below keep `--runs-per-query 3`.

## Where this stops

A confirmed regression on the stub shows the stub routes on `when_to_use`. Whether
Claude Code, Codex, Pi, or Vibe do is the live matrix's question: run the same
commands with `--agent claude` (or any registered agent). The same queries and the
same gate apply, and the answer may differ by agent and model
([`tuning-skill-activation.md`](tuning-skill-activation.md) shows one description
routing 3/3 on Opus and 1/3 on Haiku). This journey measures discovery only.
Whether the skill still produces good answers once loaded is the answer path's
question ([`did-my-skill-edit-regress.md`](did-my-skill-edit-regress.md)).
Ablations remove text; testing a *rewritten* description against the original
(a swap) is not yet supported ([`skill-ablation-spec.md`](skill-ablation-spec.md)).
