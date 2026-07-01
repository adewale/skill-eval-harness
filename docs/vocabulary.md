# Vocabulary

This page collects the terms the harness uses, with the place each one shows up in a manifest, a command, or a report. The README defines most of them inline where they first appear; this page is the single index so you do not have to hunt.

Terms are grouped by what they describe: the units you evaluate, the comparison structure, the things you assert, the artifacts a run produces, and the signals a report flags.

## Units of evaluation

**Manifest** — the `evals/shared-benchmark.json` file a skill repo owns. It names the skill, declares variants and splits, and lists cases, assertions, and ablations. `validate` checks its shape; every other command reads it.

**Case** — one scenario under test, identified by `id`. A case carries a `prompt`, optional fixture `files`, a `split`, taxonomy fields (`domain`, `difficulty`, `success_goals`, `trigger_type`), `expected_behavior`, and `assertions`.

**Run** — one execution of one case under one variant. Repeated runs of the same case/variant pair produce `run-1`, `run-2`, and so on. Repetition exists because model output varies between runs; one run is not a measurement.

**Fixture** — a real input file referenced by `case.files`, stored under the manifest's `evals/` directory. `prepare` emits fixtures as absolute `input_files` so the runner reads them before answering. Fixtures make a case harder to solve from generic knowledge or from echoing assertion keywords.

## Comparison structure

**Variant** — which arm of the comparison a run belongs to. The two defaults are `with_skill` and `without_skill`. Optional arms are `old_skill` (requires `old_skill_paths` and `--include-old-skill`) and `ablation:<id>`. The harness compares arms; a single arm in isolation says little.

**Ablation** — an opt-in variant that simulates removing one component of a skill, declared under `manifest.ablations` and prepared with `--include-ablations`. Each entry names the `removed_component` and its `expected_regressions`. An ablation is a hypothesis about which instructions are load-bearing; it becomes evidence only once it is run on a discriminating case.

**Split** — when a case is allowed to be seen.

| Split | Visible | Used for |
|---|---|---|
| `tune` | While editing the skill and evals | Iteration |
| `holdout` | At end-of-round or merge | Honest scoring |
| `holdback` | Only after scoring | Detecting memorization |

`holdout` and `holdback` prefer a private `prompt_ref` over an inline `prompt` so the answer is not exposed early. `prepare` fails on missing hidden prompts unless `--allow-missing-prompts` is passed for dry-run planning.

## Things you assert

**Assertion** — a single graded check on a run. Assertions fall into four groups.

**Objective assertion** — a deterministic check on output text or files, graded locally with no model call: `contains`, `contains_any`, `contains_all`, `excludes_any`, `regex`, `not_regex`, `file_exists`, `json_field_equals`.

**Oracle** — a `script` assertion: a deterministic command the repo owns, run against the candidate output directory. Use it when a keyword check is too weak for the property you care about. Oracles are blocked unless you pass `--allow-scripts`, because they execute repo-supplied commands.

**Process assertion** — a check on *how* the run behaved, graded from trace artifacts rather than from the answer: `skill_invoked`, `command_ran` / `command_not_ran`, `command_order`, `tool_count_le`, `no_repeated_command_loop`. Process assertions fail closed when their evidence is missing, so `command_not_ran` cannot pass without `events.json`.

**Efficiency assertion** — a budget check over `metrics.json` or `metadata.json`: `total_tokens_le`, `elapsed_seconds_le`, `command_count_le`.

**Qualitative assertion** — a `judge` or `rubric` check that the harness cannot grade by string matching. These are deferred into `judge-tasks.jsonl` and resolved either by a user-supplied `--judge-cmd` or by merging `--judge-results`. The harness never picks a model for you.

**Variant-scoped assertion** — an assertion restricted to specific arms via `variants` / `only_variants` / `except_variants`. Process checks need this: `skill_invoked=true` belongs to `with_skill`, and `skill_invoked=false` belongs to `without_skill`, so an unscoped skill-load requirement would wrongly penalize the baseline.

## Run artifacts

**`output.md`** — the final answer a run produced. Objective and qualitative assertions read it.

**`metadata.json`** — optional per-run telemetry: elapsed time, token counts, model name.

**Trace artifacts** — what a trace-aware runner writes so process and efficiency assertions have evidence:

- `trace.jsonl` — the raw runner event stream, preserved before normalization.
- `events.json` — normalized events that process assertions read.
- `metrics.json` — tokens, command counts, tool calls, elapsed time, retries.
- `environment.json` — runner, model, and sandbox details where available.

