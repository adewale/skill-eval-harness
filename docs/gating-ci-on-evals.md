# How do I gate my skill repo's CI on this?

You have a skill repo and a benchmark that passes today. The question is how to make CI
*stay* honest — fail a PR that regresses the skill, or that ships a manifest too weak to
catch a regression at all. The instinct is to treat it like a unit test: one green
check, merge on green. But an eval is not a test (see
[evals-are-not-tests.md](evals-are-not-tests.md)) — a single run is a sample, and a
manifest can be green because it is *good* or because it is *too weak to fail*. So a
useful gate has two independent jobs, and they key on different things:

1. **Did the graded outputs regress?** — turn `benchmark.json` into a CI-native
   pass/fail with `report --format junit|github`.
2. **Is the manifest itself strong enough to trust the green?** — `audit-manifest
   --fail-on-blockers` fails when the suite has structural blockers (no adversarial
   coverage, a leak-saturated case, an instruction-simulated ablation masquerading
   as evidence).

Neither calls a model. Both run in the same offline path the demo uses, so your CI
never needs an API key to grade.

## Run both gates offline on the demo

Grade the bundled demo the normal way, then serialize the result for CI. This is the
whole loop, runnable with no key:

```bash
cd examples/demo-skill
HARNESS=../../skill_benchmark.py

# (assumes /tmp/demo-runs exists from the demo README's prepare + run-codex steps)
python3 $HARNESS benchmark evals/shared-benchmark.json --runs /tmp/demo-runs \
  --variant with_skill --variant without_skill --out /tmp/demo-benchmark.json

python3 $HARNESS report --benchmark /tmp/demo-benchmark.json --format github
```

Real output (2026-07-05, Python 3.11):

```text
# Skill eval — demo-reviewer

**Lift (with − without, objective):** 1.00 − 0.00 = **1.00**

| variant | cases | runs | mean objective | mean combined | missing | exec errors |
|---|---|---|---|---|---|---|
| with_skill | 2 | 2 | 1.00 | 1.00 | 0 | 0 |
| without_skill | 2 | 2 | 0.00 | 0.00 | 0 | 0 |
```

`--format github` writes a job-summary table (and annotations) straight into a GitHub
Actions run. `--format junit` writes the same result as JUnit XML, one `<testcase>` per
case/variant/run, which any CI that reads JUnit will render and gate on:

```text
<testsuite name="skill-eval:demo-reviewer" tests="4" failures="2" errors="0" ...>
  <testcase classname="demo-reviewer.c-review" name="with_skill/run-1" />
  <testcase classname="demo-reviewer.c-review" name="without_skill/run-1">
    <failure message="2 failing check(s)">severity-label: none matched: ['Blocking', 'Minor', 'Clean']
cite-checklist: none matched: ['file and line']</failure>
  </testcase>
  <testcase classname="demo-reviewer.c-adversarial" name="with_skill/run-1" />
  <testcase classname="demo-reviewer.c-adversarial" name="without_skill/run-1">
    <failure message="1 failing check(s)">severity-label: none matched: ['Blocking', 'Minor', 'Clean']</failure>
  </testcase>
</testsuite>
```

The `without_skill` failures are *expected* here — that arm exists to prove the skill is
what passes the cases. Which is the first subtlety of gating an eval: you do not gate on
"all testcases green." You gate on the **lift** and on **named regressions**, not on the
raw pass count.

## The second gate: is the manifest strong enough?

A benchmark can be green because the manifest can't fail. `audit-manifest` scores that,
and `--fail-on-blockers` turns it into an exit code:

```bash
python3 $HARNESS audit-manifest evals/shared-benchmark.json --fail-on-blockers
echo "exit=$?"
```

Real output (2026-07-05) — the demo is a *ready* manifest, so it passes:

```text
exit=0
```

with a readiness block reporting:

```json
"readiness": {
  "ablations": { "total": 3, "materialized": 3, "instruction_simulated": 0 },
  "leak_saturated_cases": [],
  "blockers": []
}
```

