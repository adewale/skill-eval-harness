# Skill Eval Harness

[![CI](https://github.com/adewale/skill-eval-harness/actions/workflows/ci.yml/badge.svg)](https://github.com/adewale/skill-eval-harness/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

Skill Eval Harness is a Python CLI for testing whether an Agent Skill changes observable output. It reads `evals/shared-benchmark.json`, emits answer-key-safe task rows, grades files under `eval-runs/`, and writes benchmark reports you can diff across variants.

The main question is narrow: **when the same case runs with and without the skill, what changed, what passed, and did the eval itself leak the answer?**

## Core loop

1. **Describe cases** in `evals/shared-benchmark.json`: prompt, split, fixture files, variants, assertions, and ablations.
2. **Prepare tasks** with `skill-benchmark prepare`; generation rows omit `expected_behavior` and judge rubrics unless you explicitly request them.
3. **Run tasks** with Pi, Claude Code, Jetty, or another runner; each run writes `output.md` and optional `metadata.json`.
4. **Grade outputs** with deterministic assertions: string, regex, file, JSON field, and opt-in `script` oracles.
5. **Inspect the report** for pass rates, flaky repeated runs, no-lift cases, saturated assertions, judge tasks, and trigger/no-trigger results.

## What the CLI owns

- Variant pairing: `with_skill`, `without_skill`, optional `old_skill`, and `ablation:<id>`.
- Split discipline: `tune`, `holdout`, and `holdback` stay separate.
- Local grading: deterministic assertions run without model calls.
- Eval hygiene: leakage lint, manifest audit, trigger checks, repeated-run stats, and fixture recommendations.
- Interop: Anthropic-style exports, static HTML review pages, Pi trigger evals, and Jetty runbook-mode import/export.
- Judge plumbing: `judge`/`rubric` assertions can be exported or run through a user-supplied `--judge-cmd`; the harness does not choose a model for you.

## Contents

- [Quick start](#quick-start)
- [Installation](#installation)
- [Manifest format](#manifest-format)
- [Assertions](#assertions)
- [Run output contract](#run-output-contract)
- [Commands](#commands)
- [Jetty adapter](#jetty-adapter)
- [Contributing](#contributing)

## Quick start

> Requires Python 3.10+ and [uv](https://docs.astral.sh/uv/). Install from GitHub first:
>
> ```bash
> uv tool install git+https://github.com/adewale/skill-eval-harness.git@v0.4.2
> ```

Run these from a skill repo that has `evals/shared-benchmark.json`:

```bash
# 1. Check manifest shape and fixture paths.
skill-benchmark validate evals/shared-benchmark.json

# 2. Emit answer-key-safe task rows for a runner.
skill-benchmark prepare evals/shared-benchmark.json \
  --split tune \
  --runs-per-variant 3 \
  --out /tmp/tasks.jsonl

# 3. Run each task with your agent runner and save:
# eval-runs/latest/<case_id>/<variant>/run-<n>/output.md
# eval-runs/latest/<case_id>/<variant>/run-<n>/metadata.json

# 4. Grade saved outputs. Add --allow-scripts only if you trust repo-owned oracles.
skill-benchmark benchmark evals/shared-benchmark.json \
  --runs eval-runs/latest \
  --split tune \
  --allow-scripts \
  --out benchmark.json

# 5. Open a static review page.
skill-benchmark render-viewer \
  --benchmark benchmark.json \
  --runs eval-runs/latest \
  --out review.html
```

Expected landmarks:

```text
validate  -> OK: <skill-name> — <case-count> cases, <ablation-count> ablations
prepare   -> /tmp/tasks.jsonl, one JSON object per case/variant/run
benchmark -> benchmark.json with summary, results, and case_flags
viewer    -> review.html with assertion evidence and output previews
```

`benchmark.json` records one row per case/variant/run, plus aggregate pass rates, timing/token summaries, and flags for saturated, no-lift, flaky, or with-skill-failed cases.

## Installation

### From GitHub

```bash
uv tool install git+https://github.com/adewale/skill-eval-harness.git@v0.4.2
skill-benchmark --help
skill-pi-trigger-eval --help

# One-shot without installing globally:
uvx --from git+https://github.com/adewale/skill-eval-harness.git@v0.4.2 skill-benchmark --help
```

The installed commands are:

| Command | What it does |
|---|---|
| `skill-benchmark` | Validate manifests, prepare tasks, grade outputs, compare variants, run judges, and import/export runner formats. |
| `skill-pi-trigger-eval` | Runs Pi without forced `--skill` and checks whether the model loads the skill from stream events. |

### Local development

```bash
git clone https://github.com/adewale/skill-eval-harness.git
cd skill-eval-harness
uv tool install --editable .
skill-benchmark --help
```

## Documentation map

| File | Use it for |
|---|---|
| `README.md` | Manifest shape, run layout, and command contracts. |
| `CHANGELOG.md` | Release history and unreleased repo-surface changes. |
| `CONTRIBUTING.md` | Local setup, validation commands, and eval-safety rules. |
| `LESSONS_LEARNED.md` | Design lessons from the multi-skill saturation work and the roadmap/cost build-out. |
| `docs/architecture.md` | How the pipeline fits together: the stages, the runner boundary, the model/variant/run fan-out, and the invariants that keep grading honest. |
| `docs/abstractions.md` | What each core object is: manifest, prepared task, run-output contract, assertion result, `ResultSet`. |
| `docs/authoring-evals.md` | Opinionated workflow/quickstart for writing a new eval suite, including severity and graded assertions. |
| `docs/eval-framework-roadmap-spec.md` | The implemented eval-framework roadmap: goals, abstractions, and tests per feature (CF.1–CF.4, buckets 1–4, migration). |
| `docs/migrating-evals.md` | Upgrading a manifest between versions (v1 → v2): what `migrate` stamps and the judgment calls it leaves. |
| `docs/vocabulary.md` | Glossary of harness terms: variants, splits, models, ablations, assertions, severity/oracle tiers, graded scoring, cost telemetry, trace artifacts, and report flags. |
| `docs/evals-are-not-tests.md` | Why a skill eval is not a unit test, and what that changes about reading results. |
| `docs/jetty-support-spec.md` | Jetty payload/import contract and live-token unknowns. |
| `docs/trace-aware-eval-spec.md` | Trace artifact contract, shipped v0.4.1 runner support, process/efficiency assertions, and remaining trace work. |
| `docs/skill-ablation-spec.md` | Design spec for materialized (real, altered skill file) ablations: the three-layer model, manifest schema, removal mechanisms, gates, and phased plan. |
| `docs/ablation-study-walkthrough.md` + `examples/skill-pins.json` | A worked ablation study across ten real skills, pinned to exact commit SHAs (+ canonical tree hashes) so it reproduces against the evaluated versions **without vendoring** any skill content. Includes the replication lesson (2 of 3 single-shot findings refuted at n=5). |
| `docs/repo-effectiveness-audit.md` | `good-repo` audit, score, package metadata fixes, and manual GitHub settings checklist. |
| `TODO.md` | Status tracker: the eval-framework roadmap (implemented, bar two `(TODO-native)` items) and the remaining Jetty adapter work — streaming/concurrency, live API validation, judge export, per-variant overrides, and the `swap:<id>` ablation follow-on. |
| `examples/demo-skill/` | Self-contained, **offline** end-to-end example: a tiny synthetic skill, two materialized ablations, and a deterministic stub runner (no model/API). `prepare → run-codex → benchmark` confirms a regression per ablation; exercised by `tests/test_example_demo.py`. Start here. |
| `examples/adewale-workspace/` | Adewale-specific runners for Pi smoke, trigger, ablation, and aggregate reports. |
| `tests/test_skill_benchmark.py` | Executable examples for grading, leakage lint, script assertions, judge commands, Jetty export/import, trace artifacts, and trigger detection. |

## Manifest format

Each skill repo owns an `evals/shared-benchmark.json` manifest. Add a `harness` block so readers know which external harness/version to install.

```json
{
  "version": 1,
  "skill_name": "good-pr",
  "harness": {
    "name": "skill-eval-harness",
    "url": "https://github.com/adewale/skill-eval-harness",
    "version": ">=0.4.1"
  },
  "skill_paths": ["skills/good-pr/SKILL.md"],
  "variants": ["with_skill", "without_skill"],
  "optional_variants": ["old_skill"],
  "split_policy": {
    "tune": "Visible cases used during iteration.",
    "holdout": "Hidden cases scored only at end-of-round or merge.",
    "holdback": "Examples not exposed in skill/docs/eval descriptions until after scoring."
  },
  "cases": [
    {
      "id": "pos-security-meaningless-test",
      "split": "tune",
      "kind": "pr-review",
      "domain": "pull-request-quality",
      "difficulty": "core",
      "trigger_type": "explicit",
      "success_goals": ["outcome", "style"],
      "prompt": "Security fix PR includes `expect(result).toBeDefined()` as the only auth-bypass test...",
      "files": ["fixtures/security-pr/diff.patch"],
      "expected_behavior": ["Flag the weak test and require regression proof."],
      "assertions": [
        {"name": "detect-weak-test", "type": "contains_any", "values": ["weak", "toBeDefined"]},
        {"name": "qualitative-review", "type": "judge", "rubric": ["Specific", "maintainer-friendly"]}
      ],
      "tags": ["security", "testing"]
    }
  ],
  "ablations": [
    {
      "id": "no-regression-proof",
      "removed_component": "regression-proof requirement",
      "expected_regressions": ["Accepts weak tests that still pass without the fix"]
    }
  ]
}
```

### Splits

| Split | Purpose | Prompt storage |
|---|---|---|
| `tune` | Visible cases used while editing the skill and evals. | Inline `prompt` is fine. |
| `holdout` | Hidden cases scored at end-of-round or merge. | Prefer private `prompt_ref`. |
| `holdback` | Not shown in skill/docs/evals until after scoring; detects memorization. | Prefer private `prompt_ref` and ignored answer keys. |

`prepare` fails on missing hidden prompts unless `--allow-missing-prompts` is used for dry-run planning.

Use optional `files` for fixture-backed evals. Paths are relative to the manifest's `evals/` directory, validated by `validate`, and emitted by `prepare` as absolute `input_files` for the runner.

Further optional manifest surfaces (each with a behavior-preserving default; see `docs/migrating-evals.md`):

- `version`: 1 or 2 — `skill-benchmark migrate` upgrades 1 → 2 by stamping the defaults explicitly.
- `judge`: `{"model": "..."}` — the default judge model for the `judge` command; `audit-manifest` flags `judge-is-model-under-test` (fatal under `--strict-judge`).
- `datasets` + a case `template`: fan one case template over rows with `{key}` placeholder filling and stable ids (`<case>-<row id|index>`); leakage lint runs per materialized case.
- `turns` on a case: a scripted multi-turn sequence; each turn's assertions grade that turn's transcript entry (`turn-<n>/output.md`), case-level assertions grade the final answer.
- YAML manifests: a `.yaml` manifest (plus `dataset_files` mapping dataset ids to JSONL row files) compiles to the same shape in memory — validation, lint, and grading are identical.
- Reference floors: `reference_score` (0-1) / `reference_graded_score` (1-5).

## Assertions

Objective assertion types:

| Type | Checks |
|---|---|
| `contains` | One substring is present. |
| `contains_any` | At least one substring is present. |
| `contains_all` | Every listed substring is present. |
| `excludes_any` | No listed substring is present. |
| `regex` | Regex matches output. |
| `not_regex` | Regex does not match output. |
| `file_exists` | A file exists relative to the run directory. |
| `json_field_equals` | A JSON field equals an expected value. |
| `golden_output` | Output (or a named artifact) equals a reference file; explicit normalization (`exact` default, `trim`, `text`); unified diff as failure evidence. |
| `similarity` | difflib ratio against an `expected` string with a `threshold` (default 0.8), emitting a score. `mode: "embedding"` uses cosine similarity behind the opt-in `--embed-cmd`. |
| `structured_output` | JSON (an artifact via `path`, or extracted from the output) validates against a deterministic JSON-Schema subset (`type`/`properties`/`required`/`items`/`enum`/`const`/`minItems`/`maxItems`). |
| `script` | Opt-in deterministic oracle command against the output directory. A stdout line like `{"score": 6, "max_score": 7}` feeds the graded channel; exit code still decides pass/fail. |
| `skill_invoked` | Trace/process check that the runner loaded the skill, or did not, as expected. |
| `command_ran` / `command_not_ran` | Trace/process checks over normalized command events. |
| `command_order` | Trace/process check that commands appeared in a required order. |
| `tool_call` | A tool call matching `tool`/`pattern` occurred (with `min_count`/`max_count` bounds), or an ordered `order` list of calls. Matches completed call inputs, never outputs. |
| `tool_count_le` / `no_repeated_command_loop` | Trace/process budgets for tool use and thrashing. |
| `total_tokens_le` / `elapsed_seconds_le` / `command_count_le` | Efficiency checks over `metrics.json`, `metadata.json`, or normalized events. |

Every assertion may declare a **severity** — `critical` (an absorbing barrier: one failure vetoes the run, every rate collapses to 0.0 and the graded score is withheld), `gate` (lowers the pass rate; the default for objective types), or `soft` (feeds only the graded score channel — a soft failure never moves the objective, qualitative, or combined pass rates; the default for judge/similarity). Declare `severity: "gate"` on a judge assertion to keep it in the qualitative/combined rate. `--strict` on `grade`/`benchmark` promotes soft to gate. An `atLeast` floor on a scored assertion decides its pass. Every assertion may also declare an **oracle tier** — `strong` (deterministic, the default for text/process/efficiency), `demo` (the default for `script`), or `live` (judge) — reported per case as `oracle_strength` and audited (`weak-oracle-only`).

Use `script` when a keyword check is too weak for the property you care about. The command sees the candidate run directory, so it can inspect `output.md`, generated files under `outputs/`, or metadata. Script assertions are blocked unless you pass `--allow-scripts` to `grade`, `benchmark`, `aggregate`, or `export-anthropic`:

```json
{
  "name": "oracle-pass",
  "type": "script",
  "command": ["python3", "oracles/oracle.py", "{output_dir}"],
  "pass_exit_code": 0,
  "timeout_s": 30
}
```

`command` runs with cwd set to the manifest directory. `{output_dir}` is replaced with the absolute run directory. The assertion passes when the command exits with `pass_exit_code` (default `0`); stdout and stderr are stored as evidence.

Trace/process/efficiency assertions are optional and fail closed when declared evidence is missing. For example, `command_not_ran` cannot pass without `events.json`, and `total_tokens_le` cannot pass without token telemetry.

Assertions can be scoped to variants when the expected process differs by arm:

```json
{"name":"with-skill-loaded","type":"skill_invoked","expected":true,"variants":["with_skill"]}
{"name":"without-skill-clean","type":"skill_invoked","expected":false,"variants":["without_skill"]}
```

Use this for process checks such as `skill_invoked`; otherwise a with-skill requirement would incorrectly penalize the no-skill baseline.

Qualitative assertion types:

| Type | Behavior |
|---|---|
| `judge` | Deferred into `judge-tasks.jsonl`; merge results with `--judge-results`. |
| `rubric` | Same deferred qualitative flow. |
| `factuality` | Preset: a judge assertion carrying a canned anchored factuality rubric (threshold 4). `preset: "factuality"` on a judge assertion does the same. |

A judge assertion may carry **anchored graded dimensions** (`graded_dimensions: [{name, scale: "1-5", rubric: "5 = …observable…; 1 = …"}]` — the judge returns `dimension_scores`, normalized to 0-1, passing at `threshold` ≥ 4 by default) or a **dynamic rubric** (`dynamic_rubric: {instruction, minimum_criteria}` — the judge drafts case-specific criteria and must meet the minimum). A case may set a reference floor (`reference_score` 0-1 or `reference_graded_score` 1-5); scoring below it flags `below-reference-floor`. Paired reports carry a sign-flip permutation `significance` block beside every lift, and a `graded` channel when graded scores exist.

Judge results are keyed by `judge_task_id`:

```json
{"judge_task_id":"case::with_skill::run-1::qualitative-review","passed":true,"score":4,"evidence":"Specific evidence from output"}
```

## Run output contract

The harness grades either the legacy layout:

```text
runs/<case_id>/<variant>/output.md
runs/<case_id>/<variant>/metadata.json
```

or repeated/artifact layout:

```text
runs/<case_id>/<variant>/run-1/output.md
runs/<case_id>/<variant>/run-1/metadata.json
runs/<case_id>/<variant>/run-2/outputs/<artifact files>
```

Trace-aware runners may also write:

```text
runs/<case_id>/<variant>/run-1/trace.jsonl       # raw runner event stream
runs/<case_id>/<variant>/run-1/events.json       # normalized events used by process assertions
runs/<case_id>/<variant>/run-1/metrics.json      # tokens, commands, tool calls, elapsed time, retries
runs/<case_id>/<variant>/run-1/environment.json  # runner/model/sandbox details where available
```

`metadata.json` is optional, but include what your runner can capture:

```json
{
  "elapsed_ms": 12345,
  "input_tokens": 1000,
  "output_tokens": 500,
  "total_tokens": 1500,
  "model": "anthropic/claude-sonnet-4"
}
```

## Commands

### Validate

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

### Prepare tasks

```bash
skill-benchmark prepare ../repo/evals/shared-benchmark.json --split tune --out tasks.jsonl
skill-benchmark prepare ../repo/evals/shared-benchmark.json --runs-per-variant 5 --out tasks.jsonl
skill-benchmark prepare ../repo/evals/shared-benchmark.json --include-ablations --ablation-dir ablated-skills --out ablation-tasks.jsonl
```

`--include-ablations` requires `--ablation-dir DIR` whenever any ablation declares a removal (a `mechanism`/`components` + `target`): the altered skill tree is materialized there and the prepared rows point at it. (A manifest with only instruction-simulated ablations does not need it.)

Use `--include-answer-key` only for judge/debug tasks, never for generation runs.

### Import runner traces

Normalize a raw JSONL trace into `events.json` and `metrics.json` for process and efficiency assertions:

```bash
skill-benchmark import-trace \
  --source codex \
  --trace ../repo/eval-runs/latest/case/with_skill/run-1/trace.jsonl \
  --run-dir ../repo/eval-runs/latest/case/with_skill/run-1 \
  --write-metadata
```

### Run Codex JSONL tasks

`run-codex` executes prepared rows through a command compatible with `codex exec --json`, saves `trace.jsonl`, normalizes events/metrics, extracts the final answer into `output.md`, and records nonzero/timeouts as failed run artifacts:

```bash
skill-benchmark prepare ../repo/evals/shared-benchmark.json --split tune --out tasks.jsonl
skill-benchmark run-codex --tasks tasks.jsonl --runs ../repo/eval-runs/codex-tune
```

Override `--codex-cmd` for local wrappers or tests. A concrete Codex smoke command is:

```bash
skill-benchmark run-codex \
  --tasks tasks.jsonl \
  --runs ../repo/eval-runs/codex-trace \
  --codex-cmd 'codex exec --json --sandbox read-only --skip-git-repo-check --ephemeral'
```

### Run Claude tasks (with cost capture)

`run-claude` executes prepared rows through `claude -p --output-format json`, extracts the answer into `output.md`, and records real per-run `total_cost_usd` + token usage into `metrics.json`. The benchmark report then totals `cost_usd_total` per arm (over scorable runs), so a paired eval reports actual dollars:

```bash
skill-benchmark prepare ../repo/evals/shared-benchmark.json --split tune --out tasks.jsonl
skill-benchmark run-claude --tasks tasks.jsonl --runs ../repo/eval-runs/claude-tune \
  --model claude-haiku-4-5-20251001
```

`--model` is optional (omit for the CLI default); `--claude-bin` overrides the executable (a stub in tests). A nonzero exit/timeout is written as a `[CLAUDE FAILURE …]` body, which `execution_valid` treats as a non-scorable infra failure, exactly like the Codex/Jetty runners.

### Run subagent tasks (in-process seam, tool replay, multi-turn)

`run-subagent` drives prepared rows through an in-process backend — the Claude CLI by default, any provider via `--agent-cmd` (prompt JSON on stdin, `{answer, trace?, usage?}` JSON on stdout), or a plain function in tests. It writes the same run-output contract (plus normalized `events.json`/`metrics.json` from a returned trace), reuses the isolated per-variant workspace (so the CF.2 baseline-isolation invariant covers it), honors row-level models, and drives multi-turn `turns` sequences into `turn-<n>/output.md`. Tool I/O can be recorded and replayed deterministically via `--tool-replay record|replay|strict|auto` (or `$SKILL_BENCHMARK_TOOL_REPLAY`), stored as `tool-replay.json` beside each run; `strict` fails closed on an unrecorded call.

```bash
skill-benchmark run-subagent --tasks tasks.jsonl --runs eval-runs/subagent --tool-replay record
```

### Compare judges (judge-sensitivity)

A single judge number is not reproducible across judge choice for a subtle skill. Judge the same runs with two models (`benchmark --judge-results` merges each), then `compare-judges` flags whether the measured lift depends on the judge:

```bash
skill-benchmark compare-judges \
  --report haiku=benchmark.haiku.json \
  --report sonnet=benchmark.sonnet.json \
  --out judge-panel.json
```

It reports each judge's `with_skill − without_skill` combined lift and sets `sign_sensitive` (judges disagree the skill helps), `magnitude_sensitive` (lift spread > `--magnitude-eps`, default 0.1), and `judge_sensitive` (either). Needs ≥2 `--report name=path`. Every verdict from `judge` carries its `judge_model`, so which model graded a run is always recoverable.

### Pi trace runners

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

### Grade

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

### Benchmark

`benchmark` aggregates graded rows into variant summaries, paired deltas (with sign-flip `significance` and a `graded` channel), per-model grouping (`by_model`, `model_analysis` ranking and lift losers), slice summaries with lift concentration, oracle-strength shares, held-out vs tune-visible qualitative rates, and case flags. Add `--allow-scripts` only when you trust the repo-owned oracle commands in the manifest; `--strict` promotes soft assertions to gates; `--embed-cmd` enables embedding-mode similarity.

```bash
skill-benchmark benchmark ../repo/evals/shared-benchmark.json \
  --runs ../repo/eval-runs/latest \
  --allow-scripts \
  --judge-results judge-results.jsonl \
  --out benchmark.json
```

Multi-model runs prepare with `--models a,b,c` (run dirs gain a model segment: `<case>/<model>/<variant>`); grading discovers both layouts and pairs lift per (case, model).

### CI report formats

`report` serializes a `benchmark.json` for CI: `--format junit` writes one `<testcase>` per case/variant/run with evidence on failures and the paired lift as suite properties; `--format github` writes job-summary markdown plus `::warning` annotations per flagged case (and an `::error` on negative lift).

```bash
skill-benchmark report --benchmark benchmark.json --format junit --out junit.xml
skill-benchmark report --benchmark benchmark.json --format github --out "$GITHUB_STEP_SUMMARY"
```

### Trend, staleness, and harder-case suggestions

`trend` keeps an append-only history of benchmark reports and emits the series, successive diffs, recurring failures ranked by prevalence x severity, and prune candidates (cases that never failed and never discriminated across the history — suggestions only, nothing is deleted). `suggest-cases` turns saturated/no-lift flags into harder-case candidate seeds; generation is opt-in behind `--generate-cmd` and never edits a manifest.

```bash
skill-benchmark trend --history eval-history --add benchmark.json --out trend.json
skill-benchmark suggest-cases --benchmark benchmark.json --manifest evals/shared-benchmark.json --out candidates.json
```

### Migrate a manifest

`migrate` upgrades a version-1 manifest to version 2: stamps default severities and oracle tiers, marks binary judge rubrics with a `graded?` todo, prints the diff plus the judgment-call checklist (`--check` for a dry run, `--out-checklist` to save it). See `docs/migrating-evals.md` for the agent runbook.

### Judge command backend

Run deferred `judge`/`rubric` assertions with a command that reads one grading prompt from stdin and emits JSON on stdout. The prompt contains the original case prompt, `expected_behavior`, `review_rubric`, the assertion, and the saved candidate output.

```bash
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

The judge command should return JSON like `{"passed": true, "score": 4, "rationale": "..."}`. Bare or fenced JSON is accepted using `json.raw_decode` scanning rather than brace counting. `--transcripts` saves the exact prompt, stdout, stderr, and parsed result for each judge task.

### Audit manifest quality

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

The systematic way to upgrade a suite is to drive those blockers to empty, repo by repo: materialize the ablations (`materialize-ablations` / declare a `mechanism`+`target`), de-leak the leak-saturated cases (move the answer out of the prompt, or assert a downstream consequence), and add adversarial cases where missing — then the gate goes green.

### Profile skill size and references

```bash
skill-benchmark profile-skill ../repo/evals/shared-benchmark.json \
  --format markdown \
  --out skill-profile.md
```

`profile-skill` reports `SKILL.md` token estimates, reference-file counts/sizes, heading/module counts, and warnings for overly broad or oversized skills. These warnings are advisory; focused 2–3-module skills are often easier for agents to apply, but large skills can be justified when references are conditional.

### Cost telemetry (tokens and dollars)

Cost is a first-class eval signal (issue #21). Every runner path — Pi smoke, Pi trigger, `run-codex`, `run-claude`, `run-subagent`, the judge wrapper, and the Jetty importer — writes two normalized blocks into run metadata beside the raw provider fields (which are preserved unchanged for audit):

- `usage_normalized`: alias-normalized token counts (`input`/`prompt_tokens`/`totalTokens`/cache/reasoning variants) with a `source` — `provider_reported` (relayed from the provider), `trace_normalized` (summed from normalized trace events), `estimated`, `missing`, or `not_applicable`.
- `cost_normalized`: dollar cost with `currency`, per-part costs when reported, and a `source` — `provider_reported` vs `price_table_estimated` are never conflated, and **missing cost is marked `missing`, never written as zero**. Provider-reported blocks always beat trace-derived ones; offline/stub runs carry explicit `missing` markers.

Consumers of the blocks:

- `benchmark`/`aggregate` emit `cost_summary`: coverage (how many runs actually carried telemetry), operational totals (**every run counts here, including execution errors — a timed-out run still cost money — while quality rates keep excluding them**), per-variant token/cost stats (mean/median/p90), per-case spend, paired `with - without` cost deltas, ablation marginal cost and cost per confirmed regression, and judge spend as its own line (never folded into model-under-test cost).
- `cost-summary` writes the standalone suite ledger (`--out cost-summary.json`, `--md cost-summary.md`): coverage, totals, by variant/case/runner, top expensive cases and ablation arms, and `cost_quality_findings` when a `--benchmark` report is joined.
- `suite-run` projects spend **before any model call** from previous ledgers (`--cost-history <dir>`, per-run medians) or a static assumption (`--assumed-tokens-per-run`), and gates on `--max-estimated-tokens` / `--max-estimated-cost-usd` — failing closed when a dollar cap is set but no dollar estimate exists — unless `--allow-over-budget`.
- `audit-manifest --runs` adds cost-quality findings above `--expensive-case-usd` (default $1): `expensive-saturated-case`, `expensive-no-lift-case`, `high-cost-judge-only-case`, `ablation-high-spend-no-structured-regression`, and `high-footprint-low-lift-skill`.

Interpretation rule: `provider_reported` numbers are the bill; `trace_normalized` reconstructs usage from events (good for tokens, silent on dollars); `missing` means the run truly carried no telemetry — fix the runner path rather than treating it as free.

### Token overhead

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

If a repo has no paired trace metrics, the report still includes the static footprint and shows `0` runtime pairs.

### Suite preflight / allowlisted multi-skill tiers

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

### Aggregate many skills

```bash
skill-benchmark aggregate \
  $(cat examples/adewale-workspace/all-manifests.txt) \
  --runs-root .. \
  --runs-subdir eval-runs/latest \
  --out aggregate-benchmark.json
```

### Export Anthropic-compatible benchmark

```bash
skill-benchmark export-anthropic ../repo/evals/shared-benchmark.json \
  --runs ../repo/eval-runs/latest \
  --out benchmark.anthropic.json
```

### Blind comparison

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

### Review viewer (static or served)

```bash
skill-benchmark render-viewer \
  --benchmark benchmark.json \
  --runs ../repo/eval-runs/latest \
  --out review.html
```

The viewer embeds run artifacts (images inline, typed links for pdf/xlsx, text in place). `--previous-workspace <dir>` embeds a diff against that iteration's `benchmark.json` (per-variant deltas, per-case deltas, new/resolved flags; pair with the `iteration-N/` directory convention). `--serve --port 8642` hosts the review with a feedback form persisting to `feedback.json` (entries keyed by case/variant/run).

### Pi trigger evals

```bash
skill-pi-trigger-eval ../repo/evals/shared-benchmark.json \
  --split tune \
  --runs-per-query 3 \
  --out trigger-report.json
```

This creates a temporary `PI_CODING_AGENT_DIR`, copies the skill under `skills/`, runs Pi without forced `--skill`, and detects whether the model loaded the skill from JSON stream events.

### Jetty adapter

Jetty support is optional. The harness exports runbook-mode chat-completion payloads, Jetty executes them, and `import-jetty-results` copies `output.md`, artifacts, and metadata back into the normal run layout.

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

## Ablations

Ablations are opt-in variants that remove part of a skill — by simulation, or by materializing a real altered skill (below). Add entries under `manifest.ablations`, then prepare with `--include-ablations`.

```bash
skill-benchmark prepare ../repo/evals/shared-benchmark.json \
  --split tune \
  --include-ablations \
  --ablation-dir ablated-skills \
  --out ablation-tasks.jsonl
```

Ablation task variants are named `ablation:<id>`. Routing is by case population: **answer-population** ablations (instructions/resource/runtime/preprocess) run on non-trigger cases through the generic runners. **Discovery-population** ablations (e.g. a weakened `description`/`when_to_use`) measure whether the skill still *autonomously loads*, which the forced-load generic runners cannot observe — so `prepare` does **not** emit rows for them; run them through `run_pi_trigger_eval.py --ablation <id>` instead.

### Materialized ablations

By default an ablation is *instruction-simulated*: the runner is told to ignore a component. To produce a real, altered skill instead, declare a removal `mechanism` (or a `components` list) and `target` on the ablation, then materialize the trees:

```bash
skill-benchmark materialize-ablations ../repo/evals/shared-benchmark.json \
  --out-dir ablated --out ablated/provenance.json
```

Each declared ablation is written to `ablated/<id>/` as a complete altered skill tree (every manifest root, identical surface to `with_skill`, differing only by the declared edit). Mechanisms are `frontmatter_field`, `section` (fence-aware), `list_item`, deletion-only `patch`, `reference` (pointer/content/both), `script`, `asset`, and `preprocess` (inline `` !`command` ``), composable across multiple components. Ablation is removal-only — replacement/substitution is the separate `swap:<id>` feature tracked in `TODO.md`. Materialized arms are blind: the model-visible input is identical to `with_skill` (the hypothesis lives only in harness metadata).

The materialized tree flows through the runners: the Pi smoke runner mounts it (answer-population only), `run_pi_trigger_eval.py --ablation <id>` trigger-tests a discovery (e.g. weakened-description) skill, and `export-jetty --include-ablations --ablation-dir DIR` uploads it recursively. `prepare`/`export-jetty` emit only **answer-population** ablation rows (on non-trigger cases); discovery ablations are measured by the trigger adapter. The benchmark report's `ablation_regressions` block separates an aggregate "score regressed" from an assertion-level "expected regression confirmed", and only confirms when recorded provenance proves both arms ran the same skill revision. See [`docs/skill-ablation-spec.md`](docs/skill-ablation-spec.md) for the mechanism table, the component-class model, and the correctness gates.

**Evidence asymmetry (discovery vs answer).** The two paths do not yet have equal evidentiary strength:

- **Answer-population** ablations get *confirmed* causal evidence: a provenance-gated, paired with_skill-vs-ablation comparison where a confirmation requires verified provenance and a same-revision canonical hash on both arms.
- **Discovery** ablations run through `run_pi_trigger_eval.py --ablation`, which currently emits a **raw autonomous-trigger measurement for a single arm** (`evidence_class: raw_autonomous_trigger_measurement`), not a paired, provenance-verified baseline-vs-ablation comparison. Each result records a `skill_tree_hash` (baseline = canonical tree; ablation = parent tree) so a future pairing can verify both arms ran the same revision, but until that pairing exists, **read a trigger pass-rate as a measurement, not a confirmed ablation effect.**

## Compatibility notes

- **Anthropic skill-creator**: use `grade --write-grading-files` and `export-anthropic` for compatible `grading.json`/`benchmark.json` shapes.
- **Pi**: use `examples/adewale-workspace/run_pi_smoke.py` for the Adewale multi-repo smoke workflow and `skill-pi-trigger-eval` for autonomous trigger checks.
- **Other runners**: use `prepare` JSONL as the import format and write results back to the run output contract.
- **Jetty**: use `export-jetty`, `run-jetty`, and `import-jetty-results` for REST runbook-mode execution. Live response shapes still need token-backed smoke validation before treating Jetty runs as production evidence.

## Contributing

See [`CONTRIBUTING.md`](CONTRIBUTING.md) for local setup, validation commands, and eval-safety rules. The short version:

```bash
python3 -m py_compile *.py examples/adewale-workspace/*.py
python3 -m unittest discover tests -v
```

For manifest or grading changes, add or update `tests/test_skill_benchmark.py`. For docs-only changes, still run the same commands so CLI examples stay tied to current behavior.

## Non-goals

- Grading and aggregation do not call a model. Model execution happens outside that path, except for the explicit runner/judge commands that exist to call one: `run-codex`, `run-claude`, `run-jetty`, and `judge` (via `--judge-cmd` or, natively, `--judge-model`).
- The harness does not decide qualitative truth by itself; it emits judge prompts, runs a judge (an opt-in `--judge-cmd`, or `--judge-model` for the native Claude judge), and merges the returned JSON — recording which `judge_model` produced each verdict.
- Hidden prompts are not protected if you pass `--include-answer-key` to generation jobs.
- A passing answer benchmark does not prove autonomous skill loading; run `skill-pi-trigger-eval` for that.

## Repository layout

```text
skill-eval-harness/
├── README.md
├── CHANGELOG.md
├── CONTRIBUTING.md
├── LESSONS_LEARNED.md
├── TODO.md
├── pyproject.toml
├── skill_benchmark.py          # the CLI, grading, reporting, and runner adapters
├── run_pi_trigger_eval.py      # autonomous-trigger runner
├── ablation_model.py           # typed ablation/provenance value objects
├── docs/                       # architecture, abstractions, vocabulary, specs, guides (see the map above)
├── .github/
│   ├── PULL_REQUEST_TEMPLATE.md
│   ├── ISSUE_TEMPLATE/
│   └── workflows/ci.yml
├── examples/
│   ├── demo-skill/             # offline end-to-end example (stub runner, materialized ablations)
│   ├── skill-pins.json         # pinned SHAs + tree hashes for the ablation study
│   └── adewale-workspace/      # Pi smoke/trigger/ablation runners and aggregate reports
└── tests/                      # test_skill_benchmark.py + roadmap/cost/confidence-floor/doc-ref suites
```

## Development

```bash
python3 -m py_compile *.py examples/adewale-workspace/*.py
python3 -m unittest discover tests -v
```

The test suite covers repeated runs, artifact outputs, answer-key omission, leakage lint, script assertions, judge-command parsing, Anthropic export shape, Jetty export/import, trace normalization, variant-scoped process assertions, Codex JSONL runs, Pi trigger traces, and Pi smoke workspace isolation — plus the roadmap features (`test_roadmap_features.py`), cost telemetry (`test_cost_telemetry.py`), the confidence floor and detector fixtures (`test_confidence_floor.py`), and the executable doc-reference guard (`test_doc_refs.py`).

## Source checked

This README was written against:

- `skill_benchmark.py` CLI and assertion implementation
- `run_pi_trigger_eval.py` trigger runner
- `pyproject.toml` package metadata
- `docs/repo-effectiveness-audit.md` for the current `good-repo` audit
- `tests/test_skill_benchmark.py` behavior coverage
- `CHANGELOG.md`, `CONTRIBUTING.md`, and `.github/` contribution/CI surfaces
- `anti-slop-writing/skills/anti-slop-writing/SKILL.md` for the v0.4.1 docs cleanup and consistency pass
- the `good-readme` skill guidance from `https://www.skills.sh/adewale/good-readme/good-readme`
- the `good-repo` skill guidance from `good-repo/skills/good-repo/references/quality-checklist.md`
