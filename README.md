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
> uv tool install git+https://github.com/adewale/skill-eval-harness.git@v0.3.0
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
uv tool install git+https://github.com/adewale/skill-eval-harness.git@v0.3.0
skill-benchmark --help
skill-pi-trigger-eval --help

# One-shot without installing globally:
uvx --from git+https://github.com/adewale/skill-eval-harness.git@v0.3.0 skill-benchmark --help
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
| `LESSONS_LEARNED.md` | Design lessons from the multi-skill saturation work. |
| `docs/jetty-support-spec.md` | Jetty payload/import contract and live-token unknowns. |
| `docs/trace-aware-eval-spec.md` | Proposal for trace artifacts, process/efficiency assertions, Codex adapter support, richer reporting, and audit upgrades. |
| `TODO.md` | Remaining Jetty work: streaming, concurrency, live API validation, materialized ablations. |
| `examples/adewale-workspace/` | Adewale-specific runners for Pi smoke, trigger, ablation, and aggregate reports. |
| `tests/test_skill_benchmark.py` | Executable examples for repeated runs, leakage lint, script assertions, judge commands, Jetty export/import, and trigger detection. |

## Manifest format

Each skill repo owns an `evals/shared-benchmark.json` manifest. Add a `harness` block so readers know which external harness/version to install.

```json
{
  "version": 1,
  "skill_name": "good-pr",
  "harness": {
    "name": "skill-eval-harness",
    "url": "https://github.com/adewale/skill-eval-harness",
    "version": ">=0.3.0"
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
| `script` | Opt-in deterministic oracle command against the output directory. |
| `skill_invoked` | Trace/process check that the runner loaded the skill, or did not, as expected. |
| `command_ran` / `command_not_ran` | Trace/process checks over normalized command events. |
| `command_order` | Trace/process check that commands appeared in a required order. |
| `tool_count_le` / `no_repeated_command_loop` | Trace/process budgets for tool use and thrashing. |
| `total_tokens_le` / `elapsed_seconds_le` / `command_count_le` | Efficiency checks over `metrics.json`, `metadata.json`, or normalized events. |

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

Qualitative assertion types:

| Type | Behavior |
|---|---|
| `judge` | Deferred into `judge-tasks.jsonl`; merge results with `--judge-results`. |
| `rubric` | Same deferred qualitative flow. |

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
skill-benchmark prepare ../repo/evals/shared-benchmark.json --include-ablations --out ablation-tasks.jsonl
```

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

Override `--codex-cmd` for local wrappers or tests.

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

`benchmark` aggregates graded rows into variant summaries, paired deltas, slice summaries, telemetry availability, and case flags. Add `--allow-scripts` only when you trust the repo-owned oracle commands in the manifest.

```bash
skill-benchmark benchmark ../repo/evals/shared-benchmark.json \
  --runs ../repo/eval-runs/latest \
  --allow-scripts \
  --judge-results judge-results.jsonl \
  --out benchmark.json
```

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

- missing positive, negative, and adversarial eval coverage,
- missing holdout/holdback split coverage,
- missing trigger/no-trigger coverage,
- missing domain/difficulty/success-goal taxonomy for slice summaries,
- ablation-plan suggestions from major skill sections,
- saturated and no-lift cases when run data is available,
- assertions with identical with/without pass rates, and
- recommended fixture repos/files.

### Profile skill size and references

```bash
skill-benchmark profile-skill ../repo/evals/shared-benchmark.json \
  --format markdown \
  --out skill-profile.md
```

`profile-skill` reports `SKILL.md` token estimates, reference-file counts/sizes, heading/module counts, and warnings for overly broad or oversized skills. These warnings are advisory; focused 2–3-module skills are often easier for agents to apply, but large skills can be justified when references are conditional.

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

### Static viewer

```bash
skill-benchmark render-viewer \
  --benchmark benchmark.json \
  --runs ../repo/eval-runs/latest \
  --out review.html
```

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

Ablations are opt-in variants that simulate removing part of a skill. Add entries under `manifest.ablations`, then prepare with `--include-ablations`.

```bash
skill-benchmark prepare ../repo/evals/shared-benchmark.json \
  --split tune \
  --include-ablations \
  --out ablation-tasks.jsonl
```

Ablation task variants are named `ablation:<id>`. Trigger cases are skipped for ablation tasks because trigger behavior depends on the description/frontmatter rather than the body component being ablated.

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

- Local grading does not call a model. Model execution happens outside the harness, except for explicit runner commands such as `run-jetty`.
- The harness does not decide qualitative truth by itself; it emits judge prompts, runs an opt-in judge command, and merges the returned JSON.
- Hidden prompts are not protected if you pass `--include-answer-key` to generation jobs.
- A passing answer benchmark does not prove autonomous skill loading; run `skill-pi-trigger-eval` for that.

## Repository layout

```text
skill-eval-harness/
├── README.md
├── CHANGELOG.md
├── CONTRIBUTING.md
├── pyproject.toml
├── skill_benchmark.py
├── run_pi_trigger_eval.py
├── .github/
│   ├── PULL_REQUEST_TEMPLATE.md
│   ├── ISSUE_TEMPLATE/
│   └── workflows/ci.yml
├── examples/
│   └── adewale-workspace/
│       ├── all-manifests.txt
│       ├── generate_shared_manifests.py
│       ├── run_pi_smoke.py
│       └── smoke_report.py
└── tests/
    └── test_skill_benchmark.py
```

## Development

```bash
python3 -m py_compile *.py examples/adewale-workspace/*.py
python3 -m unittest discover tests -v
```

The test suite covers repeated runs, artifact outputs, answer-key omission, leakage lint, script assertions, judge-command parsing, Anthropic export shape, Jetty export shape, mocked Jetty execution, and Jetty import round trips.

## Source checked

This README was written against:

- `skill_benchmark.py` CLI and assertion implementation
- `run_pi_trigger_eval.py` trigger runner
- `pyproject.toml` package metadata
- `tests/test_skill_benchmark.py` behavior coverage
- `CHANGELOG.md`, `CONTRIBUTING.md`, and `.github/` contribution/CI surfaces
- `anti-slop-writing/skills/anti-slop-writing/SKILL.md` for this prose pass
- the `good-readme` skill guidance from `https://www.skills.sh/adewale/good-readme/good-readme`
- the `good-repo` skill guidance from `good-repo/skills/good-repo/references/quality-checklist.md`
