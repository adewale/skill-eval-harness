# Command reference

Every `skill-benchmark` subcommand, plus the `skill-pi-trigger-eval` and
`skill-trigger-matrix` entry points. The README carries the [core loop](../README.md#core-loop)
and a grouped [command index](../README.md#commands); this file is the per-command detail.

Contracts and safety rules that span commands live elsewhere: the manifest shape and
the run-output contract are in the [README](../README.md#manifest-format), the assertion
catalog is the [README's Assertions section](../README.md#assertions), and the reason
grading never calls a model is [`architecture.md`](architecture.md).

## Validate

```bash
skill-benchmark validate ../repo/evals/shared-benchmark.json
skill-benchmark validate ../repo/evals/shared-benchmark.json --strict-holdback
skill-benchmark validate ../repo/evals/shared-benchmark.json --strict-leakage
```

`validate` checks manifest shape, fixture paths, regex syntax, script oracle paths, and hidden-prompt refs. It also warns when a `contains*` assertion value appears literally in the prompt:

```text
WARN pos-ui-no-screenshot: assertion 'detect-ui-no-screenshot' value 'screenshot' appears in prompt (leakage; case may saturate)
```

That warning means a weak answer can pass by echoing the task. Use `--strict-leakage` only after you have replaced noisy keyword checks with scoped regexes, fixture-backed checks, `script` oracles, or judge assertions.

## Prepare tasks

```bash
skill-benchmark prepare ../repo/evals/shared-benchmark.json --split tune --out tasks.jsonl
skill-benchmark prepare ../repo/evals/shared-benchmark.json --runs-per-variant 5 --out tasks.jsonl
skill-benchmark prepare ../repo/evals/shared-benchmark.json --include-ablations --ablation-dir ablated-skills --out ablation-tasks.jsonl
```

`--include-ablations` requires `--ablation-dir DIR` whenever any ablation declares a removal (a `mechanism`/`components` + `target`): the altered skill tree is materialized there and the prepared rows point at it. (A manifest with only instruction-simulated ablations does not need it.)

Use `--include-answer-key` only for judge/debug tasks, never for generation runs.

## Import runner traces

Normalize a raw JSONL trace into `events.json` and `metrics.json` for process and efficiency assertions:

```bash
skill-benchmark import-trace \
  --source codex \
  --trace ../repo/eval-runs/latest/case/with_skill/run-1/trace.jsonl \
  --run-dir ../repo/eval-runs/latest/case/with_skill/run-1 \
  --write-metadata
```

## Run Codex JSONL tasks

`run-codex` is a compatibility wrapper for `run-agent --agent codex`. It executes prepared rows through a command compatible with `codex exec --json`, adds `--output-last-message <file>` for final-answer capture, saves JSONL as `trace.jsonl`, normalizes events/metrics, runs with isolated `CODEX_HOME` outside the model workdir, and records nonzero/timeouts as failed run artifacts:

```bash
skill-benchmark prepare ../repo/evals/shared-benchmark.json --split tune --out tasks.jsonl
skill-benchmark run-codex --tasks tasks.jsonl --runs ../repo/eval-runs/codex-tune
```

Override `--codex-cmd` for local wrappers or tests. It is an argv-style command prefix parsed with `shlex.split`; shell metacharacters, pipes, and inline env assignments are not interpreted. Put those in a wrapper script and pass the wrapper path instead.

```bash
skill-benchmark run-codex \
  --tasks tasks.jsonl \
  --runs ../repo/eval-runs/codex-trace \
  --codex-cmd ./bin/codex-jsonl-wrapper
```

## Run native agent tasks

`run-agent` is the provider-neutral native runner. It dispatches prepared rows through a registered backend and writes the same run contract as the compatibility wrappers:

```bash
skill-benchmark prepare ../repo/evals/shared-benchmark.json --split tune --out tasks.jsonl
skill-benchmark run-agent --agent codex --tasks tasks.jsonl --runs ../repo/eval-runs/codex-tune \
  --model openai/gpt-5.4-mini
skill-benchmark run-agent --agent claude --tasks tasks.jsonl --runs ../repo/eval-runs/claude-tune \
  --model claude-haiku-4-5-20251001
```

## Run Claude tasks (with cost capture)

`run-claude` is a compatibility wrapper for `run-agent --agent claude`: it executes prepared rows through `claude -p --output-format json`, extracts the answer into `output.md`, and records real per-run `total_cost_usd` + token usage into `metrics.json`. The benchmark report then totals `cost_usd_total` per arm (over scorable runs), so a paired eval reports actual dollars:

```bash
skill-benchmark prepare ../repo/evals/shared-benchmark.json --split tune --out tasks.jsonl
skill-benchmark run-claude --tasks tasks.jsonl --runs ../repo/eval-runs/claude-tune \
  --model claude-haiku-4-5-20251001
```

`--model` is optional (omit for the CLI default); `--claude-bin` overrides the executable (a stub in tests). A nonzero exit/timeout is written as a `[CLAUDE FAILURE …]` body, which `execution_valid` treats as a non-scorable infra failure, exactly like the Codex/Jetty runners.

## Run subagent tasks (in-process seam, tool replay, multi-turn)

`run-subagent` drives prepared rows through an in-process backend — the Claude CLI by default, any provider via `--agent-cmd` (prompt JSON on stdin, `{answer, trace?, usage?}` JSON on stdout), or a plain function in tests. It writes the same run-output contract (plus normalized `events.json`/`metrics.json` from a returned trace), reuses the isolated per-variant workspace (so the CF.2 baseline-isolation invariant covers it), honors row-level models, and drives multi-turn `turns` sequences into `turn-<n>/output.md`. Tool I/O can be recorded and replayed deterministically via `--tool-replay record|replay|strict|auto` (or `$SKILL_BENCHMARK_TOOL_REPLAY`), stored as `tool-replay.json` beside each run; `strict` fails closed on an unrecorded call.

```bash
skill-benchmark run-subagent --tasks tasks.jsonl --runs eval-runs/subagent --tool-replay record
```

## Compare judges (judge-sensitivity)

A single judge number is not reproducible across judge choice for a subtle skill. Judge the same runs with two models (`benchmark --judge-results` merges each), then `compare-judges` flags whether the measured lift depends on the judge:

```bash
skill-benchmark compare-judges \
  --report haiku=benchmark.haiku.json \
  --report sonnet=benchmark.sonnet.json \
  --out judge-panel.json
```

It reports each judge's `with_skill − without_skill` combined lift and sets `sign_sensitive` (judges disagree the skill helps), `magnitude_sensitive` (lift spread > `--magnitude-eps`, default 0.1), and `judge_sensitive` (either). Needs ≥2 `--report name=path`. Every verdict from `judge` carries its `judge_model`, so which model graded a run is always recoverable.

`compare-judges` asks *"does the result depend on which judge I picked?"* — not *"is the judge correct?"* For that, validate the judge against **human labels**:

## Validate a judge against human labels (judge-alignment)

Two judges can agree and both be wrong. `judge-alignment` scores a judge's verdicts against a human-labeled gold set (both keyed by `judge_task_id` with a `passed` bool), treating the human label as ground truth:

```bash
skill-benchmark judge-alignment \
  --labels human-labels.jsonl \
  --judge-results judge-results.jsonl \
  --out judge-alignment.json
```

It reports `agreement`, **Cohen's `cohen_kappa`** (chance-corrected, so an imbalanced label set can't flatter the judge) with a `kappa_interpretation` band, and `precision`/`recall`/`f1` plus the `confusion` matrix. Below `--min-labels` (default 50) matched labels it warns that the metrics are unstable. Fully model-free — it grades a judge you already ran.

The end-to-end calibration loop over this command, `compare-judges`, and `judge-robustness` — runnable offline on the demo — is [`can-i-trust-my-judge.md`](can-i-trust-my-judge.md).

## Error analysis (open coding → axial taxonomy)

`error-analysis` turns a `benchmark.json` into the "look at your data" surface: an open-coding **review queue** (one row per failing/errored run, anchored on its *first* upstream failure, with an open `note` slot) and an axial **failure taxonomy** (first-failures counted by category, so the few dominant buckets are visible), alongside the report's own case-flag histogram. Model-free.

```bash
skill-benchmark error-analysis --benchmark benchmark.json --out error-analysis.json
```

## Pi trace runners

The Adewale Pi smoke example writes the trace-aware run layout directly:

```bash
python3 examples/adewale-workspace/run_pi_smoke.py \
  --run-name trace-smoke \
  --selection /tmp/selection.json
```

The runner uses an isolated temporary workspace. `with_skill` receives copied skill files and fixtures. `without_skill` receives fixtures only and runs with `--no-skills`, so grep/find/read cannot discover the source repo's `skills/*/SKILL.md` or public eval manifests.

`skill-pi-trigger-eval` can also write per-query trace artifacts:

```bash
skill-pi-trigger-eval ../repo/evals/shared-benchmark.json \
  --eval-set trigger-queries.json \
  --out trigger-results.json \
  --trace-runs trigger-traces
```

## Grade

`grade` produces per-run grading rows and can emit pending judge tasks:

```bash
skill-benchmark grade ../repo/evals/shared-benchmark.json \
  --runs ../repo/eval-runs/latest \
  --out grade-report.json \
  --judge-tasks judge-tasks.jsonl
```

Write Anthropic-compatible `grading.json` files into each run directory:

```bash
skill-benchmark grade ../repo/evals/shared-benchmark.json \
  --runs ../repo/eval-runs/latest \
  --write-grading-files
```

## Benchmark

`benchmark` aggregates graded rows into variant summaries, paired deltas (with sign-flip `significance` and a `graded` channel), per-model grouping (`by_model`, `model_analysis` ranking and lift losers), slice summaries with lift concentration, oracle-strength shares, held-out vs tune-visible qualitative rates, and case flags. Add `--allow-scripts` only when you trust the repo-owned oracle commands in the manifest; `--strict` promotes soft assertions to gates; `--embed-cmd` enables embedding-mode similarity.

```bash
skill-benchmark benchmark ../repo/evals/shared-benchmark.json \
  --runs ../repo/eval-runs/latest \
  --allow-scripts \
  --judge-results judge-results.jsonl \
  --out benchmark.json
```

Multi-model runs prepare with `--models a,b,c` (run dirs gain a model segment: `<case>/<model>/<variant>`); grading discovers both layouts and pairs lift per (case, model).

## CI report formats

`report` serializes a `benchmark.json` for CI: `--format junit` writes one `<testcase>` per case/variant/run with evidence on failures and the paired lift as suite properties; `--format github` writes job-summary markdown plus `::warning` annotations per flagged case (and an `::error` on negative lift).

```bash
skill-benchmark report --benchmark benchmark.json --format junit --out junit.xml
skill-benchmark report --benchmark benchmark.json --format github --out "$GITHUB_STEP_SUMMARY"
```

The full CI gating recipe — both report formats plus the manifest-trust gate — is [`gating-ci-on-evals.md`](gating-ci-on-evals.md).

## Trend, staleness, and harder-case suggestions

`trend` keeps an append-only history of benchmark reports and emits the series, successive diffs, recurring failures ranked by prevalence x severity, and prune candidates (cases that never failed and never discriminated across the history — suggestions only, nothing is deleted). `suggest-cases` turns saturated/no-lift flags into harder-case candidate seeds; generation is opt-in behind `--generate-cmd` and never edits a manifest.

```bash
skill-benchmark trend --history eval-history --add benchmark.json --out trend.json
skill-benchmark suggest-cases --benchmark benchmark.json --manifest evals/shared-benchmark.json --out candidates.json
```

## Migrate a manifest

`migrate` upgrades a version-1 manifest to version 2: stamps default severities and oracle tiers, marks binary judge rubrics with a `graded?` todo, prints the diff plus the judgment-call checklist (`--check` for a dry run, `--out-checklist` to save it). See [`migrating-evals.md`](migrating-evals.md) for the agent runbook.

## Judge backends

Run deferred `judge`/`rubric` assertions with either a native backend (`--judge-backend claude`, `--judge-backend codex`, or `--judge-backend vibe`) or a shell command (`--judge-cmd`) that reads one grading prompt from stdin and emits JSON on stdout. The prompt contains the original case prompt, `expected_behavior`, `review_rubric`, the assertion, and the saved candidate output.

```bash
skill-benchmark judge ../repo/evals/shared-benchmark.json \
  --runs ../repo/eval-runs/latest \
  --judge-backend claude \
  --judge-model claude-haiku-4-5-20251001 \
  --transcripts judge-transcripts \
  --out judge-results.jsonl

skill-benchmark judge ../repo/evals/shared-benchmark.json \
  --runs ../repo/eval-runs/latest \
  --judge-backend codex \
  --judge-model openai/gpt-5.4-mini \
  --transcripts judge-transcripts-codex \
  --out judge-results.codex.jsonl

skill-benchmark judge ../repo/evals/shared-benchmark.json \
  --runs ../repo/eval-runs/latest \
  --judge-backend vibe \
  --judge-model mistral-large-latest \
  --transcripts judge-transcripts-vibe \
  --out judge-results.vibe.jsonl

skill-benchmark judge ../repo/evals/shared-benchmark.json \
  --runs ../repo/eval-runs/latest \
  --judge-cmd 'claude -p' \
  --transcripts judge-transcripts \
  --out judge-results.jsonl

skill-benchmark benchmark ../repo/evals/shared-benchmark.json \
  --runs ../repo/eval-runs/latest \
  --judge-results judge-results.jsonl \
  --out benchmark.json
```

Native Claude uses `claude -p --output-format json --no-session-persistence`; tool-free judges add `--tools ""`, and every native Claude judge passes the harness verdict schema through `--json-schema`. Native Codex uses isolated `CODEX_HOME` outside the model workdir plus `codex exec --output-last-message <file> --output-schema <schema.json>` so verdict parsing reads the final assistant message rather than the event JSONL stream. Native Vibe uses isolated `VIBE_HOME` outside the model workdir plus `vibe --prompt "$PROMPT" --output json` with tools disabled (`--enabled-tools re:^$`) and reads the final assistant message as the verdict JSON; `--judge-model` is passed through `VIBE_ACTIVE_MODEL`. Native judges run from an explicit working directory: a sanitized run-copy when tool exploration is enabled, otherwise a fresh empty temp directory so they cannot accidentally read the harness repo cwd. For Codex/OpenAI structured output, the harness adapts the canonical verdict schema into a strict provider schema (`additionalProperties:false`; optional fields become nullable) while still validating the returned verdict against the canonical schema. Vibe does not expose provider-enforced schema here, so harness-side schema validation is the gate. A shell judge command should return JSON like `{"passed": true, "score": 4, "rationale": "..."}`. Bare or fenced JSON is accepted using `json.raw_decode` scanning rather than brace counting. `--transcripts` saves the exact prompt, stdout, stderr, and parsed result for each judge task.

## Audit manifest quality

```bash
skill-benchmark audit-manifest ../repo/evals/shared-benchmark.json \
  --format markdown \
  --out eval-audit.md
```

Add `--runs ../repo/eval-runs/latest` to include saturated-case, no-lift, flaky repeated-run, and per-assertion discrimination analysis.

The audit reports:

- a **readiness** verdict — "is this eval worth paying to run?" — collapsing the things that decide whether a measured number will mean anything: ablations materialized vs instruction-simulated, **leak-saturated cases** (every positive objective assertion's value already appears in the prompt, so `with_skill == without_skill` by construction), adversarial coverage, and **objective-only cases** (a behaviour case with no judge assertion can only ever measure objective compliance — if the skill's value is voice/judgement it will read as zero lift). With `--runs`, it also surfaces the signals a static manifest can't see: **base-saturated cases** (measured `with_skill == without_skill` — a blocker, the case measures nothing) and **qualitative-only cases** (objective flat but the combined/judge score lifts — the skill's value is qualitative). All with an explicit `blockers` punch list;
- missing positive, negative, and adversarial eval coverage,
- missing holdout/holdback split coverage,
- missing trigger/no-trigger coverage,
- missing domain/difficulty/success-goal taxonomy for slice summaries,
- ablation-plan suggestions from major skill sections,
- the instruction-simulated ablations that should be materialized (and dangling/unknown ablation references),
- saturated and no-lift cases when run data is available,
- assertions with identical with/without pass rates, and
- recommended fixture repos/files.

**Gate it in CI.** `--fail-on-blockers` makes `audit-manifest` exit non-zero when the readiness block has any blockers, so a skill repo can keep its eval suite at "worth paying to run" the same way it keeps tests green:

```bash
skill-benchmark audit-manifest evals/shared-benchmark.json --fail-on-blockers
```

The systematic way to upgrade a suite is to drive those blockers to empty, repo by repo: materialize the ablations (`materialize-ablations` / declare a `mechanism`+`target`), de-leak the leak-saturated cases (move the answer out of the prompt, or assert a downstream consequence), and add adversarial cases where missing — then the gate goes green. The walkthrough is [`gating-ci-on-evals.md`](gating-ci-on-evals.md).

## Contamination perimeter (output-side, model-free)

```bash
skill-benchmark contamination ../repo/evals/shared-benchmark.json \
  --runs ../repo/eval-runs/latest \
  --model-cutoff 2025-01 \
  --fail-on-contamination \
  --out contamination.json
```

Three model-free checks over saved outputs: a canary tripwire (a case's declared canary string appearing verbatim in an output), output↔answer-key n-gram containment (`--ngram`, flagged above `--overlap-threshold`), and a `released_at`-vs-`--model-cutoff` gate for cases the model may have seen in training. `--fail-on-contamination` makes it a CI gate.

## Judge robustness probes

```bash
skill-benchmark judge-robustness ../repo/evals/shared-benchmark.json \
  --runs ../repo/eval-runs/latest \
  --judge-model claude-sonnet-4-6 \
  --fail-on-findings \
  --out judge-robustness.json
```

Probes a judge's stability before you trust its verdicts (model-touching; opt-in): order-flip self-consistency plus empty-output and master-key negative controls a robust judge must reject. Takes the same `--judge-cmd`/`--judge-model` backends as `judge`; `--fail-on-findings` makes it a CI gate.

## Cost telemetry (tokens and dollars)

Cost is a first-class eval signal. Every runner path — Pi smoke, Pi trigger, `run-codex`, `run-claude`, `run-subagent`, the judge wrapper, and the Jetty importer — writes legacy-compatible normalized blocks beside raw provider fields **and** an availability-aware telemetry v3 envelope into both `metadata.json` and `metrics.json`.

- `usage_normalized`: alias-normalized token counts (`input`/`prompt_tokens`/`totalTokens`/cache/reasoning variants) with a `source` — `provider_reported` (relayed from the provider), `trace_normalized` (summed from normalized trace events), `estimated`, `missing`, or `not_applicable`.
- `cost_normalized`: legacy-compatible dollar block with `currency`, per-part costs, and a source. Its v3 counterpart separates provenance (`provider_reported`, `trace_normalized`, `price_table_estimated`, or `legacy_unverified`) from availability (`available`, `unavailable`, or `not_applicable`). A measured `$0` is available; unknown cost is never zero.

Consumers of the blocks:

- `benchmark`/`aggregate` emit `cost_summary`: availability-aware coverage and operational totals (**every run counts here, including execution errors — a timed-out run still cost money — while quality rates keep excluding them**). A mixed set renders a partial known subtotal, not a false total. Per-variant stats, per-case spend, paired deltas, ablation marginal cost, and judge spend retain their basis/provenance.
- `cost-summary` writes the standalone suite ledger (`--out cost-summary.json`, `--md cost-summary.md`): coverage, totals, by variant/case/runner, top expensive cases and ablation arms, and `cost_quality_findings` when a `--benchmark` report is joined.
- `suite-run` projects spend **before any model call** from previous ledgers (`--cost-history <dir>`, per-run medians) or a static assumption (`--assumed-tokens-per-run`), and gates on `--max-estimated-tokens` / `--max-estimated-cost-usd` — failing closed when a dollar cap is set but no dollar estimate exists — unless `--allow-over-budget`.
- `audit-manifest --runs` adds cost-quality findings above `--expensive-case-usd` (default $1): `expensive-saturated-case`, `expensive-no-lift-case`, `high-cost-judge-only-case`, `ablation-high-spend-no-structured-regression`, and `high-footprint-low-lift-skill`.

Interpretation rule: `provider_reported` numbers are a direct provider envelope; `trace_normalized` reconstructs usage or cost from event streams; `unavailable` means the run carried no usable telemetry — fix the runner path rather than treating it as free. Lift-per-dollar is emitted only for scorable, basis-compatible paired costs with a strictly positive incremental cost; otherwise JSON/Markdown report a blocked reason. The complete contract and migration policy are in [`telemetry-availability-and-comparability-spec.md`](telemetry-availability-and-comparability-spec.md).

```bash
skill-benchmark cost-summary \
  --manifest ../repo/evals/shared-benchmark.json \
  --runs ../repo/eval-runs/latest \
  --benchmark benchmark.json \
  --out cost-summary.json \
  --md cost-summary.md
```

Upgrade existing run directories without guessing provenance:

```bash
skill-benchmark migrate-telemetry --runs ../repo/eval-runs/latest --check
skill-benchmark migrate-telemetry --runs ../repo/eval-runs/latest
```

The first command is byte-preserving; the second atomically adds schema v3 envelopes and any missing sibling artifact. Legacy numeric values are labelled `legacy_unverified` and do not qualify for causal lift-per-dollar comparisons.

## Profile skill size and references

```bash
skill-benchmark profile-skill ../repo/evals/shared-benchmark.json \
  --format markdown \
  --out skill-profile.md
```

`profile-skill` reports `SKILL.md` token estimates, reference-file counts/sizes, heading/module counts, and warnings for overly broad or oversized skills. These warnings are advisory; focused 2–3-module skills are often easier for agents to apply, but large skills can be justified when references are conditional.

## Token overhead

`token-overhead` combines static skill profile data with paired runtime traces. It reports the static `SKILL.md`/reference footprint, `with_skill - without_skill` token deltas, objective lift, objective lift per 1k extra total tokens — and, when cost telemetry exists, `with - without` dollar deltas, objective lift per dollar, and the total spend on saturated/no-lift pairs.

```bash
skill-benchmark token-overhead ../repo/evals/shared-benchmark.json \
  --runs-subdir eval-runs/latest \
  --format markdown \
  --out token-overhead.md

skill-benchmark token-overhead \
  ../skill-a/evals/shared-benchmark.json \
  ../skill-b/evals/shared-benchmark.json \
  --runs-subdir eval-runs/trace-smoke \
  --out token-overhead.json
```

If a repo has no paired trace metrics, the report still includes the static footprint and shows `0` runtime pairs. The decision loop that reads these numbers is [`is-my-skill-worth-its-tokens.md`](is-my-skill-worth-its-tokens.md).

## Suite preflight / allowlisted multi-skill tiers

Use `suite-run` before expensive model calls. It reads only an explicit suite file, rejects unrelated top-level manifests under the workspace root, verifies optional tree-hash pins, prints row estimates, and writes `RUN_SCOPE.json`.

```bash
skill-benchmark suite-run examples/adewale-workspace/all-manifests.txt \
  --workspace-root ../updating_all_of_my_skills \
  --pins examples/skill-pins.json \
  --tier preflight \
  --out-dir suite-runs/preflight

skill-benchmark suite-run examples/adewale-workspace/all-manifests.txt \
  --workspace-root ../updating_all_of_my_skills \
  --pins examples/skill-pins.json \
  --tier prepare \
  --include-ablations \
  --out-dir suite-runs/prepare
```

Non-model tiers are `preflight`, `static`, `prepare`, and `jetty-dry-run`. By default, a stray manifest such as `beautiful-mermaid/evals/shared-benchmark.json` fails the run instead of silently entering the matrix; pass `--allow-extra-manifests` only for exploratory audits.

## Aggregate many skills

```bash
skill-benchmark aggregate \
  $(cat examples/adewale-workspace/all-manifests.txt) \
  --runs-root .. \
  --runs-subdir eval-runs/latest \
  --out aggregate-benchmark.json
```

## Export Anthropic-compatible benchmark

```bash
skill-benchmark export-anthropic ../repo/evals/shared-benchmark.json \
  --runs ../repo/eval-runs/latest \
  --out benchmark.anthropic.json
```

## Blind comparison

```bash
skill-benchmark compare-tasks ../repo/evals/shared-benchmark.json \
  --runs ../repo/eval-runs/latest \
  --out compare-tasks.jsonl \
  --truth-out compare-truth.json

skill-benchmark compare-results \
  --truth compare-truth.json \
  --results compare-results.jsonl \
  --out compare-summary.json
```

## Materialize ablations

`materialize-ablations` writes real, altered skill trees for the manifest's declared removal ablations, plus a provenance record. The mechanism table and correctness gates are [`skill-ablation-spec.md`](skill-ablation-spec.md); the [README's Ablations section](../README.md#ablations) covers routing.

```bash
skill-benchmark materialize-ablations ../repo/evals/shared-benchmark.json \
  --out-dir ablated --out ablated/provenance.json
```

## Review viewer (static or served)

```bash
skill-benchmark render-viewer \
  --benchmark benchmark.json \
  --runs ../repo/eval-runs/latest \
  --out review.html
```

The viewer embeds run artifacts (images inline, typed links for pdf/xlsx, text in place). `--previous-workspace <dir>` embeds a diff against that iteration's `benchmark.json` (per-variant deltas, per-case deltas, new/resolved flags; pair with the `iteration-N/` directory convention). `--serve --port 8642` hosts the review with a feedback form persisting to `feedback.json` (entries keyed by case/variant/run).

## Trigger matrix (activation across agents and models)

```bash
skill-trigger-matrix ../repo/evals/shared-benchmark.json \
  --agent claude \
  --runs-per-query 3 \
  --out trigger-matrix.json
```

For each (agent, model) cell this mounts the skill where that agent discovers skills autonomously (never forcing the load), runs the manifest's `kind: "trigger"` cases the requested number of times, and reports per-cell trigger rates split by should-fire / should-not-fire polarity. The `claude` adapter spawns headless Claude Code subagents and defaults to haiku, sonnet, and opus; `--agent codex`, `--agent pi`, `--agent vibe`, and the offline `--agent stub` are included. Codex keeps credential-bearing `CODEX_HOME` outside the model workdir and exposes only `$CODEX_HOME/skills` as an extra read root. The Vibe adapter mounts skills under `.agents/skills`, keeps `VIBE_HOME` outside the model workdir, runs `vibe --prompt "$QUERY" --output streaming`, and detects native `skill` tool calls by skill name with path-evidence fallback. Additional agents register through an `AgentAdapter` subclass plus an `AGENT_CAPABILITIES` row. The tuning loop that consumes these rates is [`tuning-skill-activation.md`](tuning-skill-activation.md); manual live smoke tests wrap the same path (`RUN_TRIGGER_SMOKE=1` for Claude, `RUN_CODEX_TRIGGER_SMOKE=1` for Codex, `RUN_PI_TRIGGER_SMOKE=1` for Pi, `RUN_VIBE_TRIGGER_SMOKE=1` for Vibe). For a cheaper auth/network/process check that invokes every supported live adapter/model without asserting trigger behavior, run `RUN_AGENT_INVOKE_SMOKE=1 python3 -m unittest tests.test_trigger_matrix.AgentInvokeSmokeTests -v`.

## Pi trigger evals

```bash
skill-pi-trigger-eval ../repo/evals/shared-benchmark.json \
  --split tune \
  --runs-per-query 3 \
  --out trigger-report.json
```

This creates a temporary `PI_CODING_AGENT_DIR`, copies the skill under `skills/`, runs Pi without forced `--skill`, and detects whether the model loaded the skill from JSON stream events. It is the deeper Pi-specific tool: discovery-population ablation arms, per-query trace artifacts, and cost telemetry.

## Jetty adapter

Jetty support is optional, and its live response shapes still need token-backed smoke validation before a Jetty run counts as production evidence (see [`jetty-support-spec.md`](jetty-support-spec.md)). The harness exports runbook-mode chat-completion payloads, Jetty executes them, and `import-jetty-results` copies `output.md`, artifacts, and metadata back into the normal run layout. Provider status aliases parse into queued/running/succeeded/failed/timed-out/protocol-invalid states; unknown status and completed-without-`output.md` fail closed as protocol-invalid rather than ordinary model failures.

```bash
# Export runbook-mode Jetty chat-completion payloads. No network calls.
skill-benchmark export-jetty ../repo/evals/shared-benchmark.json \
  --split tune \
  --out jetty-payloads.jsonl

# Dry-run payload loading without a token.
skill-benchmark run-jetty \
  --payloads jetty-payloads.jsonl \
  --dry-run \
  --out jetty-dry-run.jsonl

# Live execution requires JETTY_API_TOKEN.
export JETTY_API_TOKEN=...
skill-benchmark run-jetty \
  --payloads jetty-payloads.jsonl \
  --out jetty-runs.jsonl

# Import Jetty artifacts into the normal run layout, then grade locally.
skill-benchmark import-jetty-results \
  --manifest ../repo/evals/shared-benchmark.json \
  --jetty-runs jetty-runs.jsonl \
  --runs ../repo/eval-runs/jetty

skill-benchmark benchmark ../repo/evals/shared-benchmark.json \
  --runs ../repo/eval-runs/jetty \
  --out jetty-benchmark.json
```

Defaults follow Jetty docs and `jettyio/jettyio-skills`: `claude-code`, `claude-sonnet-4-6`, `model_provider=anthropic`, and `snapshot=python312-uv`. The runbook is the system message. Runtime values go in `jetty.template_variables`. Uploaded files go in `jetty.file_paths`. Use `JETTY_BASE_URL` to override `https://flows-api.jetty.io`.
