# Vocabulary

This page is the canonical glossary: each term is defined here once, with the place it shows up in a manifest, a command, or a report. The other concept docs apply a lens to these terms rather than redefine them — [`abstractions.md`](abstractions.md) the engineering shape, [`academic-grounding.md`](academic-grounding.md) the research construct, [`evals-are-not-tests.md`](evals-are-not-tests.md) how to read the number — so when a definition changes, it changes here and the lenses follow. (The README still defines a term inline where you first meet it in a workflow; that is reference-at-use, not a second home for the definition.)

Terms are grouped by what they describe: the units you evaluate, the comparison structure, the things you assert, the artifacts a run produces, and the signals a report flags.

## Units of evaluation

**Manifest** — the `evals/shared-benchmark.json` file a skill repo owns. It names the skill, declares variants and splits, and lists cases, assertions, and ablations. `validate` checks its shape; every other command reads it.

**Case** — one scenario under test, identified by `id`. A case carries a `prompt`, optional fixture `files`, a `split`, taxonomy fields (`domain`, `difficulty`, `success_goals`, `trigger_type`), `expected_behavior`, and `assertions`.

**Run** — one execution of one case under one variant. Repeated runs of the same case/variant pair produce `run-1`, `run-2`, and so on. Repetition exists because model output varies between runs; one run is not a measurement.

**Fixture** — a real input file referenced by `case.files`, stored under the manifest's `evals/` directory. `prepare` emits fixtures as absolute `input_files` so the runner reads them before answering. Fixtures make a case harder to solve from generic knowledge or from echoing assertion keywords.

**Dataset / template** — a `datasets` block plus a case `template` fans one case shape over a row set, filling `{key}` placeholders per row into stable case ids. Materialized early (inside `iter_cases`), so validation, leakage lint, prepare, and grading all see ordinary cases. A YAML manifest with `dataset_files` (JSONL row files) compiles to the same shape in memory.

**Multi-turn case (`turns`)** — a case may declare a scripted `turns` sequence instead of a single prompt; each turn's assertions grade that turn's transcript entry (`turn-<n>/output.md`), case-level assertions grade the final answer, and the final turn stands in as the run's `output.md`. Single-shot cases are unchanged.

**Manifest version / `migrate`** — `validate` accepts `version` 1 and 2 (both grade identically; version 2 makes the severity and oracle-tier defaults explicit). `skill-benchmark migrate` stamps the mechanical defaults and prints the diff plus a checklist of the judgment calls it leaves; see [`migrating-evals.md`](migrating-evals.md).

## Case polarity

What role a case plays in the comparison. A useful suite carries all three polarities, the way
behavioral testing pairs functionality tests with their controls (Ribeiro et al. 2020).
`audit-manifest` counts each and warns when one is thin; `case_polarity` (`skill_benchmark.py`)
derives the label from the `id` prefix or `kind`.

**Positive eval** — the skill *should* fire and leave verifiable evidence of its core workflow.
Convention: `id` prefix `pos-`, or a task-success `kind`. The lift axis lives here. This is a
minimum functionality test.

**Negative eval** — the skill should be a no-op: a general checklist would overreach, but the
right move is to stay scoped or do nothing. Convention: `id` prefix `neg-`, or `kind: "negative"`.
It is the false-positive control that keeps a skill from over-applying.

**Adversarial eval** — a near-miss: a prompt that looks like it needs the skill but should be
refused, scoped down, or handled cautiously. `kind: "adversarial"`. In the literature this is a
**contrast set** (Gardner et al. 2020), a small perturbation near the decision boundary, and is
deliberately *not* an adversarial-robustness example. Read its pass rate as a discrimination
signal, not a capability score; see [`academic-grounding.md`](academic-grounding.md).

Trigger polarity is the load-time analogue, defined under **Trigger / no-trigger** below.

## Comparison structure

**Variant** — which arm of the comparison a run belongs to. The two defaults are `with_skill` and `without_skill`. Optional arms are `old_skill` (requires `old_skill_paths` and `--include-old-skill`) and `ablation:<id>`. The harness compares arms; a single arm in isolation says little.

