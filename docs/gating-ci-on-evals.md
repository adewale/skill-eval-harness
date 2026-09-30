# How do I gate my skill repo's CI on this?

You have a skill repo and a benchmark that passes today. The question is how to make CI
*stay* honest — fail a PR that regresses the skill, or that ships a manifest too weak to
catch a regression at all. The instinct is to treat it like a unit test: one green
check, merge on green. But an eval is not a test (see
[evals-are-not-tests.md](evals-are-not-tests.md)) — a single run is a sample, and a
manifest can be green because it is *good* or because it is *too weak to fail*. So a
useful gate has two independent jobs, and they key on different things:

1. **Did the graded outputs regress?** — `report --format junit|github` turns
   `benchmark.json` into CI-readable output. It reports but does not gate: the
   command exits 0 on every benchmark (see [What report can and cannot
   fail](#what-report-can-and-cannot-fail)).
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

# (assumes /tmp/demo-runs and /tmp/demo-judge.jsonl exist from the demo README's
# prepare, run-codex and judge steps)
python3 $HARNESS benchmark evals/shared-benchmark.json --runs /tmp/demo-runs \
  --variant with_skill --variant without_skill \
  --judge-results /tmp/demo-judge.jsonl --out /tmp/demo-benchmark.json

python3 $HARNESS report --benchmark /tmp/demo-benchmark.json --format github
```

The demo's `c-review` case has a judge assertion, so the benchmark needs the judge
verdicts; without `--judge-results` the report reads "Experiment status: incomplete" and
withholds the lift. Real output (2026-09-30, Python 3.11, six runs per arm):

```text
# Skill eval — demo-reviewer

**Lift (with − without, objective):** 1.00 − 0.00 = **1.00**

| variant | cases | runs | mean objective | mean combined | missing | exec errors |
|---|---|---|---|---|---|---|
| with_skill | 2 | 12 | 1.00 | 1.00 | 0 | 0 |
| without_skill | 2 | 12 | 0.00 | 0.00 | 0 | 0 |
```

`--format github` writes a job-summary table (and annotations) straight into a GitHub
Actions run. `--format junit` writes the same result as JUnit XML, one `<testcase>` per
case/variant/run, which any CI that reads JUnit will render and gate on. The same run,
reformatted and trimmed to `run-1` of each arm:

```text
<testsuite name="skill-eval:demo-reviewer" tests="24" failures="12" errors="0" ...>
  <testcase classname="demo-reviewer.c-review.default-model" name="default-model/with_skill/run-1" ... />
  <testcase classname="demo-reviewer.c-review.default-model" name="default-model/without_skill/run-1" ...>
    <failure message="3 failing check(s)">severity-label: none matched: ['Blocking', 'Minor', 'Clean']
cite-checklist: none matched: ['file and line']
actionable-review: no justification for the finding, or the concrete gap (the missing test) is never named</failure>
  </testcase>
  <testcase classname="demo-reviewer.c-adversarial.default-model" name="default-model/with_skill/run-1" ... />
  <testcase classname="demo-reviewer.c-adversarial.default-model" name="default-model/without_skill/run-1" ...>
    <failure message="1 failing check(s)">severity-label: none matched: ['Blocking', 'Minor', 'Clean']</failure>
  </testcase>
</testsuite>
```

The `without_skill` failures are *expected* here — that arm exists to prove the skill is
what passes the cases. Which is the first subtlety of gating an eval: you do not gate on
"all testcases green." What you want to gate on is the **lift** and **named
regressions**, not the raw pass count, and `report` gates on neither.

## What report can and cannot fail

`report` exits 0 whatever the benchmark says, so the CI step that runs it never fails a
PR. With `--format github` it prints `::warning` annotations for flagged cases and
`::error` annotations for a negative overall lift or incomplete evidence; annotations
mark the run without changing the step's exit code, and they act only when printed to
stdout, so the recipe below, which appends the output to `$GITHUB_STEP_SUMMARY`, shows
them as plain text. With `--format junit`, your CI's JUnit reader decides, and it counts
every `<failure>`, including the expected `without_skill` misses above. A job that fails
on JUnit failures therefore blocks every honest suite. A `report --fail-on` option that
exits non-zero on named conditions exists on the unmerged branch
`claude/twitter-thread-analysis-nrvg2d`; it is not on main.

Before trusting a lift, read two fields of `benchmark.json`. `paired_summary.interval`
says whether this run's lift excludes zero at 95%. `paired_summary.noise_check.verdict`
says whether the eval can resolve a lift of the size you care about (`resolvable`) or
why not (`too-few-cases-moved`, `noise-exceeds-headroom`, and so on; pass `benchmark
--min-lift` to set the size). Neither field drives an exit code, so a gate that needs
them has to read the JSON itself.

## The second gate: is the manifest strong enough?

A benchmark can be green because the manifest can't fail. `audit-manifest` scores that,
and `--fail-on-blockers` turns it into an exit code:

```bash
python3 $HARNESS audit-manifest evals/shared-benchmark.json --fail-on-blockers
echo "exit=$?"
```

Real output (2026-09-30) — the demo is a *ready* manifest, so it passes:

```text
exit=0
```

with a readiness block reporting (trimmed):

```json
"readiness": {
  "ablations": { "total": 3, "materialized": 3, "instruction_simulated": 0 },
  "leak_saturated_cases": [],
  "blockers": [],
  "blocker_findings": []
}
```

`--fail-on-blockers` keys on the readiness blockers, the structural problems that make a
green meaningless. They are typed findings, and which kinds block (including the
`benchmark-incomplete` blocker for a partial `--runs` benchmark) is the **Readiness**
entry in [`vocabulary.md`](vocabulary.md#report-signals). The demo has none, so it gates
clean.

Note the distinction the exit code draws: `audit-manifest` *also* emitted nine
`findings` at `recommended`/`required` severity on this same run (missing domain tags,
missing difficulty tags, …). Those are advice, not blockers — `--fail-on-blockers`
deliberately does **not** fail on them, so your CI fails on "this suite can't be trusted"
without nagging on "this suite could be richer." Add `--strict-judge` to also fail when
the declared judge model is the model under test.

When you do want a finding to fail the build, name it. `--fail-on` takes finding kinds,
severities (`required`, `recommended`) or preset names (`blockers`, `strict-judge`,
`contamination`, `judge-robustness`), comma-separated and repeatable, and exits 1 when a
matching finding fires. `--fail-on floor-eval,underpowered-eval` fails a suite whose runs
show a case failing in both arms or noise too wide to resolve the lift; `--fail-on
required` fails on every required finding. An unknown token is an error, not a gate that
never fires, and when `--runs` points at an incomplete benchmark the gate fails closed.
The accepted kinds and each one's default severity are listed in
[`commands.md`](commands.md#audit-manifest-quality).

## A workflow that ties it together

The recipe for a skill repo's `.github/workflows/`:

```yaml
# report exits 0: this step records lift and flags but never fails on a regression
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

- **Lift dropped vs. the last run** → a real regression, or a noisy sample, and the
  `benchmark.json` `significance` block cannot tell you which: it tests this run's
  lift against zero, not against the last run's lift. To compare two versions of the
  skill, run the old one as an `old_skill` arm in the same run (`old_skill_paths` in
  the manifest, `prepare --include-old-skill`), so both versions answer the same cases
  under the same conditions and sit side by side in the report's per-variant
  `summary`; the harness runs no significance test between those two arms. To test a
  named component, read `ablation_regressions`, which compares each ablation arm with
  `with_skill` and confirms an expected regression only when a named assertion flips.
  Gate on *confirmed* regressions, not on a one-run dip; an ablation cohort needs at
  least 6 matched repetition pairs to clear its sign-flip gate (see **Inference unit** in
  [`vocabulary.md`](vocabulary.md#report-signals)).
- **`audit-manifest --fail-on-blockers` exits non-zero** → read the `blockers` list. A
  `leak-saturated` blocker means an assertion passes from the prompt alone; a
  no-adversarial blocker means nothing tests the skill under pressure; a
  `benchmark-incomplete` blocker means the runs behind `--runs` were not fully graded
  (for a suite with judge assertions, pass `--judge-results`). Fix the
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

This journey gets a PR to fail on an untrustworthy manifest and to report lift and case
flags on every run. Failing a PR on a regression takes a check of your own on
`benchmark.json`, because `report --fail-on` is not merged. Whichever check you use does
not decide *whether the regression is worth blocking on* — a confirmed drop on a
regression-guard case is a hard stop, but a soft-severity dip may be acceptable. That
judgment lives in the severity tiers you set on each assertion
([authoring-evals.md](authoring-evals.md)); this gate only enforces the tiers you
already chose.
