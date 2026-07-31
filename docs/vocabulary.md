# Vocabulary

This page is the canonical glossary: each term is defined here once, with the place it shows up in a manifest, a command, or a report. The other concept docs apply a lens to these terms rather than redefine them — [`abstractions.md`](abstractions.md) the engineering shape, [`academic-grounding.md`](academic-grounding.md) the research construct, [`evals-are-not-tests.md`](evals-are-not-tests.md) how to read the number — so when a definition changes, it changes here and the lenses follow. (The README still defines a term inline where you first meet it in a workflow; that is reference-at-use, not a second home for the definition.)

Terms are grouped by what they describe: the units you evaluate, the comparison structure, the things you assert, the artifacts a run produces, the runners and judges that produce and grade them, and the signals a report flags.

## Units of evaluation

**Manifest** — the `evals/shared-benchmark.json` file a skill repo owns. It names the skill, declares variants and splits, and lists cases, assertions, and ablations. `validate` checks its shape; every other command reads it.

**Case** — one scenario under test, identified by `id`. A case carries a `prompt`, optional fixture `files`, a `split`, taxonomy fields (`domain`, `difficulty`, `success_goals`, `trigger_type`), `expected_behavior`, and `assertions`.

**Run** — one execution of one case under one variant. Repeated runs of the same case/variant pair produce `run-1`, `run-2`, and so on. Repetition exists because model output varies between runs; one run is not a measurement.

**Prepared-task draft / prepared task** — a draft is permissive planning data and cannot execute. A prepared task is the validated runner input produced at the JSONL boundary: non-empty identifiers, a closed split/variant, positive repetition, safe relative run directory, typed ablation provenance, and no skill paths on `without_skill`. Runners accept only the validated type.

**Fixture** — a real input file referenced by `case.files`, stored under the manifest's `evals/` directory. `prepare` emits fixtures as absolute `input_files` so the runner reads them before answering. Fixtures make a case harder to solve from generic knowledge or from echoing assertion keywords.

**Dataset / template** — a `datasets` block plus a case `template` fans one case shape over a row set, filling `{key}` placeholders per row into stable case ids. Materialized early (inside `iter_cases`), so validation, leakage lint, prepare, and grading all see ordinary cases. A YAML manifest with `dataset_files` (JSONL row files) compiles to the same shape in memory.

**Multi-turn case (`turns`)** — a case may declare a scripted `turns` sequence instead of a single prompt; `prompt`, `prompt_ref`, and `turns` are mutually exclusive prompt sources. Each turn's assertions grade that turn's transcript entry (`turn-<n>/output.md`), case-level assertions grade the final answer, and the final turn stands in as the run's `output.md`. Single-shot cases are unchanged.

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

**Experimental pair** — exactly one eligible `with_skill` arm and one eligible `without_skill` arm sharing `(case_id, model, run_number, population)`. The harness constructs this value before lift, paired reliability, paired cost, or token-overhead arithmetic. A missing/ineligible arm is a blocked pair; a duplicate arm is invalid rather than “last row wins.” Telemetry comparisons further require compatible provenance/unit/billing basis before a numeric delta exists.

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

**Human-text comparison** — the immutable view used by `contains`, `regex`, their positive/negative families, and `similarity`. `rendered-v1` is the default: NFC plus removal of three zero-width, non-ordering controls (`U+200B`, `U+2060`, `U+FEFF`) while raw artifacts stay unchanged. Direction-changing controls and soft hyphens remain exact. `comparison: "exact"` opts an assertion out. Grading evidence identifies transformations and verdict changes. Protocol and machine-identity assertions remain exact.

**Script oracle** — a `script` assertion: a deterministic command the repo owns, run against the candidate output directory. Use it when a keyword check is too weak for the property you care about. The script must live below a dedicated manifest-relative directory such as `oracles/`, whose complete tree is committed into the eval contract; root-level scripts are rejected because generated manifest siblings would make that tree unstable. Blocked unless you pass `--allow-scripts`, because it executes repo-supplied commands. A `{"score", "max_score"}` line on its stdout turns it into a graded oracle without giving up determinism.

**Oracle tier** — how trustworthy an assertion's evidence is, declared per assertion or defaulted by type: `strong` (deterministic, no-lies — the default for text/process/efficiency), `demo` (a marked stand-in — the default for `script`), or `live` (model-backed — the default for `judge`). The benchmark report shows each case's `strong`-oracle share, and `audit-manifest` warns on a case graded only by `demo`/`live` oracles.