`--fail-on-blockers` keys on `readiness.blockers` — the structural problems that make a
green meaningless: no adversarial cases (nothing tests whether the skill holds under
pressure), a leak-saturated case (an assertion the base model passes from the prompt
alone), an ablation that is only *instruction-simulated* and so can never confirm a
causal regression, or — once run data is supplied — a base-saturated case. The demo has
none, so it gates clean.

Note the distinction the exit code draws: `audit-manifest` *also* emitted eight
`findings` at `recommended`/`required` severity on this same run (missing domain tags,
missing difficulty tags, …). Those are advice, not blockers — `--fail-on-blockers`
deliberately does **not** fail on them, so your CI fails on "this suite can't be trusted"
without nagging on "this suite could be richer." Add `--strict-judge` to also fail when
the declared judge model is the model under test.

## A workflow that ties it together

The recipe for a skill repo's `.github/workflows/`:

```yaml
- name: Grade skill eval
  run: |
    skill-benchmark benchmark evals/shared-benchmark.json \
      --runs eval-runs/latest --variant with_skill --variant without_skill \
      --out benchmark.json
    skill-benchmark report --benchmark benchmark.json --format github >> "$GITHUB_STEP_SUMMARY"

- name: Fail if the manifest is too weak to trust
  run: skill-benchmark audit-manifest evals/shared-benchmark.json --fail-on-blockers
```

For a full-suite gate across many skills, `suite-run` adds a preflight with cost
ceilings (`--max-estimated-cost-usd`) so a PR job can refuse to start a run that would
blow its budget — the operational half of the same gate.

## Reading a failing gate, symptom by symptom

- **`report` shows lift dropped vs. the last run** → a real regression, or a flaky
  sample. The `benchmark.json` `significance` block (a sign-flip permutation test on the
  paired scores) tells you which. Gate on *confirmed* regressions, not on a one-run dip;
  a case needs ≥4 runs per arm to ever clear significance.
- **`audit-manifest --fail-on-blockers` exits non-zero** → read the `blockers` list. A
  `leak-saturated` blocker means an assertion passes from the prompt alone; a
  no-adversarial blocker means nothing tests the skill under pressure. Fix the
  manifest, not the threshold. (Missing hidden splits surface as a `required`
  *finding*, not a blocker — advice the exit code deliberately does not fail on.)
- **JUnit shows `errors` > 0 (not `failures`)** → runs crashed or timed out. These are
  execution errors, not quality failures; they poison the denominator. Re-run before you
  read the gate.
- **Everything green but lift ≈ 0** → the gate is passing on a saturated suite. The
  benchmark's `saturated`/`no-lift` case flags are the tell; a suite that can't fail
  isn't guarding anything.

## What keeps the gate honest

- **Grading is model-free by construction.** `benchmark`, `report`, and `audit-manifest`
  never call a model or the network (a guard test patches `subprocess`/`urllib` to raise
  in the grade path). Your CI grades deterministically; the only model calls are the
  earlier, explicit runner step that produced the outputs.
- **Gate on lift and named regressions, not raw pass count.** The `without_skill` arm is
  *supposed* to fail. A gate that counts total green would block every honest suite.
- **A green benchmark is not a green skill-loads.** The answer runners force-load the
  skill; passing them says nothing about autonomous activation. If activation matters for
  your gate, add a `skill-trigger-matrix` check — see
  [tuning-skill-activation.md](tuning-skill-activation.md).
- **`--fail-on-blockers` gates trust, not taste.** It fails on structural blockers that
  void the measurement, and stays quiet on `recommended` findings, so the gate means
  "this result is trustworthy," not "this suite is perfect."

## Where this stops

This journey gets a PR to fail on a regression or an untrustworthy manifest. It does not
decide *whether the regression is worth blocking on* — a confirmed drop on a
regression-guard case is a hard stop, but a soft-severity dip may be acceptable. That
judgment lives in the severity tiers you set on each assertion
([authoring-evals.md](authoring-evals.md)); this gate only enforces the tiers you
already chose.