**Ablation** — an opt-in variant that simulates removing one component of a skill, declared under `manifest.ablations` and prepared with `--include-ablations`. Each entry names the `removed_component` and its `expected_regressions`. An ablation is a hypothesis about which instructions are load-bearing; it becomes evidence only once it is run on a discriminating case. It is an ablation study in the original sense (Newell; Meyes et al. 2019) applied to instruction components, and because each entry declares `expected_regressions` it doubles as a directional-expectation (DIR) test (Ribeiro et al. 2020): a perturbation with a predicted direction of change.

**Model (axis)** — a third fan-out axis beside variant and run, set with `prepare --models a,b,c`. Each row carries its target model, and the run layout gains a model segment (`<case>/<model>/<variant>`) only when two or more models run, so single-model layouts are unchanged. The report groups `by_model`, pairs lift per (case, model), and `model_analysis` ranks models by lift and names the ones that lose it. Model is a dimension, not a new kind of variant — the variant grid stays orthogonal within each model.

**Split** — when a case is allowed to be seen.

| Split | Visible | Used for |
|---|---|---|
| `tune` | While editing the skill and evals | Iteration |
| `holdout` | At end-of-round or merge | Honest scoring |
| `holdback` | Only after scoring | Detecting memorization |

`holdout` and `holdback` prefer a private `prompt_ref` over an inline `prompt` so the answer is not exposed early. `prepare` fails on missing hidden prompts unless `--allow-missing-prompts` is passed for dry-run planning. Holding cases back is the harness's contamination control: a case kept out of the skill, the docs, and the eval text cannot have been memorized, so a high score on it is evidence rather than leakage.

## Things you assert

**Assertion** — a single graded check on a run. Assertions fall into four groups.

**Objective assertion** — a deterministic check on output text or files, graded locally with no model call: `contains`, `contains_any`, `contains_all`, `excludes_any`, `regex`, `not_regex`, `file_exists`, `json_field_equals`, `golden_output` (equality against a reference file, with explicit normalization and a diff on mismatch), `similarity` (a `difflib` ratio against an `expected` string, thresholded and scored; `mode: "embedding"` swaps in cosine similarity behind the opt-in `--embed-cmd`), and `structured_output` (JSON validated against a schema subset).

**Script oracle** — a `script` assertion: a deterministic command the repo owns, run against the candidate output directory. Use it when a keyword check is too weak for the property you care about. Blocked unless you pass `--allow-scripts`, because it executes repo-supplied commands. A `{"score", "max_score"}` line on its stdout turns it into a graded oracle without giving up determinism.

**Oracle tier** — how trustworthy an assertion's evidence is, declared per assertion or defaulted by type: `strong` (deterministic, no-lies — the default for text/process/efficiency), `demo` (a marked stand-in — the default for `script`), or `live` (model-backed — the default for `judge`). The benchmark report shows each case's `strong`-oracle share, and `audit-manifest` warns on a case graded only by `demo`/`live` oracles.

**Process assertion** — a check on *how* the run behaved, graded from trace artifacts rather than from the answer: `skill_invoked`, `command_ran` / `command_not_ran`, `command_order`, `tool_call` (a completed tool call matching `tool`/`pattern`, with order/count bounds — over both shell-command and normalized `tool_call` events), `tool_count_le`, `no_repeated_command_loop`. Process assertions fail closed when their evidence is missing, so `command_not_ran` cannot pass without `events.json`.

**Efficiency assertion** — a budget check over `metrics.json` or `metadata.json`: `total_tokens_le`, `elapsed_seconds_le`, `command_count_le`.

**Qualitative assertion** — a `judge` or `rubric` check (or the `factuality` preset, a canned anchored rubric) that the harness cannot grade by string matching. A `judge` assertion may carry anchored `graded_dimensions` (per-dimension 1–5 scores) or a `dynamic_rubric` (the judge drafts case-specific criteria, then grades against them). These are deferred into `judge-tasks.jsonl` and resolved either by a user-supplied `--judge-cmd` or by merging `--judge-results`. The harness never picks a model for you.