The normalized shapes are an adapter boundary: Pi, Codex, and Jetty emit different raw events, so each shape gets fixture tests rather than an assumed common schema.

## Report signals

These are flags a `benchmark` report raises so you read pass rates correctly.

**Lift** — the difference in objective pass rate between `with_skill` and `without_skill`. Lift, not a single arm's pass rate, is the evidence that a skill changed behavior.

**Discrimination** — an assertion's ability to separate the arms. An assertion with identical with/without pass rates discriminates nothing, whatever its individual pass rate.

**Saturated** — every `with_skill` run passes. That is not a skill failure, but it is weak evidence of lift, and it is a different measurement from skill quality.

**No-lift** — `with_skill` and `without_skill` pass at the same rate, so the case shows no skill effect. Distinct from a failed run.

**Flaky** — repeated runs of the same case/variant disagree. Flakiness is why runs repeat and why one pass is not a result.

**Leakage** — an assertion value appears literally in the prompt, so a weak answer can pass by echoing the task. `validate` warns on this; `--strict-leakage` turns the warning into a failure once you have replaced the weak check.

**Trigger / no-trigger** — whether a skill should load for a given query. A trigger case asserts autonomous skill *discovery*, detected from copied temp skill paths in the trace, not from the final answer and not from a bare skill name. Trigger behavior depends on the discovery-layer frontmatter (`description`/`when_to_use`), so a **discovery-population** ablation is measured *on* trigger cases — through `run_pi_trigger_eval.py --ablation`, which observes autonomous loading — while **answer-population** ablations (instructions/resource/runtime/preprocess) skip trigger cases. The forced-load generic runners never measure discovery ablations.

**Missing output** — a case/variant that was never run. It is marked `missing_output` and excluded from no-lift and saturation comparisons, because "not measured" is not "measured and failed."

**Token overhead** — the static `SKILL.md` and reference footprint combined with the paired `with_skill - without_skill` token delta, reported as objective lift per 1k extra tokens. It answers whether the lift was worth the context the skill consumed.

**Cost** — real dollars a run spent, recorded by a runner that reports it (`run-claude` captures `total_cost_usd` into `metrics.json`). The benchmark report totals `cost_usd_total` per arm over scorable runs, so "what did this arm cost to run" sits next to "how much did it lift."

**Base-saturated** — a case whose *measured* `with_skill` and `without_skill` combined pass rates are equal: the base model does it with or without the skill, so the case measures nothing. Surfaced by `eval-readiness` from run data as a blocker. (Contrast **leak-saturated**, which is a static property of the prompt.)

**Qualitative-only** — a case whose objective pass rates are flat across arms but whose *combined* (judge-inclusive) score lifts with the skill: the skill's value is qualitative, and an objective-only reading would call it useless. Surfaced by `eval-readiness` from run data. **Objective-only** is the static cousin: a behaviour case with no judge assertion, so it can only ever measure objective compliance.

## Populations and evidence

**Population** — which axis a measurement is on. **Answer** population: given a task, does the output meet the assertions (a paired `with_skill` vs `without_skill` comparison; the benchmark report stamps `population: "answer"`). **Trigger / discovery** population: does the skill *load on its own* for a prompt (a single arm, measured by `run_pi_trigger_eval.py`). The two are graded differently (a NO_TRIGGER case *passes by the skill not firing*), so their pass-rates are not comparable — the benchmark report excludes trigger cases and lists them under `skipped_trigger_cases`.

**Evidence class** — how much a number is worth. `EvidenceClass` has five members: `confirmed_causal` (a provenance-gated paired ablation comparison — `causal_confirmation` is the only door to it), `refuted`, `raw_measurement` (a single-arm measurement, no pairing), `indeterminate` (provenance didn't verify), and `unmeasured` (no scorable runs). The trigger report spells its report-level label `raw_autonomous_trigger_measurement` — read it as the trigger-path spelling of `raw_measurement` (its per-result `measurement` field uses the enum value directly).

**Judge-sensitivity** — whether a skill's measured lift depends on *which* model judged it. `compare-judges` flags `sign_sensitive` (judges disagree the skill even helps) and `magnitude_sensitive` (the with−without lift spread across judges exceeds a threshold). Every verdict records its `judge_model`, so a single judge number is never mistaken for a judge-independent one.

## See also

- [`evals-are-not-tests.md`](evals-are-not-tests.md) — why these terms exist and why a test-suite vocabulary does not cover them.
- [`../README.md`](../README.md) — manifest format, assertion reference, and command contracts.
- [`../LESSONS_LEARNED.md`](../LESSONS_LEARNED.md) — the iteration history that produced several of these terms.
