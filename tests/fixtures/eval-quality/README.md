# Eval-quality review fixtures (real recorded outputs)

`repo/` is the bundled demo skill with a one-case manifest, `c-review-verdict`. Its prompt
asks for a review "ending with a one-line verdict" but never says how to spell it, while
its two checks demand one exact form: `severity-exact` (`contains "Severity: Blocking"`,
case-sensitive) and `verdict-line` (`^Verdict: (APPROVE|REQUEST CHANGES)`). That is a
deliberately format-strict verifier, the kind the benchmark's `verifier_review` exists to
find.

`recorded/` holds the unedited `output.md` of the twelve real runs of that case (two
models, both arms, three runs each) from Claude Code 2.1.269 on 2026-09-23, run with
`run-claude --max-cost-usd 2.00` as part of a 36-run suite that cost $1.54. Only
`metadata.json` is reduced to `{model, provider}`. On these outputs:

| Finding | What the models wrote |
|---|---|
| `severity-exact` / `format_near_miss` | Sonnet run 2 wrote `Severity: **Blocking**`; bold markers alone failed the check |
| `verdict-line` / `never_passes` (12 of 12) | Every model wrote a verdict like `Verdict: Blocking`; the check wants a vocabulary the prompt never gave |
| `severity-exact` model-order inversion, with skill, p = 0.05 | Haiku writes `**Severity: Blocking**` (contains the exact string) 3 of 3; Sonnet phrases it three other ways, 0 of 3 |

`tests/test_eval_quality_review.py` rebuilds the benchmark report from these files and
asserts all three findings, so the signals stay proven on real model output rather than
on synthetic strings.
