# Demo skill — a self-contained ablation study you can run offline

This is the harness's executable example. It runs end to end with **no model and no
API key**: a deterministic stub stands in for the model, so the whole
prepare → run → judge → report → ablation-confirmation loop is reproducible (and runs in
CI via `tests/test_example_demo.py`).

The skill (`skills/demo/`) has two answer-path load-bearing pieces, each targeted by one
**materialized** ablation in `evals/shared-benchmark.json`. It also has a
discovery-population ablation for autonomous trigger examples.

| ablation | mechanism | removes | confirms a regression on |
|---|---|---|---|
| `no-severity` | `section` | the `## Severity rules` section of SKILL.md | the `severity-label` assertion |
| `no-checklist` | `reference` (`remove: content`) | the body of `references/checklist.md` | the `cite-checklist` assertion |
| `weaker-description` | `frontmatter_field` | the extra `when_to_use` trigger hints | measured by `skill-trigger-matrix --ablation weaker-description` |

`stub_runner.py` answers by reading the skill that the harness actually mounted into
the isolated workspace, so removing a piece really changes the output — the
regression is genuine, not scripted into the runner.

The `c-review` case also carries one qualitative assertion (`actionable-review`,
`severity: "gate"`), judged offline by `stub_judge.py` — a deterministic judge with a
careful default mode and a `--lenient` rubber-stamp mode that exists to be caught. The
judge-calibration loop over that pair (`judge-robustness`, `judge-alignment`,
`compare-judges`) is [`docs/can-i-trust-my-judge.md`](../../docs/can-i-trust-my-judge.md).

## Run it

```bash
cd examples/demo-skill
HARNESS=../../skill_benchmark.py

# 1. (optional) dry-run the ablation gates — writes nothing
python3 $HARNESS validate evals/shared-benchmark.json --check-ablations

# 2. prepare the with_skill / without_skill / ablation arms (materializes the ablations)
python3 $HARNESS prepare evals/shared-benchmark.json --split tune \
  --include-ablations --ablation-dir /tmp/demo-abl --runs-per-variant 6 \
  --out /tmp/demo-tasks.jsonl

# 3. run every arm with the deterministic stub 'model'
python3 $HARNESS run-codex --tasks /tmp/demo-tasks.jsonl --runs /tmp/demo-runs \
  --codex-cmd "python3 $(pwd)/stub_runner.py"

# 4. judge the one qualitative assertion with the offline stub judge
V="--variant with_skill --variant without_skill \
   --variant ablation:no-severity --variant ablation:no-checklist"
python3 $HARNESS judge evals/shared-benchmark.json --runs /tmp/demo-runs $V \
  --judge-cmd "python3 $(pwd)/stub_judge.py" --out /tmp/demo-judge.jsonl

# 5. score, then read the ablation_regressions block in /tmp/demo-bench.json
python3 $HARNESS benchmark evals/shared-benchmark.json --runs /tmp/demo-runs $V \
  --judge-results /tmp/demo-judge.jsonl --out /tmp/demo-bench.json
```

You should see `with_skill` pass both objective assertions and `without_skill` fail both.
The stub judge passes `actionable-review` on `with_skill` and `ablation:no-checklist` and
fails it on `without_skill` and `ablation:no-severity`. Each ablation arm fails exactly the
one objective assertion whose guidance it removed, and each is reported with
`evidence_class: "confirmed_causal"` and `expected_regression_confirmed: true`, for two
reasons. The ablation is **materialized** (a real edited tree, blind, with verified
provenance), and six unanimous repeats reach the per-case sign-flip test's threshold:
`min_p_value: 0.03125` (2/2^6), under 0.05.

Fewer repeats, or a skipped step 4, give `indeterminate` for the same observed drop, each
with its own note. At four repeats the smallest possible p is 0.125, and the note reads
`regression observed but not significant per case across replicates (min p=0.125); a case
needs >= 6 matched pairs to confirm`. Without step 4 the report is partial and the note
reads `grading evidence is incomplete`, however many repeats you ran, because a declared
grader has not produced its verdicts. Swap the stub for a real runner
(`--codex-cmd "codex exec"`, etc.) to run it against an actual model — for Claude, use `skill-benchmark run-claude` instead, which parses the `claude -p` JSON envelope and captures cost.

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
To exercise the bundled discovery ablation, add
`--ablation weaker-description --trace-runs /tmp/demo-trigger-traces`.

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
