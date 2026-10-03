# Is my skill worth its tokens?

Every skill you ship rides in the model's context on every request that loads it —
the `SKILL.md`, its frontmatter, and whichever `references/` it pulls in. That text is
the visible cost and usually the smaller one. Once it sits in a cached prefix, later
requests read it at about a tenth of the fresh-input price, and the cost-reduction
guide that `/claude-api hillclimb` follows (in the claude-api skill) measured that
"cost scales with extra actions triggered, not prompt length." The larger term is what
the skill makes the model *do*: the extra tool calls, file reads, and output tokens a
with-skill run spends beyond its without-skill pair. The naive version of the question
wants one number ("my skill adds 9 KB, is that OK?"), but 9 KB is only part of the
*bill*; whether it is *worth* it is the bill weighed against the **lift** those tokens
buy — the with-skill minus without-skill pass-rate delta from the same paired cases
the benchmark already runs. A skill that adds 4 KB and lifts nothing is worse than one
that adds 12 KB and turns a 0.2 pass rate into 0.9. So the question is not "how big is it" but "what is the
lift per token, and is any of that footprint buying nothing?"

That splits into two measurements, and they need different evidence:

- **Static footprint** is deterministic and free — no model, no run. `profile-skill`
  counts it: the text every loading request carries, usually the smaller term once
  cached.
- **Runtime lift and dollar cost** need real runs with telemetry, and they hold the
  larger term. `token-overhead` joins the static footprint to the measured objective
  lift, the with-minus-without total-token delta, and (when the runner recorded it)
  the dollar delta; `cost-summary` rolls up spend across a whole suite; the
  benchmark's `trajectory_diff` shows where the extra spend went, as per-case
  `tool_calls`, `file_reads`, and `commands` deltas. These are only as real as the
  token/cost numbers and traces your runner actually wrote.

## Run the static half offline

The static footprint costs nothing to measure and needs no runner. On the bundled
demo ([`examples/demo-skill/`](../examples/demo-skill/)):

```bash
cd examples/demo-skill
python3 ../../skill_benchmark.py profile-skill evals/shared-benchmark.json --format markdown
```

Real output (2026-07-05, Python 3.11):

```text
# Skill profile — demo-reviewer

## Summary

| Metric | Value |
|---|---:|
| skill_files | 1 |
| skill_tokens | 144 |
| reference_files | 1 |
| reference_tokens | 51 |
| modules | 2 |

## Findings

- No profile findings.
```

That is the whole standing cost of the demo skill: 144 tokens of `SKILL.md` plus 51
tokens of one reference, two loadable modules. `profile-skill` is where a "my skill is
getting big" worry starts — set `--max-skill-tokens` / `--max-reference-tokens` /
`--max-references` and it emits a finding when a component crosses your budget, so a
reference that has quietly grown past its keep shows up here before you pay for a run.

## Run the runtime half — and see why the demo can't fake it

Now join footprint to lift. `token-overhead` reads the same paired runs the benchmark
graded and reports lift-per-token and lift-per-dollar per skill. The demo's cases carry judge
assertions, so pass the verdicts the demo README's `judge` step wrote; without them the lift
is withheld as partial coverage:

```bash
python3 ../../skill_benchmark.py token-overhead evals/shared-benchmark.json \
  --runs /tmp/demo-runs --judge-results /tmp/demo-judge.jsonl --format markdown
```

Real output against the offline stub runs (2026-10-02, six repeats per arm; the summary
table, before the per-case pairs):

```text
# Token overhead report

| Skill | Static SKILL tokens | Reference tokens | Runtime pairs | Mean total delta | Median total delta | Mean input delta | Mean objective lift | Lift per 1k total tokens | Mean cost delta USD | Lift per $ | Saturated/no-lift cost USD |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| demo-reviewer | 144 | 51 | 12 | — | — | — | 1.0 | — | — | — | 0.0 |
```

**Twelve runtime pairs and a lift of 1.0, but every token and cost delta is `—`** (the
JSON report holds `null` there). The
lift is real arithmetic over the stub's answers; the deltas are missing because the stub
stands in for a model and has no tokens to report. Each run's trace-normalized usage block
reads zero, and a zero-token basis cannot divide a lift, so each pair's token comparison
is blocked with reason `basis_missing` and the lift-per-dollar status reads `missing_left`.
`cost-summary` shows the same split:

```bash
python3 ../../skill_benchmark.py cost-summary \
  --manifest evals/shared-benchmark.json --runs /tmp/demo-runs
```

```json
"coverage": {
  "runs_seen": 48,
  "runs_with_token_usage": 48,
  "runs_with_dollar_cost": 0,
  "runs_with_non_usd_cost": 0,
  "runs_missing_usage": 0,
  "runs_missing_cost": 48
}
```