**Process assertion** — a check on *how* the run behaved, graded from trace artifacts rather than from the answer: `skill_invoked`, `command_ran` / `command_not_ran`, `command_order`, `tool_call` (a completed tool call matching `tool`/`pattern`, with order/count bounds — over both shell-command and normalized `tool_call` events), `tool_count_le`, `no_repeated_command_loop`. Process assertions fail closed when their evidence is missing, so `command_not_ran` cannot pass without `events.json`.

**Efficiency assertion** — a budget check over `metrics.json` or `metadata.json`: `total_tokens_le`, `elapsed_seconds_le`, `command_count_le`.

**Qualitative assertion** — a `judge` or `rubric` check (or the `factuality` preset, a canned anchored rubric) that the harness cannot grade by string matching. A `judge` assertion may carry anchored `graded_dimensions` (per-dimension 1–5 scores), a `dynamic_rubric` (the judge drafts case-specific criteria, then grades against them), or `per_step` (below). These are deferred as keyed judge tasks and resolved by a judge backend (`--judge-cmd`, `--judge-model`, or `--judge-backend`) or by merging pre-computed `--judge-results`; `grade --judge-tasks` optionally serializes the tasks to `judge-tasks.jsonl` for an external or human workflow. The harness never picks a model for you.

**Per-step judge (`per_step`)** — a case-level judge assertion that grades *each completed trajectory step* (command, tool call, file read/write, skill load) instead of only the final answer: one criterion per step in trajectory order, passing when at least `ceil(min_met_fraction × steps)` are judged sound (default: all). The verdict reuses the dynamic-criteria shape, and each step payload resolves the untruncated invocation (`raw_ref`) and result (`raw_result_ref`) separately so the judge sees full tool arguments and outcomes. Stored verdicts bind to a SHA-256 of the exact trajectory step payload and are re-queued if that evidence or its expected criterion set changes. Trace-evidence-backed and fail-closed like a process assertion: no completed steps means the assertion fails at grade time and no judge task is emitted. Turn assertions cannot use `per_step` because turns do not own independent trace artifacts.

**Severity** — how a failed assertion counts, declared per assertion (or defaulted by type): `critical` (an absorbing barrier — one failure vetoes the run, collapses every rate to 0.0, and is excluded from every mean), `gate` (carries the pass rate; the default for objective checks), or `soft` (feeds only the per-run graded score and never moves a pass rate; the default for `judge`/`similarity`). `--strict` promotes soft to gate.

**Graded score** — the "how much better" channel beside binary pass/fail. Every assertion result carries a 0–1 `score`; soft results feed a per-run `graded_score`, and `build_paired_summary` reports a paired `graded` channel plus a sign-flip permutation significance test beside the raw lift. An optional `reference_score` / `reference_graded_score` on a case sets a no-regression floor.

**Variant-scoped assertion** — an assertion restricted to specific arms via `variants` / `only_variants` / `except_variants`. Process checks need this: `skill_invoked=true` belongs to `with_skill`, and `skill_invoked=false` belongs to `without_skill`, so an unscoped skill-load requirement would wrongly penalize the baseline.

**Assertion dependency (`depends_on`)** — an assertion may name prerequisite assertions; when a prerequisite fails or is itself skipped, the dependent is SKIPPED — out of every denominator and out of the critical veto — rather than counted as a failure. Skip is not zero: a dependent that never ran is "not measured", so an upstream miss cannot double-count as two failures. Cycles and unknown targets are rejected at validation.

**Eval intent** — what a case exists to show, declared per case as `eval_intent`: `capability` (the default — the case measures lift and participates in saturation/no-lift/staleness signals) or `regression` (the case pins behavior the skill must not lose; it reports under `regression_guards_holding`, is exempt from staleness and suggestion pruning, and its saturation is the goal, not a warning).

## Run artifacts

**`output.md`** — the final answer a run produced. Objective and qualitative assertions read it.

**`metadata.json`** — optional per-run telemetry: elapsed time, token counts, model name, and the normalized cost blocks below.

**`usage_normalized` / `cost_normalized`** — legacy-compatible normalized token and dollar blocks every runner writes alongside raw provider fields. Schema-v3 `telemetry` is the canonical contract: it separates provenance (`provider_reported`, `trace_normalized`, `price_table_estimated`, `estimated`, or `legacy_unverified`) from availability (`available`, `unavailable`, or `not_applicable`). A measured zero is available; unavailable telemetry is never numeric zero. See [`telemetry-availability-and-comparability-spec.md`](telemetry-availability-and-comparability-spec.md).