**Severity** — how a failed assertion counts, declared per assertion (or defaulted by type): `critical` (an absorbing barrier — one failure vetoes the run, collapses every rate to 0.0, and is excluded from every mean), `gate` (carries the pass rate; the default for objective checks), or `soft` (feeds only the per-run graded score and never moves a pass rate; the default for `judge`/`similarity`). `--strict` promotes soft to gate.

**Graded score** — the "how much better" channel beside binary pass/fail. Every assertion result carries a 0–1 `score`; soft results feed a per-run `graded_score`, and `build_paired_summary` reports a paired `graded` channel plus a sign-flip permutation significance test beside the raw lift. An optional `reference_score` / `reference_graded_score` on a case sets a no-regression floor.

**Variant-scoped assertion** — an assertion restricted to specific arms via `variants` / `only_variants` / `except_variants`. Process checks need this: `skill_invoked=true` belongs to `with_skill`, and `skill_invoked=false` belongs to `without_skill`, so an unscoped skill-load requirement would wrongly penalize the baseline.

## Run artifacts

**`output.md`** — the final answer a run produced. Objective and qualitative assertions read it.

**`metadata.json`** — optional per-run telemetry: elapsed time, token counts, model name, and the normalized cost blocks below.

**`usage_normalized` / `cost_normalized`** — the normalized token and dollar blocks every runner writes into a run's metadata/metrics, alongside the raw provider fields. Each carries a `source` provenance (`provider_reported`, `trace_normalized`, `price_table_estimated`, `missing`, or `not_applicable`) — missing telemetry is marked, never written as zero — so the report can total real spend and disclose coverage separately from quality.

**Trace artifacts** — what a trace-aware runner writes so process and efficiency assertions have evidence:

- `trace.jsonl` — the raw runner event stream, preserved before normalization.
- `events.json` — normalized events that process assertions read.
- `metrics.json` — tokens, command counts, tool calls, elapsed time, retries.
- `environment.json` — runner, model, and sandbox details where available.

The normalized shapes are an adapter boundary: Pi, Codex, and Jetty emit different raw events, so each shape gets fixture tests rather than an assumed common schema.

## Report signals

These are flags a `benchmark` report raises so you read pass rates correctly.

**Lift** — the difference in objective pass rate between `with_skill` and `without_skill`. Lift, not a single arm's pass rate, is the evidence that a skill changed behavior. In causal-inference terms it is the skill's average treatment effect in a paired design; the per-slice lift in `build_slice_summary` is a conditional treatment effect.

**Discrimination** — an assertion's ability to separate the arms. An assertion with identical with/without pass rates discriminates nothing, whatever its individual pass rate.

**Saturated** — every `with_skill` run passes. That is not a skill failure, but it is weak evidence of lift, and it is a different measurement from skill quality. It is a ceiling effect: the case has stopped discriminating, which is a construct-validity warning that it no longer measures the skill.

**No-lift** — `with_skill` and `without_skill` pass at the same rate, so the case shows no skill effect. Distinct from a failed run.

**Negative delta** — `with_skill` passes at a *lower* rate than `without_skill`: the skill actively hurt the case, surfaced as `negative_delta_cases` in `build_paired_summary`. Distinct from a *negative eval*, which is a case designed to test a no-op; this is a negative treatment effect.

**Flaky** — repeated runs of the same case/variant disagree. Flakiness is why runs repeat and why one pass is not a result.

**Leakage** — an assertion value appears literally in the prompt, so a weak answer can pass by echoing the task. `validate` warns on this; `--strict-leakage` turns the warning into a failure once you have replaced the weak check. Leakage is an annotation artifact (Gururangan et al. 2018) in eval clothing — a surface cue that lets a model be right for the wrong reasons (McCoy et al. 2019) without exercising the skill.

