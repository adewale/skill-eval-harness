# How do I gate my skill repo's CI on this?

You have a skill repo and a benchmark that passes today. The question is how to make CI
*stay* honest — fail a PR that regresses the skill, or that ships a manifest too weak to
catch a regression at all. The instinct is to treat it like a unit test: one green
check, merge on green. But an eval is not a test (see
[evals-are-not-tests.md](evals-are-not-tests.md)) — a single run is a sample, and a
manifest can be green because it is *good* or because it is *too weak to fail*. So a
useful gate has two independent jobs, and they key on different things:

1. **Did the graded outputs regress?** — `report --fail-on regressions` exits 1 on a
   *named* regression: a critical veto, a broken regression guard, or negative lift, and
   on incomplete evidence. `--format junit|github` renders the same result for people;
   the rendering alone never fails a job (see [The first gate: fail on what you
   declared](#the-first-gate-fail-on-what-you-declared)).
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

python3 $HARNESS report --benchmark /tmp/demo-benchmark.json --format github \
  --fail-on regressions
echo "exit=$?"
```

The demo's `c-review` case has a judge assertion, so the benchmark needs the judge
verdicts; without `--judge-results` the report reads "Experiment status: incomplete" and
withholds the lift, and the gate fails closed on it (`fail-on: demo-reviewer: experiment
evidence is incomplete (deferred judge verdicts)`, exit 1). Real output (2026-10-03,
Python 3.11, six runs per arm):

```text
# Skill eval — demo-reviewer

**Lift (with − without, objective):** 1.00 − 0.00 = **1.00**

| variant | cases | runs | mean objective | mean combined | missing | exec errors |
|---|---|---|---|---|---|---|
| with_skill | 2 | 12 | 1.00 | 1.00 | 0 | 0 |
| without_skill | 2 | 12 | 0.00 | 0.00 | 0 | 0 |

## Gate (`--fail-on regressions`)

**Passed:** no matching findings
exit=0
```

`--format github` writes a job-summary table (and annotations) straight into a GitHub
Actions run. `--format junit` writes the same result as JUnit XML, one `<testcase>` per
case/model/variant/run, which any CI that reads JUnit will render. The same run,
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
"all testcases green." A CI job that fails on JUnit failures fails this healthy suite and
a broken one alike. The gate is `--fail-on`'s exit code, not the XML, which the flag
leaves unchanged; annotations mark a run without changing any exit code.

## The first gate: fail on what you declared

A pass/fail on the averaged lift is the wrong gate, because the headline mean is what
hides a regression: an edit that breaks one case while another keeps passing still shows
positive lift. `report --fail-on` uses the same gate policy as `audit-manifest --fail-on`,
over three finding kinds that `report` raises from a graded benchmark (the `regressions`
preset names all three):

| kind | raised when | declared by |
|---|---|---|
| `critical-veto` | a `with_skill` run is vetoed by a `severity: "critical"` assertion | the assertion |
| `regression-guard-failing` | a `with_skill` run of an `eval_intent: "regression"` case scores below 1.00 objective | the case |
| `negative-lift` | overall paired lift (with − without, objective) is below zero | — |

The case-level kinds read the `with_skill` arm only. The baseline failing is the point of
the experiment, and an ablation or `old_skill` arm failing is the regression it exists to
measure. They fire even when the lift is positive, which is when a regression hides. Like
every `--fail-on`, the gate fails closed on an incomplete report (deferred judge verdicts,
missing arms, crashed runs), so a critical assertion that never got graded cannot pass by
default. Each reason prints to stderr as `fail-on: <kind>: <message>`; with
`--format github` the summary gains a Gate section and each case-level reason an
`::error`.

Watch it on the demo. Make the careless edit from
[did-my-skill-edit-regress.md](did-my-skill-edit-regress.md) (delete the
`## Severity rules` section from `skills/demo/SKILL.md`), then re-run, re-judge, and
re-grade into a fresh runs directory. The lift falls from 1.00 to 0.25 but stays
positive, and nothing in the stock manifest is declared a guard, so `--fail-on
regressions` **passes**. The case flags saw the damage; nothing was declared
must-not-break, so nothing blocks. Now declare `c-adversarial` a regression guard
(`"eval_intent": "regression"` on the case) and grade the same edit. Real output
(2026-10-03, four runs per arm, trimmed; the `fail-on:` line is stderr):

```text
- `c-adversarial`: floor: fails in both arms; no objective lift; with-skill failure (with=0.00, without=0.00)
- `c-review`: with-skill failure (with=0.50, without=0.00)

## Gate (`--fail-on regressions`)

**Failed:** 1 reason(s)

- regression-guard-failing: demo-reviewer/c-adversarial: with_skill objective pass rate 0.00 (4 of 4 run(s) below 1.00)
…
::error title=skill-eval gate regression-guard-failing::demo-reviewer/c-adversarial: with_skill objective pass rate 0.00 (4 of 4 run(s) below 1.00)
fail-on: regression-guard-failing: demo-reviewer/c-adversarial: with_skill objective pass rate 0.00 (4 of 4 run(s) below 1.00)
exit=1
```

(`git checkout skills/ evals/` restores the demo.) Deciding what goes in the gate means
deciding which cases and assertions you declare. Capability cases are reported, never
gated case by case. A one-run dip on a case the skill is still improving at is noise until
it clears the paired significance gate. Promote a case to `eval_intent: "regression"` once
it must never break, and mark prohibitions `severity: "critical"`
([authoring-evals.md](authoring-evals.md)). `tests/test_example_demo.py` pins all three
demo outcomes.

Before trusting a lift, read two fields of `benchmark.json`. `paired_summary.interval`
says whether this run's lift excludes zero at 95%. `paired_summary.noise_check.verdict`
says whether the eval can resolve a lift of the size you care about (`resolvable`) or
why not (`too-few-cases-moved`, `noise-exceeds-headroom`, and so on; pass `benchmark
--min-lift` to set the size). Neither field drives `report`'s exit code, so a gate that
needs them has to read the JSON itself (`audit-manifest --runs --fail-on
underpowered-eval` gates on the noise check).

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
`contamination`, `judge-robustness`, `regressions`), comma-separated and repeatable, and exits 1 when a
matching finding fires. `--fail-on floor-eval,underpowered-eval` fails a suite whose runs
show a case failing in both arms or noise too wide to resolve the lift; `--fail-on
required` fails on every required finding. An unknown token is an error, not a gate that
never fires, and when `--runs` points at an incomplete benchmark the gate fails closed.
The accepted kinds and each one's default severity are listed in
[`commands.md`](commands.md#finding-kinds).

## A workflow that ties it together

The recipe for a skill repo's `.github/workflows/`:

```yaml
- name: Grade skill eval
  run: |
    set -o pipefail   # the gate's exit code must survive the tee
    skill-benchmark benchmark evals/shared-benchmark.json \
      --runs eval-runs/latest --variant with_skill --variant without_skill \
      --out benchmark.json   # add --judge-results if the manifest has judge assertions
    skill-benchmark report --benchmark benchmark.json --format github \
      --fail-on regressions | tee -a "$GITHUB_STEP_SUMMARY"

- name: Fail if the manifest is too weak to trust
  run: skill-benchmark audit-manifest evals/shared-benchmark.json --fail-on-blockers
```

Tee the report; do not redirect it. GitHub acts on `::error`/`::warning` lines only when
they reach the step's stdout. Lines appended straight to `$GITHUB_STEP_SUMMARY` render as
plain text.

For a full-suite gate across many skills, `suite-run` adds a preflight with cost
ceilings (`--max-estimated-cost-usd`) so a PR job can refuse to start a run that would
blow its budget — the operational half of the same gate.

## Reading a failing gate, symptom by symptom

- **`fail-on: critical-veto: …` or `fail-on: regression-guard-failing: …`** → something
  you declared must-not-break broke in the `with_skill` arm, named by skill, case, and
  model. Open that run's `output.md` ([why-did-this-run-fail.md](why-did-this-run-fail.md)).
  It fails even when the lift headline is green; that is the point.
- **`fail-on: <skill>: experiment evidence is incomplete (…)`** → evidence is missing
  (deferred judge verdicts, missing arms, crashed runs), not a quality verdict. Run the
  judge or re-run the attempts.
- **Lift dropped vs. the last run** → a real regression, or a noisy sample, and the
  `benchmark.json` `significance` block cannot tell you which: it tests this run's
  lift against zero, not against the last run's lift. To compare two versions of the
  skill, run the old one as an `old_skill` arm in the same run (`old_skill_paths` in
  the manifest, `prepare --include-old-skill`), so both versions answer the same cases
  under the same conditions. Grade with all three arms (`--variant with_skill --variant
  without_skill --variant old_skill`; `--variant` replaces the defaults rather than adding to
  them) and the report's `paired_edit_summary` pairs the versions case by case, with
  the same significance test, interval, and noise check as the lift. To test a
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
- **Gate on named regressions, not raw pass count or the headline alone.** The
  `without_skill` arm is *supposed* to fail, so a gate that counts total green would block
  every honest suite. The averaged lift can stay positive while a guard breaks, so
  `--fail-on regressions` reads each declared case in the `with_skill` arm.
- **A green benchmark is not a green skill-loads.** The answer runners force-load the
  skill; passing them says nothing about autonomous activation. If activation matters for
  your gate, add a `skill-trigger-matrix` check — see
  [tuning-skill-activation.md](tuning-skill-activation.md).
- **`--fail-on-blockers` gates trust, not taste.** It fails on structural blockers that
  void the measurement, and stays quiet on `recommended` findings, so the gate means
  "this result is trustworthy," not "this suite is perfect."

## Where this stops

This journey gets a PR to fail on a declared regression or an untrustworthy manifest. It
does not decide *what is worth blocking on*: a critical veto or a broken regression guard
is a hard stop, while a dip on a capability case or a soft-severity assertion is only
reported. That judgment lives in the severities and intents you declare
([authoring-evals.md](authoring-evals.md)); `--fail-on` enforces only what you declared,
and the careless-edit example above passes it until you do. It also gates one report: a
drop relative to the last merged skill is the `old_skill` arm's `paired_edit_summary`
above. `regression-guard-failing` reads objective assertions only, so a judge-only guard
does not raise it; until you have calibrated the judge
([can-i-trust-my-judge.md](can-i-trust-my-judge.md)), keep guards on deterministic checks.
