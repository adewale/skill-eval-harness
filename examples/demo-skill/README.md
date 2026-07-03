# Demo skill — a self-contained ablation study you can run offline

This is the harness's executable example. It runs end to end with **no model and no
API key**: a deterministic stub stands in for the model, so the whole
prepare → run → report → ablation-confirmation loop is reproducible (and runs in
CI via `tests/test_example_demo.py`).

The skill (`skills/demo/`) has exactly two load-bearing pieces, each targeted by one
**materialized** ablation in `evals/shared-benchmark.json`:

| ablation | mechanism | removes | confirms a regression on |
|---|---|---|---|
| `no-severity` | `section` | the `## Severity rules` section of SKILL.md | the `severity-label` assertion |
| `no-checklist` | `reference` (`remove: content`) | the body of `references/checklist.md` | the `cite-checklist` assertion |

`stub_runner.py` answers by reading the skill that the harness actually mounted into
the isolated workspace, so removing a piece really changes the output — the
regression is genuine, not scripted into the runner.

## Run it

```bash
cd examples/demo-skill
HARNESS=../../skill_benchmark.py

# 1. (optional) dry-run the ablation gates — writes nothing
python3 $HARNESS validate evals/shared-benchmark.json --check-ablations

# 2. prepare the with_skill / without_skill / ablation arms (materializes the ablations)
python3 $HARNESS prepare evals/shared-benchmark.json --split tune \
  --include-ablations --ablation-dir /tmp/demo-abl --out /tmp/demo-tasks.jsonl

# 3. run every arm with the deterministic stub 'model'
python3 $HARNESS run-codex --tasks /tmp/demo-tasks.jsonl --runs /tmp/demo-runs \
  --codex-cmd "python3 $(pwd)/stub_runner.py"

# 4. score + see the ablation_regressions block
python3 $HARNESS benchmark evals/shared-benchmark.json --runs /tmp/demo-runs \
  --variant with_skill --variant without_skill \
  --variant ablation:no-severity --variant ablation:no-checklist
```

You should see `with_skill` pass both assertions, `without_skill` fail both, and each
ablation arm fail exactly the one assertion whose guidance it removed — each reported
as an `expected_regression_confirmed` because the ablation is **materialized** (a real
edited tree, blind, with verified provenance). Swap the stub for a real runner
(`--codex-cmd "claude -p"`, `codex exec`, etc.) to run it against an actual model.

## Measure activation (does the skill load on its own?)

Everything above force-loads the skill, so it says nothing about whether an agent
would *discover* it. The manifest also carries one should-fire and one
should-not-fire `kind: "trigger"` case for that question. Offline first (the stub
'agent' decides from the mounted description, deterministically):

```bash
python3 ../../run_trigger_matrix.py evals/shared-benchmark.json \
  --agent stub --out /tmp/demo-trigger-stub.json
```

Then for real, across Claude Code subagents on haiku, sonnet, and opus (spends
tokens; also wired into the manual smoke test
`RUN_TRIGGER_SMOKE=1 python3 -m unittest tests.test_trigger_matrix -v`):

```bash
python3 ../../run_trigger_matrix.py evals/shared-benchmark.json \
  --agent claude --runs-per-query 3 --out /tmp/demo-trigger-matrix.json
```

The loop for acting on the resulting per-model trigger rates is
[`docs/tuning-skill-activation.md`](../../docs/tuning-skill-activation.md).

## What it teaches

- A **materialized** ablation (real removal) yields a *confirmable* regression; an
  instruction-simulated one (mount the whole skill, tell the model to ignore X) can
  only ever be a raw measurement. `audit-manifest` flags the latter.
- Read `expected_regression_confirmed` (a named assertion flipped, provenance
  verified) as the signal — not a raw aggregate score drop.
- Run `skill-benchmark audit-manifest evals/shared-benchmark.json` on this manifest:
  the **readiness** block reports `ready: no blockers` (ablations materialized, no
  leak-saturated cases). `--fail-on-blockers` turns that into a CI gate — this demo
  is what a ready manifest looks like.
