# Skill Eval Harness

Skill Eval Harness is a Python CLI for evaluating Agent Skills with paired variants, hidden splits, repeated runs, deterministic grading, qualitative judge handoff, and benchmark exports. It prepares tasks for any agent runner, normalizes outputs into a simple run directory, then grades and aggregates the results.

Use it when you want to answer: **does this skill improve the agent, where does it fail, and is the eval itself discriminating?**

## What it does

- **Paired variants** — compare `with_skill` vs `without_skill`, plus optional `old_skill` and ablation variants.
- **Anti-overfitting splits** — keep `tune`, `holdout`, and `holdback` cases separate.
- **Answer-key-safe preparation** — generation tasks omit expected behavior/rubrics unless explicitly requested.
- **Repeated-run statistics** — measure mean, stddev, min, max, median, time, and token usage.
- **Objective grading** — run string, regex, file, JSON, and opt-in script assertions locally.
- **Prompt/assertion leakage lint** — warn when literal `contains*` assertion values appear in prompts.
- **Qualitative review handoff** — emit judge tasks, run an opt-in `--judge-cmd` backend, and merge judge results.
- **Blind comparison** — generate A/B comparison tasks and unblind the aggregate winner.
- **Compatibility exports** — write Anthropic-style `grading.json` and `benchmark.json` files.
- **Review UI** — render a static HTML benchmark viewer.
- **Trigger checks** — run Pi skill-trigger smoke evals without forcing `--skill`.
- **Manifest audits** — detect missing eval categories, hidden split gaps, missing ablations, trigger/no-trigger gaps, saturated cases, non-discriminating assertions, and fixture recommendations.
- **Jetty adapter** — export runbook-mode Jetty payloads, run them via Jetty REST, and import trajectory artifacts back into the local run layout.

## Quick start

> Requires Python 3.10+ and [uv](https://docs.astral.sh/uv/). Install from GitHub first:
>
> ```bash
> uv tool install git+https://github.com/adewale/skill-eval-harness.git@v0.3.0
> ```

```bash
# 1. Validate a skill manifest
skill-benchmark validate evals/shared-benchmark.json

# 2. Prepare paired tasks for an agent runner
skill-benchmark prepare evals/shared-benchmark.json \
  --split tune \
  --runs-per-variant 3 \
  --out /tmp/tasks.jsonl

# 3. Run each JSONL task with your agent runner and save outputs as:
# runs/<case_id>/<variant>/run-<n>/output.md
# runs/<case_id>/<variant>/run-<n>/metadata.json

# 4. Grade and benchmark saved outputs
skill-benchmark benchmark evals/shared-benchmark.json \
  --runs eval-runs/latest \
  --split tune \
  --out benchmark.json

# 5. Open a static review page
skill-benchmark render-viewer \
  --benchmark benchmark.json \
  --runs eval-runs/latest \
  --out review.html
```

`benchmark.json` reports pass rates, repeated-run variance, timing/token summaries, and flags for saturated, no-lift, flaky, or with-skill-failed cases.

## Installation

### From GitHub

```bash
uv tool install git+https://github.com/adewale/skill-eval-harness.git@v0.3.0
skill-benchmark --help
skill-pi-trigger-eval --help

# One-shot without installing globally:
uvx --from git+https://github.com/adewale/skill-eval-harness.git@v0.3.0 skill-benchmark --help
```

### Local development

```bash
git clone https://github.com/adewale/skill-eval-harness.git
cd skill-eval-harness
uv tool install --editable .
skill-benchmark --help
```

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

Script assertions are blocked unless you pass `--allow-scripts` to `grade`, `benchmark`, `aggregate`, or `export-anthropic`:

```json
{
  "name": "oracle-pass",
  "type": "script",
  "command": ["python3", "oracles/oracle.py", "{output_dir}"],
  "pass_exit_code": 0,
  "timeout_s": 30
}
```

`command` runs with cwd set to the manifest directory. `{output_dir}` is replaced with the absolute run directory.

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

### Prepare tasks

```bash
skill-benchmark prepare ../repo/evals/shared-benchmark.json --split tune --out tasks.jsonl
skill-benchmark prepare ../repo/evals/shared-benchmark.json --runs-per-variant 5 --out tasks.jsonl
skill-benchmark prepare ../repo/evals/shared-benchmark.json --include-ablations --out ablation-tasks.jsonl
```

Use `--include-answer-key` only for judge/debug tasks, never for generation runs.

### Grade

```bash
skill-benchmark grade ../repo/evals/shared-benchmark.json \
  --runs ../repo/eval-runs/latest \
  --out grade-report.json \
  --judge-tasks judge-tasks.jsonl
```

Execute repo-owned script assertions explicitly:

```bash
skill-benchmark benchmark ../repo/evals/shared-benchmark.json \
  --runs ../repo/eval-runs/latest \
  --allow-scripts \
  --out benchmark.json
```

Write Anthropic-compatible per-run grading files:

```bash
skill-benchmark grade ../repo/evals/shared-benchmark.json \
  --runs ../repo/eval-runs/latest \
  --write-grading-files
```

### Benchmark

```bash
skill-benchmark benchmark ../repo/evals/shared-benchmark.json \
  --runs ../repo/eval-runs/latest \
  --judge-results judge-results.jsonl \
  --out benchmark.json
```

### Judge command backend

Run deferred `judge`/`rubric` assertions with any command that reads a prompt from stdin and emits JSON on stdout:

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

The judge command should return JSON like `{"passed": true, "score": 4, "rationale": "..."}`. Bare or fenced JSON is accepted using `json.raw_decode` scanning rather than brace counting.

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
- ablation-plan suggestions from major skill sections,
- saturated and no-lift cases when run data is available,
- assertions with identical with/without pass rates, and
- recommended fixture repos/files.

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

Jetty support is an optional execution adapter. The harness still owns manifests and grading; Jetty runs the task and returns trajectory artifacts.

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

Defaults follow Jetty docs and `jettyio/jettyio-skills`: `claude-code`, `claude-sonnet-4-6`, `model_provider=anthropic`, `snapshot=python312-uv`, runbook content in the system message, runtime values in `jetty.template_variables`, and uploaded files in `jetty.file_paths`. Use `JETTY_BASE_URL` to override `https://flows-api.jetty.io`.

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
- **Jetty**: use `export-jetty`, `run-jetty`, and `import-jetty-results` for REST runbook-mode execution. Live API response shapes still need token-backed smoke validation before claiming broad Jetty production coverage.

## Non-goals

- Local grading does not call a model. Execution stays runner-agnostic except for explicit adapter commands such as `run-jetty`.
- It does not replace human/LLM qualitative review; it emits judge tasks and merges results.
- It does not make hidden prompts safe if you pass `--include-answer-key` to generation jobs.
- It does not guarantee a skill triggers autonomously unless you run trigger evals.

## Repository layout

```text
skill-eval-harness/
├── README.md
├── pyproject.toml
├── skill_benchmark.py
├── run_pi_trigger_eval.py
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
python3 -m py_compile *.py
python3 -m unittest discover tests -v
```

The test suite covers repeated runs, artifact outputs, judge-result merging, answer-key omission, Anthropic export shape, Jetty export shape, mocked Jetty execution, and Jetty import round trips.

## Source checked

This README was written against:

- `skill_benchmark.py` CLI and assertion implementation
- `run_pi_trigger_eval.py` trigger runner
- `pyproject.toml` package metadata
- `tests/test_skill_benchmark.py` behavior coverage
- the `good-readme` skill guidance from `https://www.skills.sh/adewale/good-readme/good-readme`
