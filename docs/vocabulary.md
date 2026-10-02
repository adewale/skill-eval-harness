# Vocabulary

This page is the canonical glossary: each term is defined here once, with the place it shows up in a manifest, a command, or a report. The other concept docs apply a lens to these terms rather than redefine them — [`abstractions.md`](abstractions.md) the engineering shape, [`academic-grounding.md`](academic-grounding.md) the research construct, [`evals-are-not-tests.md`](evals-are-not-tests.md) how to read the number — so when a definition changes, it changes here and the lenses follow. (The README still defines a term inline where you first meet it in a workflow; that is reference-at-use, not a second home for the definition.)

Terms are grouped by what they describe: the units you evaluate, the comparison structure, the things you assert, the artifacts a run produces, the runners and judges that produce and grade them, the signals a report flags, and the eval's own health.

## Units of evaluation

**Manifest** — the `evals/shared-benchmark.json` file a skill repo owns. It names the skill, declares variants and splits, and lists cases, assertions, and ablations. `validate` checks its shape; every other command reads it.

**Case** — one scenario under test, identified by `id`. A case carries a `prompt`, optional fixture `files`, a `split`, taxonomy fields (`domain`, `difficulty`, `success_goals`, `trigger_type`), `expected_behavior`, and `assertions`, and optionally a **case source** and a **known answer** (below).

**Case source** — where a case came from, declared as the optional `source`: `production`, `bug-report`, `hand-written`, `synthesized`, or `imported`. `audit-manifest` counts the sources in `case_sources` and raises `case-source-unrecorded` when any case records none, or `synthesized-cases-only` when every case is synthesized.

**Known answer** — an answer the case's own deterministic checks should pass, declared inline as `reference_answer` (tune cases only) or as `reference_answer_ref`, a manifest-relative file that `holdout` and `holdback` cases must use, because a known answer is an answer key. The two fields are mutually exclusive and not allowed on trigger cases. Only `audit-manifest`'s known-answer check reads it: the reference answer must pass every gate text check, and the **null answer** (the prompt echoed back) must fail at least one. Prepared tasks never carry a known answer, even with `--include-answer-key`.

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

**Experimental pair** — exactly one eligible treatment arm and one eligible control arm sharing `(case_id, model, run_number, population)`, under a declared **contrast** (`experimental_pairs.ContrastSpec`). Skill presence pairs `with_skill` against `without_skill`; an ablation pairs `with_skill` against `ablation:<id>` under the contrast id `ablation:<id>`; the edit contrast (`skill_edit`) pairs `with_skill` against `old_skill` for `benchmark`'s `paired_edit_summary`. The harness constructs this value before lift, paired reliability, paired cost, token-overhead, or ablation-confirmation arithmetic. A missing or ineligible arm is a blocked pair, named after the arm (`missing_without_skill`, `missing_ablation:<id>`, `unscorable_arm`); a duplicate arm is invalid rather than “last row wins.” Each contrast also names the factors held fixed between its arms, today effort, so arms that ran at different effort block as `effort_mismatch`. Pairing diagnostics and blocked pairs carry the `contrast_id`. Telemetry comparisons further require compatible provenance/unit/billing basis before a numeric delta exists.

**Split** — when a case is allowed to be seen.

| Split | Visible | Used for |
|---|---|---|
| `tune` | While editing the skill and evals | Iteration |
| `holdout` | At end-of-round or merge | Scoring a version you have already chosen |
| `holdback` | Only after scoring | Detecting memorization |

`holdout` and `holdback` prefer a private `prompt_ref` over an inline `prompt` so the answer is not exposed early. `prepare` fails on missing hidden prompts unless `--allow-missing-prompts` is passed for dry-run planning. Holding cases back is the harness's contamination control: a case kept out of the skill, the docs, and the eval text cannot have been memorized, so a high score on it is evidence rather than leakage.

