# Evals Are Not Tests

This is the **interpretation lens** on the terms in [`vocabulary.md`](vocabulary.md): how to read the numbers they name. A unit test and a skill eval both end in pass or fail, which is why they get confused. The machinery underneath is different, and treating an eval like a test produces confident, wrong conclusions. This page explains the difference using the things this harness actually measures.

The short version: a test asks whether one program is correct; an eval asks whether a change moved a model's behavior, and by how much, across many cases and repeated runs. Almost everything that follows is a consequence of that gap.

## A test scores one program; an eval scores a difference

A unit test runs one program and checks its output against a known answer. The program is the subject, and its pass is the whole result.

An eval runs the *same* case twice — once `with_skill`, once `without_skill` — and the result that matters is the gap between them. The harness calls that gap **lift**. A `with_skill` run that passes tells you almost nothing on its own: the baseline might pass too. That is why the harness pairs variants and reports paired deltas rather than a single column of pass rates. If you read only the `with_skill` column, you are reading an eval as if it were a test, and you will credit the skill for behavior the model already had.

## A passing test is the goal; a passing eval can mean the eval is too easy

In a test suite, green is success. Make every test pass and ship.

In an eval, green across both arms is a warning. The harness flags it two ways. **Saturated** means every `with_skill` run passes — pleasant, but weak evidence of lift. **No-lift** means `with_skill` and `without_skill` pass at the same rate, so the case measured nothing about the skill. A strong model saturates easy cases the way a strong student aces a placement test: the score is real and tells you little. The response is not to celebrate green; it is to add harder fixtures or artifact-level checks until the baseline starts to fail. A test you can always pass is finished. An eval you can always pass is broken.

## A test is deterministic; an eval is a sample

The same input gives a test the same output every time, so one run is a verdict. A flaky test is a bug to fix.

Model output varies between runs, so one run of an eval is a sample, not a verdict. The harness repeats runs (`run-1`, `run-2`, …) and flags **flaky** when they disagree. Flakiness here is data about the case and the model, not a defect to suppress. This is also why a run that never happened is marked `missing_output` and excluded from comparisons: "not measured" and "measured and failed" are different facts, and collapsing them invents fake no-lift cases.

## A test has one correct answer; an eval accepts equivalent good behavior

`assertEqual(x, 5)` admits exactly one value. That precision is the point of a test.

An eval grades open-ended output, where several phrasings are equally correct. An assertion that demanded `Decision: BLOCK` once failed a correct answer that wrote `**Decision: BLOCK.**`. The fix was not to loosen the standard but to assert the *behavior* — a semantically meaningful regex or `contains` variant — without calibrating away genuine misses. Objective assertions still grade deterministically and locally with no model call; the judgment is in choosing checks that track the property instead of one spelling of it. When string matching cannot reach the property, the work moves to a `script` oracle or a deferred `judge` assertion.

## A test checks output; an eval also checks process

A test inspects what a function returned. How it got there is invisible and usually irrelevant.

For a skill, the path is part of the claim. Did the agent actually load the skill, or answer from general knowledge? Did it stay inside a tool or token budget? Final output cannot answer this, so the harness reads **trace artifacts** — `events.json` and `metrics.json` — for process assertions like `skill_invoked`, `command_order`, and `total_tokens_le`. These fail closed when the evidence is missing, because inferring "the skill loaded" from confident prose is exactly the mistake that lets a baseline masquerade as a skilled run. A skill that lifts pass rates while doubling token cost is a different verdict from one that lifts them for free, and **token overhead** reports lift per 1k extra tokens so you can see which one you have.

## A test cannot leak its answer; an eval can

A test holds the expected value in code the program under test never reads. There is no leak to worry about.

An eval puts its prompt in front of the model, and if an assertion's keyword sits in that prompt, a weak answer passes by echoing the task. The harness has a **leakage** lint for exactly this, and `--strict-leakage` turns the warning into a failure once you have replaced the weak check with a scoped regex, a fixture-backed check, an oracle, or a judge. A green run from a leaked assertion looks identical to a green run from real skill behavior, which is why leakage is a property of the eval's design, not of the model.

## A test does not overfit; an eval can be memorized

Run a test a thousand times and it measures the same thing. There is nothing to overfit, because the answer was never hidden.

An eval that stays visible while you tune against it stops measuring capability and starts measuring fit to that specific case. The **splits** exist to counter this: `tune` cases are for iteration, `holdout` is scored at end-of-round, and `holdback` stays out of the skill, docs, and eval descriptions until after scoring, so a suspiciously high score reveals memorization instead of hiding it. Tune saturation is not release proof; that claim waits on hidden prompts, private answer keys, and real fixtures being filled and scored.

## What carries over

Evals keep the test-suite habits worth keeping: deterministic local grading, fixtures checked into the repo, fast feedback, and assertions specific enough to mean something. The harness leans on all of them.

The trap is importing the test-suite *mindset* — that green is the goal, that one run is a verdict, that the answer is fixed, that output is the whole story. An eval is a measurement instrument pointed at a stochastic system, and its job is to produce an honest difference between a world with the skill and a world without it. Read the [vocabulary](vocabulary.md) next for the precise terms, or the [README](../README.md) for the commands that compute these signals.