**Trigger / no-trigger** — whether a skill should load for a given query. A trigger case asserts autonomous skill *discovery*, detected from copied temp skill paths in the trace, not from the final answer and not from a bare skill name. Trigger behavior depends on the discovery-layer frontmatter (`description`/`when_to_use`), so a **discovery-population** ablation is measured *on* trigger cases — through `run_pi_trigger_eval.py --ablation`, which observes autonomous loading — while **answer-population** ablations (instructions/resource/runtime/preprocess) skip trigger cases. The forced-load generic runners never measure discovery ablations.

**Missing output** — a case/variant that was never run. It is marked `missing_output` and excluded from no-lift and saturation comparisons, because "not measured" is not "measured and failed."

**Token overhead** — the static `SKILL.md` and reference footprint combined with the paired `with_skill - without_skill` token delta, reported as objective lift per 1k extra tokens. It answers whether the lift was worth the context the skill consumed.

**Cost** — real dollars a run spent, normalized into `cost_normalized` by every runner that reports it (`run-claude` and `run-subagent` capture provider cost; Pi smoke/trigger parse it from the stream; Jetty from the trajectory). The benchmark report carries a `cost_summary` ledger — operational totals over *all* runs (execution errors included: they were still paid for), per-variant mean/median/p90, paired cost deltas, ablation marginal cost and cost per confirmed regression, and judge spend as its own line. The standalone `cost-summary` command writes the suite ledger (JSON + markdown) with top spenders and spend-without-signal findings; `suite-run` projects spend before any model call and gates on `--max-estimated-cost-usd` / `--max-estimated-tokens`; `token-overhead` adds dollar deltas and lift-per-dollar. Cost sits next to lift, never mixed into it.

**Base-saturated** — a case whose *measured* `with_skill` and `without_skill` combined pass rates are equal: the base model does it with or without the skill, so the case measures nothing. Surfaced by `eval-readiness` from run data as a blocker. (Contrast **leak-saturated**, which is a static property of the prompt.)

**Qualitative-only** — a case whose objective pass rates are flat across arms but whose *combined* (judge-inclusive) score lifts with the skill: the skill's value is qualitative, and an objective-only reading would call it useless. Surfaced by `eval-readiness` from run data. **Objective-only** is the static cousin: a behaviour case with no judge assertion, so it can only ever measure objective compliance.

## Populations and evidence

**Population** — which axis a measurement is on. **Answer** population: given a task, does the output meet the assertions (a paired `with_skill` vs `without_skill` comparison; the benchmark report stamps `population: "answer"`). **Trigger / discovery** population: does the skill *load on its own* for a prompt (a single arm, measured by `run_pi_trigger_eval.py`). The two are graded differently (a NO_TRIGGER case *passes by the skill not firing*), so their pass-rates are not comparable — the benchmark report excludes trigger cases and lists them under `skipped_trigger_cases`.

**Evidence class** — how much a number is worth. `EvidenceClass` has five members: `confirmed_causal` (a provenance-gated paired ablation comparison — `causal_confirmation` is the only door to it), `refuted`, `raw_measurement` (a single-arm measurement, no pairing), `indeterminate` (measured, but provenance, coverage, execution validity, or statistical significance is insufficient — not confirmed and not refuted), and `unmeasured` (no scorable runs). The trigger report spells its report-level label `raw_autonomous_trigger_measurement` — read it as the trigger-path spelling of `raw_measurement` (its per-result `measurement` field uses the enum value directly).

**Judge-sensitivity** — whether a skill's measured lift depends on *which* model judged it. `compare-judges` flags `sign_sensitive` (judges disagree the skill even helps) and `magnitude_sensitive` (the with−without lift spread across judges exceeds a threshold). Every verdict records its `judge_model`, so a single judge number is never mistaken for a judge-independent one.

## See also

- [`evals-are-not-tests.md`](evals-are-not-tests.md) — why these terms exist and why a test-suite vocabulary does not cover them.
- [`../README.md`](../README.md) — manifest format, assertion reference, and the command index.
- [`commands.md`](commands.md) — per-command contracts: flags, examples, and output shapes.
- [`../LESSONS_LEARNED.md`](../LESSONS_LEARNED.md) — the iteration history that produced several of these terms.
- [`academic-grounding.md`](academic-grounding.md) — the research constructs behind these terms, with citations.
