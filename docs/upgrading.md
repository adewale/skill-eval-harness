# Upgrading skill-eval-harness

Each section covers one released-version boundary. Follow every section after your installed version; do not skip an intermediate artifact migration.

## 0.5.1 → 0.6.0

Most version-1 and version-2 manifests continue to validate without edits. The
upgrade risk is in saved artifacts and custom adapters: 0.6.0 rejects identities,
verdicts, lifecycle states, and telemetry comparisons that 0.5.1 could accept or
coerce.

### Before installing

Keep the old package and run tree available until a regenerated report has been
reviewed. Telemetry migration is atomic per run directory, but a separate copy gives
users a clean rollback and preserves the exact input behind the 0.5.1 report.

```bash
cp -R eval-runs/latest eval-runs/latest-v0.5.1
cp benchmark.json benchmark-v0.5.1.json

python -m venv .venv-0.6
.venv-0.6/bin/python -m pip install skill-eval-harness==0.6.0
```

A fresh virtual environment keeps the old CLI usable while the new report is checked.

### What remains compatible

- Manifest versions 1 and 2 remain valid. There is no manifest version 3.
- `run-codex` and `run-claude` remain compatibility commands over `run-agent`.
- Legacy token and cost fields remain readable beside the schema-v3 telemetry envelope.
- Schema-v1 trace events and the older single-model run-directory layout remain readable.
- Python 3.10, 3.11, and 3.12 remain supported. The runtime now includes PyYAML plus
  exact-pinned `regex==2026.7.19`; the latter supplies one Unicode semantics and a native
  timeout for every `rendered-v1` regex. `comparison: "exact"` keeps stdlib `re` behavior.

`skill-benchmark migrate` still means **manifest version 1 → 2**. The new
`migrate-telemetry` command upgrades saved `metadata.json` and `metrics.json`; the two
commands solve different migrations.

### Run the upgrade checks

#### 1. Validate the manifest

```bash
.venv-0.6/bin/skill-benchmark validate evals/shared-benchmark.json
```

An unchanged v1/v2 manifest should pass. If it does not, fix the reported field rather
than changing the manifest version.

#### 2. Inspect telemetry migration without writing

```bash
.venv-0.6/bin/skill-benchmark migrate-telemetry \
  --runs eval-runs/latest-v0.5.1 \
  --check \
  --out telemetry-migration-check.json
```

The check reports which run directories would change. It does not alter either
artifact. Legacy numbers are retained as `legacy_unverified`; migration does not invent
a provider, currency conversion, trace, or comparison basis.

Apply the migration to a working copy when the check looks correct:

```bash
cp -R eval-runs/latest-v0.5.1 eval-runs/latest-v0.6
.venv-0.6/bin/skill-benchmark migrate-telemetry \
  --runs eval-runs/latest-v0.6 \
  --out telemetry-migration.json
```

A migrated run has both `metadata.json` and `metrics.json`, each with
`telemetry_schema_version: 3` and the same `telemetry` envelope. Re-running the command
is idempotent.

#### 3. Regenerate reports

Use the migrated copy rather than overwriting the old report:

```bash
.venv-0.6/bin/skill-benchmark benchmark \
  evals/shared-benchmark.json \
  --runs eval-runs/latest-v0.6 \
  --split tune \
  --out benchmark-v0.6.json
```

Compare decisions, eligible sample counts, and blocked reasons. Do not require the two
JSON documents to be structurally identical: 0.6.0 adds telemetry availability and
pairing diagnostics, and some former numeric totals become `null` when the underlying
set is incomplete.

### Inputs that may need repair

#### Prepared task and result identities

Every comparative observation needs one exact identity:

```text
(case_id, model, run_number, population)
```

`run_number` must be a positive integer. Each identity may contain at most one
`with_skill` and one `without_skill` row. Missing or mismatched arms appear as blocked
pairs; duplicate arms are rejected. CLI-generated 0.5.1 task rows normally already
carry `run_number`, but hand-written or post-processed JSONL may not.

Do not repair a mismatch by renumbering one arm until it lines up. Regenerate the
missing observation or leave the pair blocked, because the repetition identity is part
of the measurement.

#### Stored judge verdicts

0.6.0 rejects duplicate task IDs and verdicts whose fields disagree. In particular:

- `passed` must be a JSON boolean;
- numeric fields must be finite;
- a scored verdict needs an explicit threshold;
- `passed` must agree with `score >= threshold`;
- dimension scores must match the declared dimensions and their derived aggregate;
- dynamic criteria need unique names and a feasible `minimum_criteria`.

Deduplicate by `judge_task_id`, then regenerate malformed verdicts with the judge
command. Do not keep whichever duplicate happened to occur last in a file.

#### Trigger eval sets

Each row must be an object with a nonblank string `query` and a JSON boolean
`should_trigger`:

```json
{"query": "Review this pull request before merge.", "should_trigger": true}
```

Strings such as `"false"`, integer `0`, missing fields, and non-list envelopes are now
errors instead of truthy/falsy inputs.

#### Jetty results

A successful Jetty record needs:

- a recognized, non-conflicting success lifecycle;
- a nonblank `trajectory_id`; and
- an `output.md` artifact.

Unknown states, conflicting `status`/`state` fields, duplicate import destinations, and
unsafe run paths fail before import. Normalize a provider-specific state in the adapter;
do not relabel an incomplete trajectory as successful.

#### Custom Codex wrappers

Codex answer runs now use `--output-last-message`, and the harness places `CODEX_HOME`
outside the model workspace. A custom `--codex-cmd` wrapper must accept the arguments the
harness appends. Smoke one prepared task before starting a paid matrix; ambient Codex
rules and user configuration no longer implicitly enter the eval workspace.

### Expected report changes

A changed number is not automatically a regression. Check these intentional semantic
changes first:

- **Missing telemetry stays missing.** A partial set exposes a `known_*` subtotal and
  availability counts instead of a false complete total. Measured zero remains numeric.
- **Comparisons use exact pairs.** Lift, reliability, cost, token, slice, readiness, and
  ablation views exclude missing, mismatched, ineligible, or cross-population arms.
- **Ablation confirmation needs more evidence.** The two-sided paired sign-flip gate
  cannot confirm five informative pairs (`p >= 0.0625`); six unanimous pairs reach
  `p = 0.03125`. Named assertion coverage must also be symmetric across the pair.
- **Failed Pi streams cannot pass as clean negative triggers.** Exit zero does not
  override a provider/protocol failure or a missing terminal event.
- **Trace counts require proven completion.** Started, failed, malformed, and unknown
  lifecycle events no longer count as completed commands or tool calls.

Review the new `pairing`, availability, and blocked-reason fields before deciding that a
skill changed. They often explain a smaller denominator or a missing ratio directly.

### Rollback

Keep `eval-runs/latest-v0.5.1`, `benchmark-v0.5.1.json`, and the old environment until
the 0.6.0 report has been accepted. Rolling back the executable is then just using the
old environment or reinstalling the pinned release:

```bash
uv tool install --force skill-eval-harness==0.5.1
```

Do not convert a migrated tree back by deleting selected telemetry keys. Restore the
saved 0.5.1 tree instead; that preserves the artifact pair exactly as the old report read
it.