A `holdout` score is an unbiased estimate only while it does not choose between versions. Once you score `holdout` every round and keep the best-scoring revision, `holdout` has become a selection set and its winning score overstates the win; the next unseen split, `holdback`, then carries the honest number. [`comparing-with-claude-api-evals.md`](comparing-with-claude-api-evals.md#the-test-set) maps this onto the hillclimb guide's train/validation/test split.

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

**Severity** — how a failed assertion counts, declared per assertion (or defaulted by type): `critical` (an absorbing barrier: one failure vetoes the run, so every pass rate the run carries is set to 0.0 (a rate with no check behind it, such as the objective rate of a case gated only by judges, stays null) and its graded score is withheld; those zeros still count in every pass-rate mean, and the case gains a `critical-failure` flag), `gate` (carries the pass rate; the default for objective checks), or `soft` (feeds only the per-run graded score and never moves a pass rate; the default for `judge`/`similarity`). `--strict` promotes soft to gate.

**Graded score** — the "how much better" channel beside binary pass/fail. Objective results carry a 0–1 `score`, and a judge verdict carries one when the judge returns it (anchored dimensions and dynamic rubrics always do; a plain yes/no verdict may not); soft results with a score feed a per-run `graded_score`, and `soft_passed` / `soft_total` count every soft verdict. `build_paired_summary` reports a paired `graded` channel, with a sign-flip permutation significance test and an interval, beside the raw lift. The channel takes scores in 0–1, as `atLeast` does: anchored dimensions are normalized from their 1–5 scale as `(score - 1) / 4`, and a plain judge that scores on its own scale declares it as **`score_scale: [low, high]`** (two finite numbers, low < high; `validate` refuses anything else, and refuses it beside `atLeast`, `graded_dimensions`, `dynamic_rubric` or `per_step`). Such a judge must return a `score` in that range, or the verdict is a parse error and stays unavailable; the harness then passes it when the raw score reaches the assertion's `threshold` (on the same scale, the top of the scale by default) and records `(score - low) / (high - low)` as its `score`, with `raw_score` beside it. A plain judge with no `score_scale` keeps its score as returned, so a 1–5 answer leaves the channel `partial` (`invalid_graded_score`). An optional `reference_score` / `reference_graded_score` on a case sets a no-regression floor.

**Variant-scoped assertion** — an assertion restricted to specific arms via `variants` / `only_variants` / `except_variants`. Process checks need this: `skill_invoked=true` belongs to `with_skill`, and `skill_invoked=false` belongs to `without_skill`, so an unscoped skill-load requirement would wrongly penalize the baseline.

**Assertion dependency (`depends_on`)** — an assertion may name prerequisite assertions; when a prerequisite fails or is itself skipped, the dependent is SKIPPED — out of every denominator and out of the critical veto — rather than counted as a failure. Skip is not zero: a dependent that never ran is "not measured", so an upstream miss cannot double-count as two failures. Cycles and unknown targets are rejected at validation.

**Eval intent** — what a case exists to show, declared per case as `eval_intent`: `capability` (the default — the case measures lift and participates in saturation/no-lift/staleness signals) or `regression` (the case pins behavior the skill must not lose; it reports under `regression_guards_holding`, is exempt from staleness and suggestion pruning, and its saturation is the goal, not a warning).

## Run artifacts

**`output.md`** — the final answer a run produced. Objective and qualitative assertions read it.

**`metadata.json`** — optional per-run telemetry: elapsed time, token counts, model name, and the normalized cost blocks below.

**Stop class** — how a run's answer ended, recorded as `stop_class` beside the provider's raw `stop_reason`: `completed`, `truncated` (cut off at an output limit), `turn_limit` (stopped by the eval's turn budget), `refused`, `other`, or `unavailable` (the backend exposes no stop signal; only Claude runs observe one today). A `truncated` or `turn_limit` run is unscorable and blocks its pair; a refusal is graded and counted. The benchmark report's `run_endings` block counts each class per variant.

**Served model check** — `served_model_check` compares `requested_model` with the model(s) the provider reported. One rule covers every backend (`completion_contracts.ServedModel`): with exactly one reported model, that model is credited as `served_model` and checked; with several, none is credited (`served_model` is null, `served_models` lists them). The values are `match`; `mismatch` (the credited model is another model, or none of several is the requested one; the run is unscorable); `mixed` (several models answered and one of them matches the request or cannot be verified against it; scored, but no single model can be credited); `unverifiable` (an alias the harness cannot resolve; still scored); `unavailable` (no served model was reported); and `not_requested` (the run asked for no model). The comparison first reduces both ids to the bare model id: it drops a provider prefix (`anthropic/`, Bedrock's `us.anthropic.`), a context-window suffix (`sonnet[1m]`), Bedrock's `-v1:0` version suffix and a `-latest` alias, and reads Vertex's `@20250805` as a dated snapshot. Two Claude ids then match when family and version agree (`claude-sonnet-4-0` is version 4) and the request names no snapshot or the served one; a bare family alias such as `sonnet` matches any id in that family; other ids match themselves or themselves plus a dated snapshot suffix. `claude-sonnet-5` served as `claude-sonnet-5-5`, and `haiku` served as a Sonnet id, are a `mismatch`; an id the harness cannot parse, such as a Bedrock inference-profile ARN, is `unverifiable`. Claude subagent turns are not counted, because a subagent may run on another model by design.

**Effort (per run)** — `effort: {requested, applied_by}` records the reasoning effort a run used. Without `--effort` on `run-claude`, `run-codex`, or `run-agent` it reads `requested: null`, `applied_by: "backend_default"`, and backend defaults differ by model and CLI version. A with/without pair whose arms ran at different effort, or where only one arm recorded effort, is blocked rather than compared.

**Execution validity** — whether a run counts toward scoring, recorded as `execution_valid` on each result row. It is false for an infrastructure failure: a nonzero `returncode`, a timeout, an incomplete provider response or committed artifact set, invalid run metadata, or an `output.md` that starts with a runner failure marker (`[CLAUDE FAILURE`, `[CODEX FAILURE`, `[GEMINI FAILURE`, `[JETTY FAILURE`, `[VIBE FAILURE`, `[TIMEOUT`). It is also false when the completion evidence says the answer is not the requested model's finished answer, and then the row names the reason in `unscorable_reason`: `stopped:truncated`, `stopped:turn_limit`, or `served_model_mismatch`. A run that produced no output is marked `missing_output` instead (see **Missing output** below). A missing or invalid run is left out of every pass rate and blocks its experimental pair (`unscorable_arm`), yet still counts in the cost ledger because it was paid for. A refusal and a `mixed` served-model run stay valid and graded.

**`usage_normalized` / `cost_normalized`** — legacy-compatible normalized token and dollar blocks every runner writes alongside raw provider fields. Schema-v3 `telemetry` is the canonical contract: it separates provenance (where an available number came from) from availability (`available`, `unavailable`, or `not_applicable`). A measured zero is available; unavailable telemetry is never numeric zero. The provenance values and the separate usage-source and cost-source sets are listed once, in [`commands.md`](commands.md#cost-telemetry-tokens-and-dollars); the contract is [`telemetry-availability-and-comparability-spec.md`](telemetry-availability-and-comparability-spec.md).

**Availability** — how the harness says whether it observed something (`observation_contracts.Availability`): `complete`, `partial`, `unavailable`, or `not_applicable`. Only `complete` counts as observed evidence. New fields write these values; `Availability.parse` still reads the spellings earlier artifacts persisted (`incomplete` as `partial`; `unknown`, `unobserved` and `missing` as `unavailable`; `not-applicable` as `not_applicable`), and a test keeps production code from writing a retired spelling again.

**Trace-event lifecycle** — the closed state of one normalized event: completed, in-progress, failed, or unknown, with a recorded source (provider status, intrinsically terminal/start event kind, explicit legacy adaptation, or unknown). Missing or misspelled status is not completion. Only completed operations contribute tool/command/file metrics.

**Trace artifacts** — what a trace-aware runner writes so process and efficiency assertions have evidence:

- `trace.jsonl` — the raw runner event stream, preserved before normalization.
- `events.json` — normalized events that process assertions read.
- `metrics.json` — tokens, command counts, tool calls, elapsed time, retries.
- `environment.json` — runner, model, and sandbox details where available.

The normalized shapes are an adapter boundary: Pi, Codex, Gemini, and Jetty emit different raw events, so each shape gets fixture tests rather than an assumed common schema.

## Runners and judges

**Agent backend** — one row in `agent_capabilities.BACKENDS`, declaring the provider's capability gates, explicit answer route and executable command entrypoints, native answer/autonomous-trigger/judge bindings, workspace, trace, smoke, failure, and CLI-option policy. `AGENT_BACKENDS`, `JUDGE_BACKENDS`, `run_trigger_matrix.ADAPTERS`, `WORKSPACE_BUILDERS`, `AGENT_CAPABILITIES`, and `SMOKE_TARGETS` remain compatibility projections rather than independent registration points. Implementation dispatch and workspace entries may be replaced temporarily; policy projections are immutable, and a new provider requires a complete row. Native answer runners are dispatched by `run-agent --agent <name>`; `run-codex` and `run-claude` are compatibility wrappers over the same path. `skill-benchmark agent-capabilities` renders the machine-readable view, while [`agent-parity.md`](agent-parity.md) is the reader-facing table.

**Answer-runner outcome** — one frozen execution variant: completed, timed out, spawn failed, or provider failed. Return code, timeout, answer, and failure are not independent flags. A validated context carries the closed provider identity plus finite non-negative elapsed/usage/cost fields; the shared artifact writer consumes the union exhaustively.

**Workspace isolation** — every arm of a case runs in a fresh isolated workspace built by one shared builder: `with_skill` gets the (real or ablated) skill tree mounted, `without_skill` gets no skill files at all, so the baseline cannot read the skill from disk. Credential-bearing runner homes (`CODEX_HOME`, `GEMINI_CLI_HOME`, `VIBE_HOME`) live outside the model's working directory. This is the CF.2 invariant: baseline isolation is enforced by construction and covered by a cross-runner test, because a baseline that can see the skill silently destroys lift.

**Judge backend** — how a deferred qualitative assertion gets its verdict. `--judge-cmd` is the universal escape hatch (any shell command: prompt on stdin, JSON verdict on stdout); `--judge-backend claude|codex|gemini|vibe` selects a native adapter (with `--judge-model` picking the model; usage/cost is captured when that provider reports it). Every verdict records its `judge_model`, and judge spend is its own ledger line in `cost_summary`, never folded into the model under test.

**Judge task** — one deferred qualitative check on one run, keyed by `judge_task_id` (`case::variant::run-n::assertion`, with a model segment when the model axis is fanned). The id renders the run's `manifest_contracts.RunCoordinate` (case, variant, run number, optional model), the one key that result rows, judge tasks, human judgements and experimental pairs share. `grade --judge-tasks` can emit pending tasks to `judge-tasks.jsonl`; the `judge` command reconstructs the same tasks from the manifest and runs. Verdicts merge back by the same key, which is also how human labels pair with verdicts in `judge-alignment`.

**Judge verdict kind** — the strict semantic shape of a stored verdict: boolean, scored, dimension-scored, dynamic-rubric, or consensus. Pass is derived from that shape (for example `score >= threshold`); duplicate IDs, string truthiness, non-finite values, and contradictory pass/score/threshold fields are rejected at import.

**Judge repetition / panel** — the two merges that stabilize a judge's verdict: `--judge-runs N` repeats each task with one judge, and `--judge-panel` (repeated) asks several judge models. Both fold the member verdicts with one rule, `judge_verdict.resolve_consensus`: a strict majority passes (median for scores), `--quorum` sets a k-of-n bar for a panel instead, and an exact tie passes only if the median score clears the verdict's explicit threshold; otherwise the tie is `unresolved` and does not pass. Both report an `agreement` block (`concur`, `n`, `concur_fraction`, `unanimous`, `unresolved`), so a judge that disagrees with itself on identical input is visible per task rather than averaged away.

**Judge alignment** — a judge's accuracy against human labels as ground truth: `judge-alignment` reports raw agreement, Cohen's kappa (chance-corrected, so an imbalanced label set cannot flatter the judge), precision/recall/F1, and the confusion matrix, and warns below `--min-labels` matched labels. Distinct from judge-sensitivity (below): two judges can agree and both be wrong.

**Feedback (human judgement store)** — `feedback.json`, the one place a person's judgement of a run is stored. `render-viewer --serve` writes each entry (`case_id`, `variant`, `run_number`, an optional judge `assertion` name, a `pass`/`fail`/`unsure` verdict, a note), and a later entry for the same run and assertion replaces the earlier one. The run is a `RunCoordinate`, so an entry whose `variant` is not a real arm (`with_skill`, `without_skill`, `old_skill`, or `ablation:<id>`) is rejected. `judge-alignment --labels feedback.json` reads a pass/fail verdict on a named assertion as that judge task's label, and `error-analysis --feedback` attaches run-level notes to its review queue.

**Judge robustness** — a judge's stability under probes it must not fail: `judge-robustness` re-judges with the rubric order flipped (`order_flip_consistency`; one verdict per order, so it cannot separate position bias from a judge that varies at random) and feeds negative controls — an empty output and a master-key prompt injection — that a sound judge must reject (`control_leak_rate`). Model-touching and opt-in; it never runs in the grade path. The calibration walkthrough over alignment, robustness, and sensitivity is [`can-i-trust-my-judge.md`](can-i-trust-my-judge.md).

**Jetty lifecycle** — one closed imported/executing state: queued, running, succeeded, failed, timed out, or protocol-invalid. Unknown aliases and conflicting stored discriminators are protocol-invalid. “Succeeded” is not semantic success until `output.md` exists; timeout remains distinct from provider failure. A Jetty dry run is planning, not an execution lifecycle.

## Report signals

These are flags a `benchmark` report raises so you read pass rates correctly.

**Lift** — the difference in objective pass rate between `with_skill` and `without_skill`. Lift, not a single arm's pass rate, is the evidence that a skill changed behavior. In causal-inference terms it is the skill's average treatment effect in a paired design; the per-slice lift in `build_slice_summary` is a conditional treatment effect.

**Discrimination** — an assertion's ability to separate the arms. An assertion with identical with/without pass rates discriminates nothing, whatever its individual pass rate.

A case can stop discriminating at either extreme. The four entries below are different conditions: the first is a target while you iterate, and the other three are warnings about the case.

**With-skill ceiling** — every `with_skill` run of a case passes. It is a fair target while you iterate on the skill, but it says nothing about lift on its own, because the baseline may pass too. No flag marks it; read it beside the `without_skill` rate.

**Saturated** — the benchmark flag `saturated/non-discriminating`: both arms average an objective pass rate of 1.0 over the case's scored pairs (a combined score of 1.0 for a case gated only by judges, see **Case flag signal**) (`effect_estimates.ceiling_or_floor` returns the ceiling), so the base model already does the task and the case cannot show lift. It is a construct-validity warning that the case no longer measures the skill, not a skill failure. `audit-manifest --runs` reports it as `saturated-eval` unless the case is a regression guard, whose saturation is the goal.

**Base-saturated** — readiness's measured version, over the *combined* (judge-inclusive) score: a case whose `with_skill` and `without_skill` combined pass rates, averaged over validated pairs, are equal and above zero, at any level rather than only at 1.0. The base model does as well with or without the skill, so the case measures nothing. `audit-manifest --runs` lists it under `base_saturated_cases` with a `base-saturated-case` blocker; a regression-intent case goes to `regression_guards_holding` instead. Contrast **leak-saturated**, a static property of the prompt (every positive assertion value already appears in it).

**Floor** — a case both arms fail. The benchmark flags `floor: fails in both arms` when both arms average a combined score of 0 over the scored pairs (objective and gate-judge checks together, the score readiness reads), and `audit-manifest --runs` reports it as `floor-eval`; readiness lists a case whose combined score is 0 in both arms under `floor_cases`, with a `floor-eval` blocker that covers regression guards too. The likelier cause is a broken case or assertion rather than a hard task, so audit the case before making it harder; `suggest-cases` never seeds a harder variant from it.

**No-lift** — the benchmark flag `no objective lift`: `with_skill` passes no more often than `without_skill` over the scored pairs, so the case shows no skill effect (a **negative delta** carries the flag too). Distinct from a failed run.

**Case flag signal** — the rate a `case_flags` entry's flags and its `with_skill` / `without_skill` values read: `objective` (the objective pass rate) for a case with an objective check, `combined` for a case gated only by judges, which has no objective rate. A combined entry reads the score readiness reads, so its `floor`, ceiling (`saturated/non-discriminating`), `no objective lift`, `with-skill failure` and `flaky repeated pass rates` flags agree with readiness's `floor_cases` and `base_saturated_cases`; `critical-failure` and `below-reference-floor` read the runs' vetoes and graded scores either way.

**Negative delta** — `with_skill` passes at a *lower* rate than `without_skill`: the skill actively hurt the case, surfaced as `negative_delta_cases` in `build_paired_summary`. Distinct from a *negative eval*, which is a case designed to test a no-op; this is a negative treatment effect.

**Flaky** — repeated runs of the same case/variant disagree. Flakiness is why runs repeat and why one pass is not a result. Before reading it as model variance, rule out the eval: an ambiguous case, a judge that flips its verdict on identical output, or state an earlier run left in a reused workspace. `audit-manifest --runs` reports each flaky case as a `required` `flaky-eval` finding.

**Leakage** — an assertion value appears literally in the prompt, so a weak answer can pass by echoing the task. `validate` warns on this; `--strict-leakage` turns the warning into a failure once you have replaced the weak check. Leakage is an annotation artifact (Gururangan et al. 2018) in eval clothing — a surface cue that lets a model be right for the wrong reasons (McCoy et al. 2019) without exercising the skill.

**Trigger / no-trigger** — whether a skill should load for a given query. A trigger case asserts autonomous skill *discovery*, detected from copied temp skill paths in the trace, not from the final answer and not from a bare skill name. Trigger behavior depends on the discovery-layer frontmatter (`description`/`when_to_use`), so a **discovery-population** ablation is measured *on* trigger cases — through the autonomous-trigger runners, `skill-trigger-matrix --ablation` (any registered adapter) or the deeper Pi tool `run_pi_trigger_eval.py --ablation`, both of which observe autonomous loading — while **answer-population** ablations (instructions/resource/runtime/preprocess) skip trigger cases. The forced-load generic runners never measure discovery ablations.

**Missing output** — a case/variant that was never run. It is marked `missing_output` and excluded from no-lift and saturation comparisons, because "not measured" is not "measured and failed." A run that did produce output but cannot be scored is the separate **Execution validity** case above.

**Trajectory diff** — the benchmark report's paired event-stream comparison (`trajectory_diff`): per case, over validated experimental pairs, commands exclusive to one arm across all paired repetitions, completed-event count deltas, and per-arm skill-load rates. It answers *how* the arms behaved, beside whether they passed — the diagnosis view for no-lift and qualitative-only cases. An arm without non-empty, readable trace evidence blocks its pair with a named reason; missing evidence never reads as an empty diff.

**Token overhead** — the static `SKILL.md` and reference footprint combined with the paired `with_skill - without_skill` token delta, reported as objective lift per 1k extra tokens. It answers whether the lift was worth the context the skill consumed.

**Cost** — real dollars a run spent, normalized into `cost_normalized` by every runner that reports it (`run-claude` and `run-subagent` capture provider cost; Pi smoke/trigger parse it from the stream; Jetty from the trajectory). The benchmark report carries a `cost_summary` ledger — operational totals over *all* runs (execution errors included: they were still paid for), per-variant mean/median/p90, paired cost deltas, ablation marginal cost and cost per confirmed regression, and judge spend as its own line. The standalone `cost-summary` command writes the suite ledger (JSON + markdown) with top spenders and spend-without-signal findings; `suite-run` projects spend before any model call and gates on `--max-estimated-cost-usd` / `--max-estimated-tokens`; `token-overhead` adds dollar deltas and lift-per-dollar. Cost sits next to lift, never mixed into it.

**Qualitative-only** — a case whose objective pass rates are flat across arms but whose *combined* (judge-inclusive) score lifts with the skill: the skill's value is qualitative, and an objective-only reading would call it useless. Surfaced in the readiness block of `audit-manifest --runs`. **Objective-only** is the static cousin: a behaviour case with no judge assertion, so it can only ever measure objective compliance.

**Readiness** — `audit-manifest`'s verdict on whether a suite is worth paying to run. Its blockers are typed findings (`readiness.blocker_findings`, with their messages repeated in `blockers`), and exactly six kinds block: `ablation-instruction-simulated`, `leak-saturated-case` and `no-adversarial-cases` from the manifest, and with `--runs`, `base-saturated-case`, `floor-eval` and `benchmark-incomplete`. The last fires when the benchmark behind `--runs` is partial (for example, judge assertions graded without `--judge-results`): readiness cannot see the run-measured signals, so it blocks instead of reporting them clear. `--fail-on-blockers` turns the verdict into a CI gate. Readiness is about the *eval's* trustworthiness, not the skill's quality; a ready manifest can still measure a bad skill.

**Reliability (pass@k / pass^k)** — unbiased estimates from repeated runs of "at least one of k runs passes" (pass@k) and "all k runs pass" (pass^k), per (case, variant) with a pooled per-variant headline, plus `paired_lift`: the with−without delta on each, sign-flip tested. pass@k reads as best-case capability, pass^k as dependability; a skill can raise one and not the other.

**Inference unit** — what one delta in a paired sign-flip test is a delta of (`effect_estimates.InferenceUnit`). Benchmark lift counts `case` units (one delta per case and model, averaged over its repetitions), ablation confirmation counts `replicate_pair` units (matched repetitions within one case), and `trigger-compare` counts `query` units (one authored query and polarity, across agents and models). The `significance`, `interval` and `noise_check` blocks of a benchmark carry the unit as `unit`. The exact two-sided test cannot reach p ≤ 0.05 until at least 6 units move the same way (`2 / 2**6 = 0.03125`; five stop at 0.0625), and the notes that say so name the unit (`minimum_units_note`). More repetitions sharpen each case's rate but never add a case, so a case-level test short of units needs more cases, not more repeats.

**Interval** — the 95% range for lift on every paired block (`paired_summary.interval`, each `by_model` entry, and `paired_summary.graded.interval`), found by inverting the sign-flip test. The test and the interval read one set of sign patterns and one decision rule, so a bounded interval excludes zero exactly when the test rejects "no lift", on the exact path and the sampled one. The path is exact while the units that moved take at most 2**14 sign outcomes (14 distinct deltas, or many more when deltas repeat, as pass-rate deltas do; unchanged units never count), and seeded sampling beyond. The interval is `bounded: false`, with a `reason`, when five or fewer units leave no shift excludable at 95%, and when every delta is equal, because a sign-flip test cannot size a sample with no spread.

**Noise check** — `paired_summary.noise_check` asks whether the eval can resolve the lift you care about. It sets `cases_moved` against the 6 moved units p ≤ 0.05 needs (see **Inference unit**), and the `noise_floor` (the interval's half-width) against the `headroom` left above `without_skill` and against `benchmark --min-lift` when given; its `verdict` is `no-data`, `too-few-cases-moved`, `unbounded`, `noise-exceeds-headroom`, `noise-exceeds-min-lift`, or `resolvable`. When the lift is withheld (incomplete pairing, or a report that is partial because an answer run or a grading verdict is missing), both it and the interval move to `observed_noise_check` / `observed_interval`.

**Contamination** — output-side evidence that a case was answered from memory rather than worked: the `contamination` command checks verbatim n-gram containment between output and answer key (`ngram_containment`), a per-case `canary` GUID tripwire that must never appear in an output, and a `released_at` vs `--model-cutoff` gate for cases older than the model's training data. Model-free; `--fail-on-contamination` gates CI and fails on any answer run it had no output to check.

## Eval health

**Eval health** — `audit-manifest`'s rating of the eval itself, read before any lift: five marks, each `ok`, `concern` (a finding counts against it), or `unavailable` (no evidence was available, which is not the same as `ok`). By `id`:

1. `realistic-cases` — the cases are real requests, and the skill loads the way real use loads it.
2. `grader-correct` — the grader passes a known-good answer, fails a null answer, and agrees with people.
3. `baseline-headroom` — the `without_skill` arm has room to move, and no case fails in both arms.
4. `noise-below-min-lift` — the noise is smaller than the smallest lift worth acting on.
5. `arms-differ-only-in-skill` — effort and model are held fixed, the arms are paired within one run, and no answer leaks.

Marks 3–5 need `--runs`. Marks 3 and 4 also need a complete benchmark; mark 5 reads the run conditions (effort, served model) on any benchmark, so arms run under different conditions count against it even while the benchmark is partial. Why these five replace the hillclimbing post's four, and which finding kinds count against each, is [`comparing-with-claude-api-evals.md`](comparing-with-claude-api-evals.md#five-marks-of-a-lift-eval); the report shape is in [`commands.md`](commands.md#eval-health).

**Finding** — one typed record of something wrong with a skill, an eval, its grader, or a run, serialized as `{kind, severity, message, evidence?}`. Every `kind` is registered in `findings.FindingKind` with what it is about, a default severity (`required` or `recommended`), and the eval-health mark it counts against, if any; [`commands.md`](commands.md#finding-kinds) lists them. Whether a finding fails a command is decided by a gate policy (`gate_policy.GatePolicy`, the policy behind `--fail-on` and its presets), not by the finding.

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
