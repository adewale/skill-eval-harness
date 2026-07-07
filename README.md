# Skill Eval Harness

[![CI](https://github.com/adewale/skill-eval-harness/actions/workflows/ci.yml/badge.svg)](https://github.com/adewale/skill-eval-harness/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

Skill Eval Harness is a Python CLI that measures the **causal lift** of an Agent Skill: it runs the same case with and without the skill, then reports what changed, what passed, and whether the eval leaked its own answer. It reads `evals/shared-benchmark.json`, emits answer-key-safe task rows, grades files under `eval-runs/` locally and deterministically — no model call in the grade path — and writes benchmark reports you can diff across variants.

General eval frameworks (openai/evals, vitest-evals, viteval) score one output against a rubric. This one measures the *difference the skill makes*, and spends its surface area on keeping that difference honest: paired with/without comparison, `tune`/`holdout`/`holdback` split discipline, leakage lint, materialized ablations with provenance gates, and per-model lift. None of those frameworks have them, and they are what make a reported number trustworthy rather than merely green.

## Core loop

1. **Describe cases** in `evals/shared-benchmark.json`: prompt, split, fixture files, variants, assertions, and ablations.
2. **Prepare tasks** with `skill-benchmark prepare`; generation rows omit `expected_behavior` and judge rubrics unless you explicitly request them.
3. **Run tasks** with Pi, Claude Code, Jetty, or another runner; each run writes `output.md` and optional `metadata.json`.
4. **Grade outputs** with deterministic assertions: string, regex, file, JSON field, and opt-in `script` oracles.
5. **Inspect the report** for pass rates, flaky repeated runs, no-lift cases, saturated assertions, judge tasks, and trigger/no-trigger results.

## What the CLI owns

- Causal lift: `with_skill` vs `without_skill` (plus optional `old_skill` and `ablation:<id>`), with paired significance and per-model lift.
- Split discipline: `tune`, `holdout`, and `holdback` stay separate, so you can't tune on your test set.
- Local grading: deterministic assertions run without model calls.
- Eval hygiene: leakage lint, manifest audit, trigger checks, repeated-run stats, and fixture recommendations.
- Activation: does the skill load on its own? `skill-trigger-matrix` reports autonomous trigger rates per (agent × model), split by should-fire / should-not-fire.
- Cost as a signal: normalized token/dollar telemetry per run, a suite cost ledger, and lift-per-dollar (`cost-summary`, `token-overhead`).
- Interop: Anthropic-style exports, static/served HTML review pages, and Jetty runbook-mode import/export.
- Judge plumbing: `judge`/`rubric` assertions can be exported or run through native Claude/Codex backends (`--judge-backend`) or a user-supplied `--judge-cmd`; the harness does not choose a model for you.

## Contents

- [Quick start](#quick-start)
- [Installation](#installation)
- [Manifest format](#manifest-format)
- [Assertions](#assertions)
- [Run output contract](#run-output-contract)
- [Ablations](#ablations)
- [Commands](#commands) (full detail in [`docs/commands.md`](docs/commands.md))
- [Jetty adapter](docs/commands.md#jetty-adapter)
- [Contributing](#contributing)

## Quick start

> Requires Python 3.10+ and [uv](https://docs.astral.sh/uv/). Install from GitHub first:
>
> ```bash
> uv tool install git+https://github.com/adewale/skill-eval-harness.git@main
> ```
>
> `@main` matches the development branch. Pinning the latest release tag (`@v0.5.0`) is more reproducible and matches this release's documented command surface.

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

`benchmark.json` records one row per case/variant/run, plus aggregate pass rates, timing/token summaries, and flags for saturated, no-lift, flaky, or with-skill-failed cases. It also carries a `reliability` block — unbiased **pass@k** and **pass^k** per (case, variant) from the repeated runs — beside the paired lift's sign-flip `significance`.

## Installation

### From GitHub

```bash
# Track main (matches this README), or pin the latest release tag for reproducibility.
uv tool install git+https://github.com/adewale/skill-eval-harness.git@main
skill-benchmark --help
skill-pi-trigger-eval --help

# One-shot without installing globally:
uvx --from git+https://github.com/adewale/skill-eval-harness.git@main skill-benchmark --help
```

The installed commands are:

| Command | What it does |
|---|---|
| `skill-benchmark` | Validate manifests, prepare tasks, grade outputs, compare variants, run judges, and import/export runner formats. |
| `skill-pi-trigger-eval` | Runs Pi without forced `--skill` and checks whether the model loads the skill from stream events. |
| `skill-trigger-matrix` | Measures autonomous skill activation per (agent, model) cell — Claude Code subagents on haiku/sonnet/opus by default, Pi and an offline stub included, other agents via an adapter subclass. |

### Local development

```bash
git clone https://github.com/adewale/skill-eval-harness.git
cd skill-eval-harness
uv tool install --editable .
skill-benchmark --help
```

## Documentation map

[`docs/README.md`](docs/README.md) groups these by kind (user journeys, concepts, specs, audits) and holds the convention for adding a new user-journey walkthrough.

| File | Use it for |
|---|---|
| `README.md` | Manifest shape, run layout, and the command index. |
| `docs/commands.md` | Full per-command reference: flags, examples, and output shapes for every subcommand. |
| `CHANGELOG.md` | Release history and unreleased repo-surface changes. |
| `CONTRIBUTING.md` | Local setup, validation commands, and eval-safety rules. |
| `LESSONS_LEARNED.md` | Design lessons from the multi-skill saturation work and the roadmap/cost build-out. |
| `docs/architecture.md` | How the pipeline fits together: the stages, the runner boundary, the model/variant/run fan-out, and the invariants that keep grading honest. |
| `docs/abstractions.md` | What each core object is: manifest, prepared task, run-output contract, assertion result, `ResultSet`. |
| `docs/authoring-evals.md` | Opinionated workflow/quickstart for writing a new eval suite, including severity and graded assertions. |
| `docs/tuning-skill-activation.md` | The activation-tuning loop: trigger cases in both polarities, the (agent, model) trigger-rate matrix, how to read under/over-trigger, and the adapter seam for adding agents. |
| `docs/is-my-skill-worth-its-tokens.md` | Keep/trim/cut walkthrough: static footprint (`profile-skill`) vs. runtime lift-per-token and lift-per-dollar (`token-overhead`, `cost-summary`). |
| `docs/gating-ci-on-evals.md` | The CI recipe: `report --format junit|github` for regressions plus `audit-manifest --fail-on-blockers` for manifest trust. |
| `docs/did-my-skill-edit-regress.md` | The edit → re-run → diff loop: the within-run `ablation_regressions` block (assertion-level, significance-gated) and cross-iteration `render-viewer --previous-workspace` diffs over the `iteration-N/` convention. |
| `docs/which-model-should-my-skill-target.md` | Ranking model tiers by lift: `prepare --models` fan-out, the `by_model` / `model_analysis` blocks, and reading real lift vs. base-model saturation per tier. |
| `docs/why-did-this-run-fail.md` | Debugging one failing run: the `error-analysis` taxonomy + review queue, then the run dir (`output.md`/`metadata.json`), mapped to a failure class and a manifest-or-skill decision. |
| `docs/eval-framework-roadmap-spec.md` | The implemented eval-framework roadmap: goals, abstractions, and tests per feature (CF.1–CF.4, buckets 1–4, migration). |
| `docs/migrating-evals.md` | Upgrading a manifest between versions (v1 → v2): what `migrate` stamps and the judgment calls it leaves. |
| `docs/vocabulary.md` | Glossary of harness terms: variants, splits, models, ablations, assertions, severity/oracle tiers, graded scoring, cost telemetry, trace artifacts, and report flags. |
| `docs/evals-are-not-tests.md` | Why a skill eval is not a unit test, and what that changes about reading results. |
| `docs/academic-grounding.md` | The research constructs behind the harness's terms, with citations; meshes the workflow, measurement, and theory layers. |
| `docs/jetty-support-spec.md` | Jetty payload/import contract and live-token unknowns. |
| `docs/trace-aware-eval-spec.md` | Trace artifact contract, shipped v0.4.1 runner support, process/efficiency assertions, and remaining trace work. |
| `docs/agent-backend-interface-spec.md` | Draft spec for turning Claude/Codex/Gemini/Vibe support into a shared agent backend interface: parity matrix, judge backends, trigger adapters, telemetry, and tool replay. |
| `docs/skill-ablation-spec.md` | Design spec for materialized (real, altered skill file) ablations: the three-layer model, manifest schema, removal mechanisms, gates, and phased plan. |
| `docs/ablation-study-walkthrough.md` + `examples/skill-pins.json` | A worked ablation study across ten real skills, pinned to exact commit SHAs (+ canonical tree hashes) so it reproduces against the evaluated versions **without vendoring** any skill content. Includes the replication lesson (2 of 3 single-shot findings refuted at n=5). |
| `docs/repo-effectiveness-audit.md` | `good-repo` audit, score, package metadata fixes, and manual GitHub settings checklist. |
| `TODO.md` | Status tracker: the eval-framework roadmap (implemented, bar two `(TODO-native)` items) and the remaining Jetty adapter work — streaming/concurrency, live API validation, judge export, per-variant overrides, and the `swap:<id>` ablation follow-on. |
| `examples/demo-skill/` | Self-contained, **offline** end-to-end example: a tiny synthetic skill, two answer-path materialized ablations, one discovery ablation for trigger examples, and a deterministic stub runner (no model/API). `prepare → run-codex → benchmark` confirms a regression per answer-path ablation; exercised by `tests/test_example_demo.py`. Also carries should-fire/should-not-fire trigger cases for `skill-trigger-matrix` (offline via `--agent stub`; live smoke via `RUN_TRIGGER_SMOKE=1`). Start here. |
| `examples/adewale-workspace/` | Adewale-specific Pi smoke runner and cross-repo aggregate report (the trigger runners are the top-level `skill-pi-trigger-eval` and `skill-trigger-matrix`). |
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
    "version": ">=0.5.0"
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
| `tool_call` | A tool call matching `tool`/`pattern` occurred (with `min_count`/`max_count` bounds), or an ordered `order` list of calls. BFCL-style set relations over completed-call **tool names** (exact, case-insensitive — *not* substring): `expected_no_call` (the named tool, or any name matching `pattern`, must *not* have been called), `required_calls` (an order-independent subset of tool names that must all appear, extras allowed), `call_set` (an exact multiset of tool names — same names and multiplicities, no unexpected named calls). Use `pattern`/`order`/`command_ran` for regex or command-text matching. Matches completed call inputs, never outputs. |
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

The materialized tree flows through the runners: the Pi smoke runner mounts it (answer-population only), `run_pi_trigger_eval.py --ablation <id>` trigger-tests a discovery (e.g. weakened-description) skill, and `export-jetty --include-ablations --ablation-dir DIR` uploads it recursively. `prepare`/`export-jetty` emit only **answer-population** ablation rows (on non-trigger cases); discovery ablations are measured by the trigger adapter. The benchmark report's `ablation_regressions` block separates an aggregate "score regressed" from an assertion-level "expected regression confirmed", and only confirms when recorded provenance proves both arms ran the same skill revision **and** the replicated regression clears a significance test (a two-sided permutation test run **per case** over that case's per-run scores; a regression is significant iff at least one confirmed case clears p≤0.05). Because the exact permutation discretizes, a case needs **≥4 runs per arm** to ever reach significance (`C(8,4)=70` → minimum p `2/70≈0.029`); a single-shot (or 3-per-arm) ablation ties at a p it cannot pass and is reported `INDETERMINATE`, never confirmed. See [`docs/skill-ablation-spec.md`](docs/skill-ablation-spec.md) for the mechanism table, the component-class model, and the correctness gates.

**Evidence asymmetry (discovery vs answer).** The two paths do not yet have equal evidentiary strength:

- **Answer-population** ablations get *confirmed* causal evidence: a provenance-gated, paired with_skill-vs-ablation comparison where a confirmation requires verified provenance and a same-revision canonical hash on both arms.
- **Discovery** ablations run through `run_pi_trigger_eval.py --ablation`, which currently emits a **raw autonomous-trigger measurement for a single arm** (`evidence_class: raw_autonomous_trigger_measurement`), not a paired, provenance-verified baseline-vs-ablation comparison. Each result records a `skill_tree_hash` (baseline = canonical tree; ablation = parent tree) so a future pairing can verify both arms ran the same revision, but until that pairing exists, **read a trigger pass-rate as a measurement, not a confirmed ablation effect.**

## Commands

Full per-command detail — flags, examples, output shapes — lives in
[`docs/commands.md`](docs/commands.md). This is the index; the [core loop](#core-loop)
above is the five commands you need first (`validate`, `prepare`, `benchmark`,
`render-viewer`, and a runner).

**Core loop**

| Command | What it does |
|---|---|
| `skill-benchmark validate` | Check manifest shape, fixture paths, regex, oracle paths, and prompt-leakage. |
| `skill-benchmark prepare` | Emit answer-key-safe task rows per case/variant/run (`--include-ablations` materializes ablated trees). |
| `skill-benchmark grade` | Score saved outputs into per-run rows; emit pending judge tasks. |
| `skill-benchmark benchmark` | Aggregate into variant summaries, paired lift + significance, by-model, cost, and case flags. |
| `skill-benchmark render-viewer` | Static or `--serve`d review page with embedded artifacts and iteration diffs. |

**Runners** (the only model-touching commands)

| Command | What it does |
|---|---|
| `skill-benchmark run-codex` | Drive prepared rows through `codex exec --json`; save trace, events, metrics, answer. |
| `skill-benchmark run-claude` | Drive `claude -p --output-format json`, capturing real per-run cost + token usage. |
| `skill-benchmark run-agent` | Provider-neutral native runner over registered backends (`--agent claude` or `--agent codex`); compatibility wrappers delegate here. |
| `skill-benchmark run-subagent` | In-process backend seam: any provider via `--agent-cmd`, tool replay, multi-turn `turns`. |
| `skill-benchmark import-trace` | Normalize a raw JSONL trace into `events.json`/`metrics.json` for process/efficiency checks. |

**Measurement trust** (model-free unless noted)

| Command | What it does |
|---|---|
| `skill-benchmark audit-manifest` | Readiness verdict + blockers; `--fail-on-blockers` gates CI on "worth paying to run". |
| `skill-benchmark report` | Serialize `benchmark.json` as JUnit XML or GitHub job-summary + annotations. |
| `skill-benchmark contamination` | Output-side perimeter: canary tripwire, output↔answer n-gram overlap, released-at/cutoff gate. |
| `skill-benchmark error-analysis` | Open-coding review queue + axial failure taxonomy over a `benchmark.json`. |
| `skill-benchmark compare-judges` | Flag whether measured lift depends on which judge model graded. |
| `skill-benchmark judge-alignment` | Score a judge against human labels: agreement, Cohen's kappa, precision/recall/F1. |
| `skill-benchmark judge-robustness` | Order-flip self-consistency + negative controls a robust judge must reject (opt-in, model-touching). |
| `skill-benchmark judge` | Run deferred `judge`/`rubric` assertions through `--judge-backend`/`--judge-model` or `--judge-cmd`. |

**Cost and size**

| Command | What it does |
|---|---|
| `skill-benchmark cost-summary` | Suite cost ledger: coverage, totals, by variant/case/runner, top spenders, cost-quality findings. |
| `skill-benchmark token-overhead` | Static footprint vs. runtime lift-per-token and lift-per-dollar. |
| `skill-benchmark profile-skill` | `SKILL.md`/reference token counts, module counts, oversize warnings (static, offline). |

**Scale, trend, iteration**

| Command | What it does |
|---|---|
| `skill-benchmark suite-run` | Allowlisted multi-skill preflight/tier with cost ceilings; writes `RUN_SCOPE.json`. |
| `skill-benchmark aggregate` | Cross-skill report over many manifests. |
| `skill-benchmark trend` | Append-only history: series, diffs, prevalence×severity failure ranking, prune candidates. |
| `skill-benchmark suggest-cases` | Turn saturated/no-lift flags into harder-case seeds (generation opt-in, never edits a manifest). |
| `skill-benchmark migrate` | Upgrade a v1 manifest to v2: stamp severity/oracle tiers, print the judgment-call checklist. |

**Interop and export**

| Command | What it does |
|---|---|
| `skill-benchmark export-anthropic` | Emit an Anthropic-skill-creator-compatible `benchmark.json`. |
| `skill-benchmark compare-tasks` / `skill-benchmark compare-results` | Blind A/B comparison export and scoring. |
| `skill-benchmark export-jetty` / `skill-benchmark run-jetty` / `skill-benchmark import-jetty-results` | Jetty runbook-mode export, execute, and import (optional; see the [Jetty adapter](docs/commands.md#jetty-adapter)). |

**Activation** (separate entry points — does the skill load on its own?)

| Command | What it does |
|---|---|
| `skill-trigger-matrix` | Autonomous trigger rate per (agent × model), split by should-fire / should-not-fire. |
| `skill-pi-trigger-eval` | The deeper Pi-specific trigger tool: discovery-population ablation arms, traces, cost. |

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

- Grading and aggregation do not call a model. Model execution happens outside that path, except for the explicit runner/judge commands that exist to call one: `run-codex`, `run-claude`, `run-agent`, `run-jetty`, and `judge` (via `--judge-cmd` or a native `--judge-backend`).
- The harness does not decide qualitative truth by itself; it emits judge prompts, runs a judge (an opt-in `--judge-cmd`, or a native `--judge-backend` plus `--judge-model`), and merges the returned JSON — recording which backend/model produced each verdict.
- Hidden prompts are not protected if you pass `--include-answer-key` to generation jobs.
- A passing answer benchmark does not prove autonomous skill loading; run `skill-trigger-matrix` (any adapter-backed agent × model) or `skill-pi-trigger-eval` (Pi, with ablation arms) for that.

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
├── run_pi_trigger_eval.py      # autonomous-trigger runner (Pi: ablation arms, traces, cost)
├── run_trigger_matrix.py       # activation matrix across agents × models (claude/pi/stub adapters)
├── ablation_model.py           # typed ablation/provenance value objects
├── docs/                       # architecture, abstractions, vocabulary, specs, guides (see the map above)
├── .github/
│   ├── PULL_REQUEST_TEMPLATE.md
│   ├── ISSUE_TEMPLATE/
│   └── workflows/ci.yml
├── examples/
│   ├── demo-skill/             # offline end-to-end example (stub runner, materialized ablations)
│   ├── skill-pins.json         # pinned SHAs + tree hashes for the ablation study
│   └── adewale-workspace/      # Pi smoke runner + cross-repo aggregate report
└── tests/                      # test_skill_benchmark.py + roadmap/cost/confidence-floor/doc-ref suites
```

## Development

```bash
python3 -m py_compile *.py examples/adewale-workspace/*.py
python3 -m unittest discover tests -v
```

The test suite is organized by subject: manifest validation and eval hygiene (`test_manifest.py`), grading (`test_grading.py`), judge plumbing (`test_judging.py`), report views (`test_reporting.py`), closed-form statistics (`test_stats.py`), runner adapters (`test_runners.py`), the ablation experiment end to end (`test_ablations.py`), cost telemetry (`test_cost_telemetry.py`), the confidence floor and detector fixtures (`test_confidence_floor.py`), the trigger matrix (`test_trigger_matrix.py`), plus three executable drift guards: doc code references (`test_doc_refs.py`), shared-owner/doc-sync consolidation guards (`test_consolidation_guards.py`), and relative-link resolution across the docs (`test_doc_links.py`). Shared fixture builders live in `tests/helpers.py`.

## Source checked

This README was written against:

- `skill_benchmark.py` CLI and assertion implementation
- `run_pi_trigger_eval.py` trigger runner
- `run_trigger_matrix.py` agent×model activation matrix
- `pyproject.toml` package metadata
- `docs/repo-effectiveness-audit.md` for the current `good-repo` audit
- `tests/test_skill_benchmark.py` behavior coverage
- `CHANGELOG.md`, `CONTRIBUTING.md`, and `.github/` contribution/CI surfaces
- `anti-slop-writing/skills/anti-slop-writing/SKILL.md` for the v0.4.1 docs cleanup and consistency pass
- the `good-readme` skill guidance from `https://www.skills.sh/adewale/good-readme/good-readme`
- the `good-repo` skill guidance from `good-repo/skills/good-repo/references/quality-checklist.md`