**Trace-event lifecycle** — the closed state of one normalized event: completed, in-progress, failed, or unknown, with a recorded source (provider status, intrinsically terminal/start event kind, explicit legacy adaptation, or unknown). Missing or misspelled status is not completion. Only completed operations contribute tool/command/file metrics.

**Trace artifacts** — what a trace-aware runner writes so process and efficiency assertions have evidence:

- `trace.jsonl` — the raw runner event stream, preserved before normalization.
- `events.json` — normalized events that process assertions read.
- `metrics.json` — tokens, command counts, tool calls, elapsed time, retries.
- `environment.json` — runner, model, and sandbox details where available.

The normalized shapes are an adapter boundary: Pi, Codex, and Jetty emit different raw events, so each shape gets fixture tests rather than an assumed common schema.

## Runners and judges

**Agent backend** — one row in `agent_capabilities.BACKENDS`, declaring the provider's capability gates, explicit answer route and executable command entrypoints, native answer/autonomous-trigger/judge bindings, workspace, trace, smoke, failure, and CLI-option policy. `AGENT_BACKENDS`, `JUDGE_BACKENDS`, `run_trigger_matrix.ADAPTERS`, `WORKSPACE_BUILDERS`, `AGENT_CAPABILITIES`, and `SMOKE_TARGETS` remain compatibility projections rather than independent registration points. Implementation dispatch and workspace entries may be replaced temporarily; policy projections are immutable, and a new provider requires a complete row. Native answer runners are dispatched by `run-agent --agent <name>`; `run-codex` and `run-claude` are compatibility wrappers over the same path. `skill-benchmark agent-capabilities` renders the machine-readable view, while [`agent-parity.md`](agent-parity.md) is the reader-facing table.

**Answer-runner outcome** — one frozen execution variant: completed, timed out, spawn failed, or provider failed. Return code, timeout, answer, and failure are not independent flags. A validated context carries the closed provider identity plus finite non-negative elapsed/usage/cost fields; the shared artifact writer consumes the union exhaustively.

**Workspace isolation** — every arm of a case runs in a fresh isolated workspace built by one shared builder: `with_skill` gets the (real or ablated) skill tree mounted, `without_skill` gets no skill files at all, so the baseline cannot read the skill from disk. Credential-bearing runner homes (`CODEX_HOME`, `VIBE_HOME`) live outside the model's working directory. This is the CF.2 invariant: baseline isolation is enforced by construction and covered by a cross-runner test, because a baseline that can see the skill silently destroys lift.

