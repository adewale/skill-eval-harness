# Skill Eval Harness

[![CI](https://github.com/adewale/skill-eval-harness/actions/workflows/ci.yml/badge.svg)](https://github.com/adewale/skill-eval-harness/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

Skill Eval Harness is a Python CLI that measures the **causal lift** of an Agent Skill: it runs the same case, model, and repetition with and without the skill, validates that exact experimental identity, then reports what changed, what passed, and whether the eval leaked its own answer. It reads `evals/shared-benchmark.json`, emits answer-key-safe task rows, grades files under `eval-runs/` locally and deterministically — no model call in the grade path — and writes benchmark reports you can diff across variants.

General eval frameworks (openai/evals, vitest-evals, viteval) score one output against a rubric. This one measures the *difference the skill makes*, and spends its surface area on keeping that difference honest: paired with/without comparison, `tune`/`holdout`/`holdback` split discipline, leakage lint, materialized ablations with provenance gates, and per-model lift. None of those frameworks have them, and they are what make a reported number trustworthy rather than merely green.

## Questions this helps answer

| Question | Command/report to use |
|---|---|
| Does this skill improve outputs compared with no skill at all? | `prepare` paired `with_skill` / `without_skill` rows, then `benchmark` paired lift and significance. |
| Which prompts improved, regressed, saturated, or showed no lift? | `benchmark` `case_flags`, `render-viewer`, and `error-analysis`. |
| Is the skill worth its extra tokens or dollars? | `profile-skill`, `token-overhead`, `cost-summary`, and lift-per-dollar summaries. |
| Did my latest skill edit introduce a regression? | Re-run the same manifest, inspect `ablation_regressions`, `trend`, and `render-viewer --previous-workspace`. |
| Which instruction, checklist, reference, script, or asset is load-bearing? | Materialized `ablation:<id>` arms plus declared `expected_regressions`. |
| Does the agent discover/load the skill when it should, and stay quiet when it should not? | `skill-trigger-matrix` or `skill-pi-trigger-eval`, split by should-fire / should-not-fire cases. |
| Which model tier should this skill target? | `prepare --models`, then `benchmark` `by_model` and `model_analysis`. |
| Is this eval safe to spend model budget on? | `validate --strict-leakage --leakage-min-chars 1 --check-ablations` and `audit-manifest --fail-on-blockers`. |
| Can I trust this LLM judge or rubric result? | `judge`, `compare-judges`, `judge-robustness`, and `judge-alignment`. |
| Could the eval be contaminated by leaked answer keys or memorized canaries? | Prompt leakage lint plus `contamination` over generated outputs. |
| Can this become a CI gate? | `suite-run`, `report --format junit|github`, and readiness blockers from `audit-manifest`. |

## Core loop

1. **Describe cases** in `evals/shared-benchmark.json`: prompt, split, fixture files, variants, assertions, and ablations.
2. **Prepare tasks** with `skill-benchmark prepare`; generation rows omit `expected_behavior` and judge rubrics unless you explicitly request them.
3. **Run tasks** with Claude, Codex, Mistral Vibe, Jetty, or any runner that writes the run-output contract; Pi support is currently trigger-focused plus workspace-specific smoke tooling.
4. **Grade outputs** with deterministic assertions: string, regex, file, JSON field, and opt-in `script` oracles.
5. **Inspect the report** for pass rates, flaky repeated runs, no-lift cases, saturated assertions, judge tasks, and trigger/no-trigger results.

## What the CLI owns

- Causal lift: exact `(case, model, repetition, population)` `with_skill` vs `without_skill` pairs (plus optional `old_skill` and `ablation:<id>`), with blocked-pair diagnostics, paired significance, and per-model lift.
- Split discipline: `tune`, `holdout`, and `holdback` are explicit filters/report labels. The CLI prevents accidental all-split mixing; private `prompt_ref` storage and when to run hidden splits remain user-owned policy.
- Local grading: deterministic assertions run without model calls.
- Eval hygiene: leakage lint, manifest audit, trigger checks, repeated-run stats, and fixture recommendations.
- Activation: does the skill load on its own? `skill-trigger-matrix` reports autonomous trigger rates per (agent × model), split by should-fire / should-not-fire.
- Cost as a signal: normalized token/dollar telemetry per run, a suite cost ledger, and lift-per-dollar (`cost-summary`, `token-overhead`).
- Interop: Anthropic-style exports, static/served HTML review pages, and Jetty runbook-mode import/export.
- Judge plumbing: `judge`/`rubric` assertions can be exported or run through native Claude/Codex/Vibe backends (`--judge-backend`) or a user-supplied `--judge-cmd`; the harness does not choose a model for you.

## Contents

- [Questions this helps answer](#questions-this-helps-answer)
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

> Requires Python 3.10+ and [uv](https://docs.astral.sh/uv/). Install from PyPI:
>
> ```bash
> uv tool install skill-eval-harness
> ```
>
> For exact reproducibility, pin the current release: `uv tool install skill-eval-harness==0.6.0`.

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

`benchmark.json` records one row per case/variant/run, plus aggregate pass rates, timing/token summaries, and flags for saturated, no-lift, flaky, or with-skill-failed cases. It also carries a `reliability` block — unbiased **pass@k** and **pass^k** per (case, variant) from the repeated runs — beside the paired lift's sign-flip `significance`, and a `trajectory_diff` block: per case, over validated experimental pairs, the commands exclusive to one arm across the case's complete repetition set, event-count deltas (steps/commands/tool calls/file reads/file writes), and per-arm skill-load rates — how the arms *behaved*, beside whether they passed. An arm without non-empty, readable trace evidence blocks its pair with a named reason instead of reading as an empty diff.

## Installation

### From PyPI

```bash
uv tool install skill-eval-harness
skill-benchmark --help
skill-pi-trigger-eval --help

# Pin a release exactly:
uv tool install skill-eval-harness==0.6.0

# One-shot without installing globally:
uvx --from skill-eval-harness skill-benchmark --help
```

### From GitHub

Use this for development snapshots before the next PyPI release:

```bash
uv tool install git+https://github.com/adewale/skill-eval-harness.git@main
uvx --from git+https://github.com/adewale/skill-eval-harness.git@main skill-benchmark --help
```

Upgrading a saved run tree requires more than changing the package pin. Follow the
relevant release boundary in [`docs/upgrading.md`](docs/upgrading.md) before regenerating
reports; manifest migration and telemetry migration are separate commands.

The installed commands are:

| Command | What it does |
|---|---|
| `skill-benchmark` | Validate manifests, prepare tasks, grade outputs, compare variants, run judges, and import/export runner formats. |
| `skill-pi-trigger-eval` | Runs Pi without forced `--skill` and checks whether the model loads the skill from stream events. |
| `skill-trigger-matrix` | Measures autonomous skill activation per (agent, model) cell — Claude, Codex, Pi, Vibe, and an offline stub are built in; additional agents add an adapter implementation and one unified backend-registry row. |

### Local development

```bash
git clone https://github.com/adewale/skill-eval-harness.git
cd skill-eval-harness
uv tool install --editable .
skill-benchmark --help
```

## Documentation map

[`docs/README.md`](docs/README.md) groups these by kind (user journeys, concepts, reference, specs, audits) and holds the convention for adding a new user-journey walkthrough.

| File | Use it for |
|---|---|
| `README.md` | Manifest shape, run layout, and the command index. |
| `docs/README.md` | The docs index: journeys/concepts/reference/specs grouping and the convention for adding a user-journey walkthrough. |
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
| `docs/can-i-trust-my-judge.md` | Calibrating a judge before believing its numbers: `judge-robustness` (order-flip + negative controls), `judge-alignment` (human labels, Cohen's kappa, precision/recall), and `compare-judges` (does the lift survive a judge swap?). |
| `docs/eval-framework-roadmap-spec.md` | The implemented eval-framework roadmap: goals, abstractions, and tests per feature (CF.1–CF.4, buckets 1–4, migration). |
| `docs/migrating-evals.md` | Upgrading a manifest between versions (v1 → v2): what `migrate` stamps and the judgment calls it leaves. |
| `docs/upgrading.md` | Version-by-version harness upgrades: saved-run backup, artifact migration, strict input repairs, expected report changes, and rollback. |
| `docs/porting-existing-evals.md` | Arriving from another framework: `dataset_files` + a template case carry the rows across, then the paired baseline, splits, leakage lint, and the `audit-manifest` punch list supply what the old suite had no slot for. |
| `docs/vocabulary.md` | Glossary of harness terms: variants, splits, models, ablations, assertions, severity/oracle tiers, graded scoring, cost telemetry, trace artifacts, agent/judge backends, judge calibration, reliability, contamination, and report flags. |
| `docs/evals-are-not-tests.md` | Why a skill eval is not a unit test, and what that changes about reading results. |
| `docs/academic-grounding.md` | The research constructs behind the harness's terms, with citations; meshes the workflow, measurement, and theory layers. |
| `docs/jetty-support-spec.md` | Jetty payload/import contract and live-token unknowns. |
| `docs/trace-aware-eval-spec.md` | Trace artifact contract, shipped v0.4.1 runner support, process/efficiency assertions, and remaining trace work. |
| `docs/telemetry-availability-and-comparability-spec.md` | Implemented schema-v3 contract for measured-zero, unavailable, partial, and blocked telemetry/comparisons, including legacy migration. |
| `docs/agent-backend-interface-spec.md` | Draft spec for turning Claude/Codex/Gemini/Vibe support into a shared agent backend interface: parity matrix, judge backends, trigger adapters, telemetry, and tool replay. |
| `docs/agent-cli-control-plane.md` | The shared native-CLI control plane: process invocation, config isolation, tool policy, final-answer channels, schemas, telemetry, where Claude/Codex/Vibe intentionally differ, and the cheap comprehensive live-smoke command. |
| `docs/agent-cli-tradeoffs.md` | Claude/Codex/Vibe trade-offs: which CLI surfaces are strong or weak, Vibe-only gaps, and what missing schema/telemetry/prompt controls mean for eval reports. |
| `docs/agent-parity.md` | The per-agent support matrix: which answer/judge/trigger surfaces Claude, Codex, Vibe, Pi, Jetty, subagent, and the stub each cover, with live-smoke status per backend. |
| `docs/skill-ablation-spec.md` | Design spec for materialized (real, altered skill file) ablations: the three-layer model, manifest schema, removal mechanisms, gates, and phased plan. |
| `docs/ablation-study-walkthrough.md` + `examples/skill-pins.json` | A worked ablation study across ten real skills, pinned to exact commit SHAs (+ canonical tree hashes) so it reproduces against the evaluated versions **without vendoring** any skill content. Includes the replication lesson (2 of 3 single-shot findings refuted at n=5). |
| `docs/repo-effectiveness-audit.md` | `good-repo` audit, score, package metadata fixes, and manual GitHub settings checklist. |
| `docs/correctness-by-construction-audit.md` | The closed trigger, experimental-pair, answer-outcome, judge-verdict, prepared-task, Jetty, trace, human-text comparison, and ablation-provenance constructions, their proof tests, and residual risks. |
| `TODO.md` | Status tracker: the eval-framework roadmap (implemented, bar two `(TODO-native)` items), the remaining Jetty adapter work (streaming/concurrency, live API validation, judge export, per-variant overrides, the `swap:<id>` ablation follow-on), the agent-backend parity follow-ups (Gemini CLI open; Vibe done), and the migration/user-journey doc backlog. |
| `examples/demo-skill/` | Self-contained, **offline** end-to-end example: a tiny synthetic skill, two answer-path materialized ablations, one discovery ablation for trigger examples, and a deterministic stub runner (no model/API). `prepare → run-codex → benchmark` confirms a regression per answer-path ablation; exercised by `tests/test_example_demo.py`. Also carries should-fire/should-not-fire trigger cases for `skill-trigger-matrix` (offline via `--agent stub`; live smoke via `RUN_TRIGGER_SMOKE=1`). Start here. |
| `examples/adewale-workspace/` | Adewale-specific Pi smoke runner and cross-repo aggregate report (the trigger runners are the top-level `skill-pi-trigger-eval` and `skill-trigger-matrix`). |
| `scripts/smoke_supported_clis.py` | Opt-in, low-cost smoke across native Claude/Codex/Vibe answer paths and Pi trigger path using a disposable demo-skill eval. |
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
    "version": ">=0.6.0"
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
| `tool_call` | A tool call matching `tool`/`pattern` occurred (with `min_count`/`max_count` bounds), or an ordered `order` list of calls. BFCL-style set relations over completed-call **tool names** (exact, case-insensitive — *not* substring): `expected_no_call` (the named tool, or any name matching `pattern`, must never have been observed—even as started, failed, or in progress), `required_calls` (an order-independent subset of completed tool names that must all appear, extras allowed), `call_set` (an exact multiset of completed tool names—same names and multiplicities, no unexpected named calls). Use `pattern`/`order`/`command_ran` for regex or command-text matching. Positive selectors match completed call inputs, never outputs. |
| `tool_count_le` / `no_repeated_command_loop` | Trace/process budgets for tool use and thrashing. |
| `total_tokens_le` / `elapsed_seconds_le` / `command_count_le` | Efficiency checks over `metrics.json`, `metadata.json`, or normalized events. |

Assertion objects are closed contracts, including nested `graded_dimensions` and
`dynamic_rubric` objects: unknown fields and fields that do not apply to the selected assertion
type are validation errors, so a misspelled severity or path cannot silently change the grader.
A `golden_output` reference must already be a regular file. A local script oracle must live in a
dedicated subdirectory (for example `oracles/check.py`); the harness binds that oracle tree into
the eval-contract digest and rejects symlinks. Changing an imported helper or data file therefore
invalidates stale prepared runs, while generated files beside the manifest cannot make the
contract self-referential.

Human-readable answer assertions (`contains`, `contains_any`, `contains_all`,
`excludes_any`, `regex`, `not_regex`, and `similarity`) compare through the
versioned `rendered-v1` view by default. The raw `output.md` is never rewritten:
the comparison view applies NFC canonical normalization and removes only a
narrow allow-list of zero-width, non-ordering controls: `U+200B ZERO WIDTH
SPACE`, `U+2060 WORD JOINER`, and `U+FEFF ZERO WIDTH NO-BREAK SPACE`. Controls
that can change visible glyph order (including bidi overrides/isolates) and
`U+00AD SOFT HYPHEN` remain exact. Case-insensitive literal and ratio comparisons
use Unicode case-folding.
Results record `comparison: "rendered-v1"`; when normalization changes the input
they also record the affected code points and, for deterministic matchers,
whether it changed the verdict. Embedding similarity records that field as
`null` because determining the raw verdict would require a second external
embedding call. Similarity scores are rounded to four decimals before either
`threshold` or `atLeast` derives the verdict, so a published score cannot
contradict `passed`. Embedding vectors must contain finite, non-boolean numbers;
negative cosine values map to the 0.0 floor of the public 0-1 score domain. Use
`"comparison": "exact"` when an assertion deliberately tests formatting
characters (and `"ci": false` when case must also remain exact). A rendered operand may not become empty, and regex
source must already be NFC/control-stable so normalization cannot create an
empty regex branch. Every `rendered-v1` regex verdict uses exact-pinned
`regex==2026.7.19` in `VERSION0` compatibility mode under a 0.25-second native
operation deadline; inserting a removable control therefore cannot switch regex
engines or Unicode character-class semantics. When normalization changes the
candidate, its normalized verdict and optional raw diagnostic share that deadline.
Timeout or resource exhaustion produces partial/unavailable evidence rather
than a positive or negative verdict. This path is in-process, works in worker
threads, and owns no process signal or timer. Because that engine targets
CPython for non-ASCII text, `rendered-v1` regex evaluation is unavailable on
PyPy; `comparison: "exact"` retains the existing stdlib `re` behavior.
Negative assertions use the same view, so invisible
characters cannot hide banned content. `golden_output`, structured JSON,
scripts, commands, tool names, and paths retain their exact/protocol semantics.

Every assertion may declare a **severity** — `critical` (an absorbing barrier: one failure vetoes the run, every rate collapses to 0.0 and the graded score is withheld), `gate` (lowers the pass rate; the default for objective types), or `soft` (feeds only the graded score channel — a soft failure never moves the objective, qualitative, or combined pass rates; the default for judge/similarity). Declare `severity: "gate"` on a judge assertion to keep it in the qualitative/combined rate. `--strict` on `grade`/`benchmark` promotes soft to gate. An `atLeast` floor on a plain scored judge requires a normalized 0–1 score and decides its pass; on `graded_dimensions` it tightens the normalized form of the dimension threshold. Missing score evidence remains unavailable rather than becoming a failure. Dynamic and per-step judges use `minimum_criteria` and `min_met_fraction` respectively instead of `atLeast`. Every assertion may also declare an **oracle tier** — `strong` (deterministic, the default for text/process/efficiency), `demo` (the default for `script`), or `live` (judge) — reported per case as `oracle_strength` and audited (`weak-oracle-only`).

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
| `judge` | Deferred as a keyed judge task; `grade --judge-tasks` can serialize the queue, and `--judge-results` merges verdicts. |
| `rubric` | Same deferred, keyed qualitative flow. |
| `factuality` | Preset: a judge assertion carrying a canned anchored factuality rubric (threshold 4). `preset: "factuality"` on a judge assertion does the same. |

A judge assertion may carry **anchored graded dimensions** (`graded_dimensions: [{name, scale: "1-5", rubric: "5 = …observable…; 1 = …"}]` — the judge returns `dimension_scores`, normalized to 0-1, passing at `threshold` ≥ 4 by default), a **dynamic rubric** (`dynamic_rubric: {instruction, minimum_criteria}` — the judge drafts case-specific criteria and must meet the minimum), or a **per-step trajectory rubric** (`per_step: true`, or `per_step: {min_met_fraction: f}`). A per-step judge grades EACH completed trajectory step — one criterion per step, named step-1..step-N in trajectory order, with the untruncated invocation and result records resolved separately from `trace.jsonl` beside the normalized summaries — and passes when at least `ceil(f × steps)` steps are judged sound (default: every step). It is trace-evidence-backed and fails closed like a process assertion: a run with no completed steps fails the assertion at grade time and no judge task (no model spend) is emitted. Stored verdicts carry a hash of the exact step payload and are re-queued if the trajectory, criterion names, or derived minimum changes. Per-step assertions are case-level only; turn assertions do not have independent trace artifacts. A case may set a reference floor (`reference_score` 0-1 or `reference_graded_score` 1-5); scoring below it flags `below-reference-floor`. Paired reports carry a sign-flip permutation `significance` block beside every lift, and a `graded` channel when graded scores exist.

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
runs/<case_id>/<variant>/run-1/artifact-commit.json # required-file SHA-256 inventory, written last by current runners
runs/answer-design.json                          # exact expected answer experiment and eval-contract digest
```

Current answer and Jetty writers record independent process, provider-response, trace,
and artifact-set evidence. Tool/command/file/retry/skill measurements are available only
when the first three channels are complete; readers derive artifact completeness by
verifying `artifact-commit.json`. Legacy directories without a marker remain readable but
cannot acquire committed-artifact provenance. Current runners also attest every run to
`answer-design.json`; reports with missing, extra, duplicated, or stale task identities remain
partial and expose any surviving calculations only under explicitly labelled observed fields.

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

The materialized tree flows through the runners: the Pi smoke runner mounts it (answer-population only), the autonomous-trigger runners (`skill-trigger-matrix --ablation <id>` with any registered adapter, or `run_pi_trigger_eval.py --ablation <id>`) trigger-test a discovery (e.g. weakened-description) skill, and `export-jetty --include-ablations --ablation-dir DIR` uploads it recursively. A discovery ablation graduates from raw measurement to a causal evidence class through `skill-benchmark trigger-compare`, which pairs the baseline and `--ablation` matrix reports of the same revision under the same provenance/coverage/significance gate the answer path uses. `prepare`/`export-jetty` emit only **answer-population** ablation rows (on non-trigger cases); discovery ablations are measured by the autonomous-trigger runners. The benchmark report's `ablation_regressions` block separates an aggregate "score regressed" from an assertion-level "expected regression confirmed", and only confirms when recorded provenance proves both arms ran the same skill revision **and** the replicated regression clears a significance test (a two-sided paired sign-flip test run **per (case, model)** over exact repetition-level deltas; a regression is significant iff at least one confirmed cohort clears p≤0.05). Because the exact test discretizes, a cohort needs **≥6 matched pairs** to ever reach significance (`2/2^6=0.03125`; five pairs floor at `0.0625`); fewer pairs are reported `INDETERMINATE`, never confirmed. See [`docs/skill-ablation-spec.md`](docs/skill-ablation-spec.md) for the mechanism table, the component-class model, and the correctness gates.

**Evidence paths (discovery vs answer).** A single runner report and a paired comparison have deliberately different evidentiary strength:

- **Answer-population** ablations get *confirmed* causal evidence: a provenance-gated, paired with_skill-vs-ablation comparison where a confirmation requires verified provenance and a same-revision canonical hash on both arms.
- **Discovery** runners emit a **raw autonomous-trigger measurement for one arm** (`evidence_class: raw_autonomous_trigger_measurement`). Each report records its declared agent/model/query design and every `(query_id, run_number)` repetition; `skill_tree_hash` names the bytes actually mounted (canonical in the baseline arm, edited in the ablation arm), while ablation provenance records the canonical parent. `skill-benchmark trigger-compare` validates exact cardinality, rejects duplicates and missing cells, verifies same-revision provenance, and only then computes a causal verdict. Until two reports pass that gate, **read a trigger pass-rate as a measurement, not a confirmed ablation effect.**

## Commands

Full per-command detail — flags, examples, output shapes — lives in
[`docs/commands.md`](docs/commands.md). This is the index; the [core loop](#core-loop)
above is the five commands you need first (`validate`, `prepare`, `benchmark`,
`render-viewer`, and a runner).

**Core loop**

| Command | What it does |
|---|---|
| `skill-benchmark agent-capabilities` | List the unified backend registry, capability gates, trace dialect, smoke policy, and answer/trigger/judge surfaces as JSON. |
| `skill-benchmark validate` | Check manifest shape, fixture paths, regex, oracle paths, and prompt-leakage. |
| `skill-benchmark prepare` | Emit answer-key-safe task rows per case/variant/run (`--include-ablations` materializes ablated trees). |
| `skill-benchmark materialize-ablations` | Write the declared ablated skill trees to disk without preparing tasks — inspect or diff an ablation before spending a run on it. |
| `skill-benchmark grade` | Score saved outputs into per-run rows; emit pending judge tasks. |
| `skill-benchmark benchmark` | Aggregate into variant summaries, paired lift + significance, by-model, cost, and case flags. |
| `skill-benchmark render-viewer` | Static or `--serve`d review page with embedded artifacts and iteration diffs. |

**Runners** (the only model-touching commands)

| Command | What it does |
|---|---|
| `skill-benchmark run-codex` | Drive prepared rows through isolated `codex exec --json --output-last-message`; save trace, events, metrics, answer. |
| `skill-benchmark run-claude` | Drive `claude -p --output-format stream-json`, capturing real per-run cost + token usage AND the full tool-use stream as the run's trace (`trace.jsonl`/`events.json`), so process assertions have evidence on Claude answer runs. |
| `skill-benchmark run-agent` | Provider-neutral native runner over registered backends (`--agent claude`, `--agent codex`, or `--agent vibe`); compatibility wrappers delegate here. |
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
| `skill-benchmark cost-summary` | Suite cost ledger: complete/partial/unavailable totals, coverage, by variant/case/runner, top spenders, cost-quality findings. |
| `skill-benchmark migrate-telemetry` | Dry-run or atomically upgrade saved run artifacts to the availability-aware telemetry v3 envelope. |
| `skill-benchmark token-overhead` | Static footprint vs. runtime lift-per-token and lift-per-dollar, with blocked reasons for incompatible pairs. |
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
| `skill-benchmark trigger-compare` | Pair baseline and `--ablation` trigger reports of the same skill revision: declared-cell/repetition completeness, duplicate rejection, agent/model cells collapsed by stable authored-query ID, direction-aware sign-flip significance, and a causal-confirmation evidence class. |

## Compatibility notes

- **Anthropic skill-creator**: use `grade --write-grading-files` and `export-anthropic` for compatible `grading.json`/`benchmark.json` shapes.
- **Pi**: use `examples/adewale-workspace/run_pi_smoke.py` for the Adewale multi-repo smoke workflow and `skill-pi-trigger-eval` for autonomous trigger checks.
- **Mistral Vibe**: use `run-agent --agent vibe`, `judge --judge-backend vibe`, and `skill-trigger-matrix --agent vibe`. The harness isolates `VIBE_HOME`, passes `--model` as `VIBE_ACTIVE_MODEL`, mounts trigger skills under `.agents/skills`, and requires `MISTRAL_API_KEY` (or a copied `.env` from the current `VIBE_HOME`, falling back to `~/.vibe/.env`) for live runs.
- **Other runners**: use `prepare` JSONL as the import format and write results back to the run output contract.
- **Jetty**: use `export-jetty`, `run-jetty`, and `import-jetty-results` for REST runbook-mode execution. Live runs require `--out`, keep an exclusively owned atomic attempt journal, and resume acknowledged trajectories after interruption or a local polling deadline; an uncertain submission is blocked unless the operator explicitly accepts duplicate-spend risk with `--resubmit-unknown`. Response shapes were validated against production `flows-api.jetty.io` on 2026-07-17 (captured fixtures in `tests/fixtures/jetty/`); re-verify anytime with the opt-in live smoke — `RUN_JETTY_SMOKE=1 JETTY_API_TOKEN=... JETTY_SMOKE_COLLECTION=<your-collection> python3 -m unittest discover tests -k smoke_jetty` (five real sandbox runs, never in default CI).

## Contributing

See [`CONTRIBUTING.md`](CONTRIBUTING.md) for local setup, validation commands, and eval-safety rules. The short version:

```bash
pip install -e ".[test]"
python3 -m py_compile *.py examples/adewale-workspace/*.py
ty check
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
├── run_trigger_matrix.py       # activation matrix across agents × models (claude/codex/pi/vibe/stub adapters)
├── ablation_model.py           # typed ablation/provenance/task value objects
├── agent_capabilities.py       # unified backend surfaces, capabilities, CLI options, smoke, and failure policy
├── experimental_pairs.py       # exact pair identities and blocked-pair construction
├── runner_contracts.py         # closed answer-runner outcome union
├── judge_verdict.py            # strict imported/stored judge verdict variants
├── jetty_contracts.py          # closed Jetty lifecycle and observation contract
├── trace_contracts.py          # normalized event lifecycle contract
├── trigger_contracts.py        # autonomous-trigger invocation/detection/observation contract
├── telemetry.py                # schema-v3 availability/provenance/comparison domain
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
pip install -e ".[test]"
python3 -m py_compile *.py examples/adewale-workspace/*.py
ty check
python3 -m unittest discover tests -v
```

The test suite is organized by subject: manifest validation and eval hygiene (`test_manifest.py`), grading (`test_grading.py`), human-text construction and matching (`test_text_contracts.py`), judge plumbing (`test_judging.py`), report views (`test_reporting.py`), closed-form statistics and pair identity (`test_stats.py`, `test_experimental_pairs.py`), runner/Jetty adapters and lifecycle contracts (`test_runners.py`, `test_jetty_contracts.py`), the ablation experiment end to end (`test_ablations.py`), cost telemetry (`test_cost_telemetry.py`), the confidence floor and detector fixtures (`test_confidence_floor.py`), the trigger matrix (`test_trigger_matrix.py`), plus three executable drift guards: doc code references (`test_doc_refs.py`), shared-owner/doc-sync consolidation guards (`test_consolidation_guards.py`), and relative-link resolution across the docs (`test_doc_links.py`). Shared fixture builders live in `tests/helpers.py`.

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