All 48 runs (two cases, four arms, six repeats) carry a usage block, and none carries a
dollar cost. The harness records the missing cost as `source: "missing"` in each run's
`metadata.json` rather than reporting `0`: a missing number and a zero number are
different claims, and the ledger keeps them apart.

To get real runtime numbers, run the same cases through a runner that captures
telemetry. `run-claude` parses the `claude -p` JSON envelope and records
`usage_normalized` / `cost_normalized`; the Pi smoke runner does the same. Re-run
`token-overhead` / `cost-summary` against *those* runs and the `—` cells become the deltas
below.

## Reading the numbers, symptom by symptom

Once the runtime pairs are real, read the row for the keep/trim/cut decision:

- **High `Lift per 1k total tokens` / `Lift per $`** → the footprint is earning its
  keep. Leave it. This is the case the skill exists for.
- **Positive footprint, `Mean objective lift` ≈ 0** → you are paying tokens for nothing
  measurable. Either the cases are **saturated** (the base model already passes them —
  the benchmark's `saturated`/`no-lift` case flags catch this) so the eval can't *see*
  the lift, or the skill genuinely isn't helping. Check the flags before you cut:
  saturation is an eval problem, no-lift is a skill problem. `Saturated/no-lift cost
  USD` totals exactly the spend on cases that bought no lift — that column is the
  trim list.
- **Large `Reference tokens`, small lift** → suspect a reference. `profile-skill`
  tells you which module carries the bytes. Before you drop it and re-run, write down
  three gates, following the adoption gates in the `/claude-api hillclimb` cost
  guide: a quality band (the trimmed skill's lift stays within a named distance of
  the current lift), a cost margin (runtime tokens or dollars fall by more than a
  named amount), and a mechanism (the saving shows up where you predicted, such as a
  smaller `file_reads` or `tool_calls` delta in `trajectory_diff`). Cut the reference
  only when all three pass. A lower bill with no visible mechanism is a confound, and
  a reference the model rarely read was nearly free, so deleting it may not cut cost
  at all. (This is a footprint ablation you can do by hand; the [ablation
  study](ablation-study-walkthrough.md) does the causal version.)
- **`with_skill` already passes every case** → quality has nowhere left to climb on
  this suite, so make the objective the same quality at lower cost. The blog post
  [Automating eval design and hillclimbing with
  Claude](https://claude.dev/blog/automating-eval-design-and-hillclimbing/) calls cost
  "one generally strong objective" because you can still pursue it "even if an
  evaluation is saturated"; the gates above are how you hold quality at parity while
  you cut.
- **`audit-manifest --runs <dir>`** folds the same signal into review findings —
  expensive-but-saturated cases, high-cost judge-only cases with no deterministic
  oracle — so the cost view shows up next to the manifest hygiene view.

A worked scale for what "expensive" looks like on real models: a 2026 ten-skill suite
run recorded in [issue #21] billed **$175.21** across 2,568 generation calls, and a
*single* skill — `swiss-poster-skill` at 1,272 runs and ~44.8M tokens — was **~$124**
of it. The ablation arms alone were $157.91 against $17.30 for the plain with/without
baseline. That is the shape of the decision: most of the money is in a few skills and
in the ablation matrix, so "is my skill worth its tokens" is usually really "is *this*
skill, on *these* cases, at *this* repeat count, worth its share of the suite."

## What keeps the measurement honest

- **Footprint is deterministic; lift is not.** `profile-skill` is exact and repeatable.
  A single-shot lift number is a coin flip — the same repetition discipline the rest of
  the harness insists on applies here. Read a one-run `token-overhead` delta as a hint,
  not a verdict.
- **Missing telemetry is not zero cost.** The ledger's `source: "missing"` /
  `not_applicable` markers exist because a runner that forgot to record usage would
  otherwise report a skill as free. If `runs_with_dollar_cost` is below `runs_seen`,
  your dollar totals are *underreported*, not low — fix the runner before you trust the
  bill.
- **Provider-reported beats estimated.** When a runner records the provider's own cost,
  the ledger stamps `source: "provider_reported"`; a price-table estimate is stamped
  `price_table_estimated` with its table version. Don't compare a provider-reported
  total to an estimated one and call it a regression.
- **Exclude execution errors from the lift, keep them in the bill.** A timed-out run
  cost money but proves nothing about quality; the reports count it in operational cost
  and drop it from the pass-rate denominator.

## Where this stops

This journey decides whether a skill's footprint is *worth it on the cases you have*.
It does not tell you which single component is load-bearing — that is a causal claim,
and cutting a reference and eyeballing the lift is not the same as a provenance-gated,
significance-tested removal. When `token-overhead` says a reference looks like dead
weight, confirm it with a materialized ablation in the
[ablation study walkthrough](ablation-study-walkthrough.md) before you delete it.

[issue #21]: https://github.com/adewale/skill-eval-harness/issues/21