**Judge backend** — how a deferred qualitative assertion gets its verdict. `--judge-cmd` is the universal escape hatch (any shell command: prompt on stdin, JSON verdict on stdout); `--judge-backend claude|codex|vibe` selects a native adapter (with `--judge-model` picking the model; the native paths capture the judge's real dollar cost). Every verdict records its `judge_model`, and judge spend is its own ledger line in `cost_summary`, never folded into the model under test.

**Judge task** — one deferred qualitative check on one run, keyed by `judge_task_id` (`case::variant::run-n::assertion`, with a model segment when the model axis is fanned). `grade --judge-tasks` can emit pending tasks to `judge-tasks.jsonl`; the `judge` command reconstructs the same tasks from the manifest and runs. Verdicts merge back by the same key, which is also how human labels pair with verdicts in `judge-alignment`.

**Judge verdict kind** — the strict semantic shape of a stored verdict: boolean, scored, dimension-scored, dynamic-rubric, or consensus. Pass is derived from that shape (for example `score >= threshold`); duplicate IDs, string truthiness, non-finite values, and contradictory pass/score/threshold fields are rejected at import.

**Judge repetition / panel** — the two merges that stabilize a judge's verdict. `--judge-runs N` repeats each task and majority-merges `passed` (median for scores), killing within-judge noise; `--judge-panel` (repeated) runs several judge models and folds them into one consensus verdict with an `agreement` block, an optional `--quorum`, and even ties reported as `unresolved` rather than silently resolved.

**Judge alignment** — a judge's accuracy against human labels as ground truth: `judge-alignment` reports raw agreement, Cohen's kappa (chance-corrected, so an imbalanced label set cannot flatter the judge), precision/recall/F1, and the confusion matrix, and warns below `--min-labels` matched labels. Distinct from judge-sensitivity (below): two judges can agree and both be wrong.

**Judge robustness** — a judge's stability under probes it must not fail: `judge-robustness` re-judges with the rubric order flipped (`order_flip_consistency`) and feeds negative controls — an empty output and a master-key prompt injection — that a sound judge must reject (`control_leak_rate`). Model-touching and opt-in; it never runs in the grade path. The calibration walkthrough over alignment, robustness, and sensitivity is [`can-i-trust-my-judge.md`](can-i-trust-my-judge.md).

**Jetty lifecycle** — one closed imported/executing state: queued, running, succeeded, failed, timed out, or protocol-invalid. Unknown aliases and conflicting stored discriminators are protocol-invalid. “Succeeded” is not semantic success until `output.md` exists; timeout remains distinct from provider failure. A Jetty dry run is planning, not an execution lifecycle.

## Report signals

These are flags a `benchmark` report raises so you read pass rates correctly.

**Lift** — the difference in objective pass rate between `with_skill` and `without_skill`. Lift, not a single arm's pass rate, is the evidence that a skill changed behavior. In causal-inference terms it is the skill's average treatment effect in a paired design; the per-slice lift in `build_slice_summary` is a conditional treatment effect.

**Discrimination** — an assertion's ability to separate the arms. An assertion with identical with/without pass rates discriminates nothing, whatever its individual pass rate.

**Saturated** — every `with_skill` run passes. That is not a skill failure, but it is weak evidence of lift, and it is a different measurement from skill quality. It is a ceiling effect: the case has stopped discriminating, which is a construct-validity warning that it no longer measures the skill.

**No-lift** — `with_skill` and `without_skill` pass at the same rate, so the case shows no skill effect. Distinct from a failed run.

**Negative delta** — `with_skill` passes at a *lower* rate than `without_skill`: the skill actively hurt the case, surfaced as `negative_delta_cases` in `build_paired_summary`. Distinct from a *negative eval*, which is a case designed to test a no-op; this is a negative treatment effect.

**Flaky** — repeated runs of the same case/variant disagree. Flakiness is why runs repeat and why one pass is not a result.

**Leakage** — an assertion value appears literally in the prompt, so a weak answer can pass by echoing the task. `validate` warns on this; `--strict-leakage` turns the warning into a failure once you have replaced the weak check. Leakage is an annotation artifact (Gururangan et al. 2018) in eval clothing — a surface cue that lets a model be right for the wrong reasons (McCoy et al. 2019) without exercising the skill.

**Trigger / no-trigger** — whether a skill should load for a given query. A trigger case asserts autonomous skill *discovery*, detected from copied temp skill paths in the trace, not from the final answer and not from a bare skill name. Trigger behavior depends on the discovery-layer frontmatter (`description`/`when_to_use`), so a **discovery-population** ablation is measured *on* trigger cases — through the autonomous-trigger runners, `skill-trigger-matrix --ablation` (any registered adapter) or the deeper Pi tool `run_pi_trigger_eval.py --ablation`, both of which observe autonomous loading — while **answer-population** ablations (instructions/resource/runtime/preprocess) skip trigger cases. The forced-load generic runners never measure discovery ablations.

**Missing output** — a case/variant that was never run. It is marked `missing_output` and excluded from no-lift and saturation comparisons, because "not measured" is not "measured and failed."

**Trajectory diff** — the benchmark report's paired event-stream comparison (`trajectory_diff`): per case, over validated experimental pairs, commands exclusive to one arm across all paired repetitions, completed-event count deltas, and per-arm skill-load rates. It answers *how* the arms behaved, beside whether they passed — the diagnosis view for no-lift and qualitative-only cases. An arm without non-empty, readable trace evidence blocks its pair with a named reason; missing evidence never reads as an empty diff.

**Token overhead** — the static `SKILL.md` and reference footprint combined with the paired `with_skill - without_skill` token delta, reported as objective lift per 1k extra tokens. It answers whether the lift was worth the context the skill consumed.

**Cost** — real dollars a run spent, normalized into `cost_normalized` by every runner that reports it (`run-claude` and `run-subagent` capture provider cost; Pi smoke/trigger parse it from the stream; Jetty from the trajectory). The benchmark report carries a `cost_summary` ledger — operational totals over *all* runs (execution errors included: they were still paid for), per-variant mean/median/p90, paired cost deltas, ablation marginal cost and cost per confirmed regression, and judge spend as its own line. The standalone `cost-summary` command writes the suite ledger (JSON + markdown) with top spenders and spend-without-signal findings; `suite-run` projects spend before any model call and gates on `--max-estimated-cost-usd` / `--max-estimated-tokens`; `token-overhead` adds dollar deltas and lift-per-dollar. Cost sits next to lift, never mixed into it.

**Base-saturated** — a case whose *measured* `with_skill` and `without_skill` combined pass rates are equal: the base model does it with or without the skill, so the case measures nothing. Surfaced as a blocker in the readiness block of `audit-manifest --runs`. (Contrast **leak-saturated**, which is a static property of the prompt.)

**Qualitative-only** — a case whose objective pass rates are flat across arms but whose *combined* (judge-inclusive) score lifts with the skill: the skill's value is qualitative, and an objective-only reading would call it useless. Surfaced in the readiness block of `audit-manifest --runs`. **Objective-only** is the static cousin: a behaviour case with no judge assertion, so it can only ever measure objective compliance.

**Readiness** — `audit-manifest`'s verdict on whether a suite is worth paying to run, collapsed into an explicit `blockers` list (instruction-simulated ablations, leak-saturated cases, no adversarial coverage; with `--runs`, base-saturated cases). `--fail-on-blockers` turns the verdict into a CI gate. Readiness is about the *eval's* trustworthiness, not the skill's quality — a ready manifest can still measure a bad skill.

**Reliability (pass@k / pass^k)** — unbiased estimates from repeated runs of "at least one of k runs passes" (pass@k) and "all k runs pass" (pass^k), per (case, variant) with a pooled per-variant headline, plus `paired_lift`: the with−without delta on each, sign-flip tested. pass@k reads as best-case capability, pass^k as dependability; a skill can raise one and not the other.

**Contamination** — output-side evidence that a case was answered from memory rather than worked: the `contamination` command checks verbatim n-gram containment between output and answer key (`ngram_containment`), a per-case `canary` GUID tripwire that must never appear in an output, and a `released_at` vs `--model-cutoff` gate for cases older than the model's training data. Model-free; `--fail-on-contamination` gates CI.

## Populations and evidence

**Population** — which axis a measurement is on. **Answer** population: given a task, does the output meet the assertions (a paired `with_skill` vs `without_skill` comparison; the benchmark report stamps `population: "answer"`). **Trigger / discovery** population: does the skill *load on its own* for a prompt (a single arm, measured by the autonomous-trigger runners `skill-trigger-matrix` and `run_pi_trigger_eval.py`). The two are graded differently (a NO_TRIGGER case *passes by the skill not firing*), so their pass-rates are not comparable — the benchmark report excludes trigger cases and lists them under `skipped_trigger_cases`.

**Trigger comparison** — `skill-benchmark trigger-compare`, the trigger population's paired causal gate: a baseline trigger report against an `--ablation` report of the same canonical revision. Each report declares its expected agent/model/query cells, every persisted observation carries `(query_id, run_number)`, and self-digested manifest/protocol blocks bind the treatment declaration and behavior-affecting runner configuration. Each row repeats the protocol digest and observed isolation state. Missing, duplicate, mismatched, incomplete, protocol-drifted, or identity-invalid evidence blocks confirmation; legacy reports without these fields must be regenerated. Complete agent/model cells are reported but collapsed to one delta per stable authored-query ID and polarity before the sign-flip test, so agents and models are repeated measurements rather than independent inference units. `causal_confirmation` requires matching ablation IDs, verified provenance, complete coverage, a negative aggregate mean, and significance to produce `confirmed_causal`; a significant change in the improving direction is not a regression. The comparison upgrades trigger evidence from a single-arm `raw_measurement` to `confirmed_causal` / `refuted` / `indeterminate`.

**Evidence class** — how much a number is worth. `EvidenceClass` has five members: `confirmed_causal` (a provenance-gated paired ablation comparison — `causal_confirmation` is the only door to it), `refuted`, `raw_measurement` (a single-arm measurement, no pairing), `indeterminate` (measured, but provenance, coverage, execution validity, or statistical significance is insufficient — not confirmed and not refuted), and `unmeasured` (no scorable runs). The trigger report spells its report-level label `raw_autonomous_trigger_measurement` — read it as the trigger-path spelling of `raw_measurement` (its per-result `measurement` field uses the enum value directly).

**Judge-sensitivity** — whether a skill's measured lift depends on *which* model judged it. `compare-judges` flags `sign_sensitive` (judges disagree the skill even helps) and `magnitude_sensitive` (the with−without lift spread across judges exceeds a threshold). Every verdict records its `judge_model`, so a single judge number is never mistaken for a judge-independent one.

## See also

- [`evals-are-not-tests.md`](evals-are-not-tests.md) — why these terms exist and why a test-suite vocabulary does not cover them.
- [`../README.md`](../README.md) — manifest format, assertion reference, and the command index.
- [`commands.md`](commands.md) — per-command contracts: flags, examples, and output shapes.
- [`../LESSONS_LEARNED.md`](../LESSONS_LEARNED.md) — the iteration history that produced several of these terms.
- [`academic-grounding.md`](academic-grounding.md) — the research constructs behind these terms, with citations.
