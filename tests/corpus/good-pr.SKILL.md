---
name: good-pr
description: >
  Help users prepare and review pull requests that maintainers actually want to
  merge. Use this skill whenever someone asks for help writing a PR description,
  preparing a contribution to an open-source project, reviewing their own PR
  before submitting, writing a bug fix PR, drafting a feature PR, or asks
  anything like "how do I make a good pull request", "review my PR", or "why do
  my PRs keep getting rejected". Also trigger when a user shares a diff or patch
  and wants help presenting it, when they mention contributing to a repo they
  don't maintain, when they're preparing their first contribution to a project,
  or when they want a pre-submission sanity check. Even if the user doesn't
  explicitly mention "PR" — if they're packaging a code change for someone else
  to review, this skill applies.
compatibility: Agent Skills clients including Codex, OpenCode, Pi, Gemini CLI, and Claude Code.
---

# Good PR

A skill for crafting pull requests that respect maintainers' time and earn
trust. Every piece of guidance here comes from inverting real maintainer
frustrations — these are the things that separate contributions that get merged
from ones that get closed.

## Why This Matters

Maintainers have to *own* every line of code that gets merged. If your PR
introduces a subtle bug and you disappear for a month (which is completely
fine — OSS contributors have lives), the maintainers are the ones debugging it
at 2am. A PR that looks "80% done" can still be a net negative if the remaining
20% is a landmine.

Your job as a contributor is to make the maintainer's life easier, not harder.
That means doing the work to *prove* your change is correct — not just asserting
it. The checklist below is how you do that.

## Quick Navigation

What does the user need help with?

```
├─ Writing a PR description     -> references/pr-template.md (blank template)
│                                  references/pr-example.md (filled-in example)
├─ Self-reviewing before submit -> references/review-checklist.md
├─ Automated readiness check    -> scripts/check-pr-readiness.sh
├─ Understanding what makes
│  a good PR                    -> The Checklist (below)
└─ Common mistakes to avoid     -> Anti-Patterns (below)
```

## The Checklist

Walk through each area when preparing a PR. Not every PR needs every item, but
every item should be consciously considered.

### 1. Reproduction Steps

A maintainer who can't reproduce a bug from your description will close the PR —
not because they're dismissive, but because they literally cannot verify the fix.

The PR should reference a specific issue. If none exists, create one. Include:

- Environment details (OS, runtime version, browser, etc.)
- Exact steps to trigger the bug
- What currently happens (broken behavior)
- What should happen instead

**Example:**

```
## Steps to reproduce
1. Run `npm start` with Node 20.11
2. Navigate to /settings/profile
3. Click "Save" without changing any fields
4. Observe: 500 error in console — expected: no-op or "no changes" toast
```

If you can't reproduce the bug reliably, you can't prove your fix works.

### 2. Visual Evidence

Any PR that touches UI must include before/after screenshots or recordings.
Asking maintainers to pull your branch and click around is asking them to do
your job.

- **Screenshots** for static changes (layout, styling, text)
- **Screen recordings or GIFs** for interactive or animated changes
- Multiple viewport sizes if the change is responsive
- Cover relevant states: empty, loading, error, populated

This is the single easiest thing to include and the most common thing people
skip. It takes 30 seconds and saves the reviewer minutes of context-building.

Make the evidence trustworthy, not just present:

- **Caption each before/after pair** with the specific defect it demonstrates
  ("label no longer overlaps the container border") — the image should confirm
  a claim, not make the reviewer play spot-the-difference
- **Prefer generated artifacts over hand-taken screenshots** when the project
  renders output programmatically (diagramming, charting, PDF/image pipelines):
  render the "before" from the base commit and the "after" from your branch, so
  there's no doubt the evidence matches the code in the PR
- **Include the regeneration command** (e.g. `npm run render-examples`) so a
  skeptical reviewer can rebuild the evidence instead of trusting attachments

For ordinary UI work a hand-taken screenshot is fine — don't build a render
pipeline just to fix a button color.

### 3. Code That Fits

Study the codebase before contributing. A PR that "works" but requires a full
refactor before merging is one the maintainer will rewrite from scratch. Match
the project's patterns:

- Naming conventions, file structure, abstraction style
- Error handling patterns already in use
- No new dependencies without prior discussion
- No unrelated refactoring mixed into the PR
- Minimal diff — solve exactly the problem described

If you think the codebase *should* be structured differently, open a discussion
first. Don't smuggle an architecture change into a bug fix.

### 4. Tests That Prove the Fix

This is where contributions most often quietly fail. A test suite that passes
is not evidence of correctness — your tests must **fail when the bug is
reintroduced**. This is the litmus test:

1. Write the test
2. Confirm it passes with your fix
3. Revert your fix temporarily
4. Confirm the test **fails**
5. Re-apply the fix

If a test passes both with and without the fix, it tests nothing. Maintainers
*will* check this — they'll reintroduce the bug to see if your test catches it.
If it doesn't, the PR loses all credibility.

For security, authorization, data-exposure, or permission bugs, make the
evidence especially concrete without turning it into checklist theater. Ask for
one targeted negative regression test that asserts the security invariant and
would fail on the vulnerable or reverted code. Good evidence usually includes:

- The denied path: `401`/`403`, thrown auth error, or blocked side effect
- No protected data leaked and no unauthorized state change performed
- A positive control showing the authorized path still works
- A short PR testing note with the command run and "verified this test fails
  when the fix is reverted" — or a brief reason if that check is not practical

Do **not** ask for broad security theater: no full exploit write-up,
screenshots, or unrelated suite expansion unless the project asks for it. Keep
the ask scoped to evidence the maintainer can review quickly.

Mention this verification in the PR description:

```
## Testing
- Added unit test for empty-form submission in `settings.test.ts`
- Verified test fails when the guard clause on line 42 is removed
- Existing test suite passes (no regressions)
```

### 5. Scoped and Safe

A PR that claims to fix one thing but touches many things is a red flag. Even
if each individual change is correct, the combined risk multiplies.

- Keep PRs small and focused on a single concern
- Run the *full* test suite, not just your new tests
- Note areas of risk in the description ("this changes the auth middleware,
  so I also manually tested login and signup flows")
- If your fix touches shared code, explain why and what you verified

Don't self-assign emergency labels or demand rollbacks unless you have clear
evidence of widespread breakage or the project asks contributors to flag
severity. For security fixes, use a neutral, specific title, state concrete
impact/evidence, and let maintainers choose urgency and triage. Panic PRs that
break more than they fix destroy trust faster than anything.

### 6. A Description That Stands Alone

The PR description is your pitch. A maintainer should understand *everything*
about this change without pulling the code.

See `references/pr-template.md` for a fill-in template. The key sections:

- **What** — one-sentence summary
- **Why** — link to issue, brief root cause explanation
- **How** — approach taken, alternatives considered
- **Testing** — what you tested, how, and evidence it works
- **Risk** — what could go wrong and how you mitigated it

### 7. Trust Is Earned

If this is your first contribution to a project:

- Start small — fix a typo, improve docs, tackle a "good first issue"
- Be responsive to review feedback (don't ghost after submitting)
- Accept that maintainers may rewrite parts of your code
- Don't argue style if the project has established conventions

Maintainers remember reliable contributors. A few solid small PRs buy you
latitude for bigger changes later.

## Anti-Patterns

These are real mistakes that get PRs closed. Each shows the wrong way and the
right way — learn from the contrast.

### Bad vs. Good PR Description

```markdown
# NEVER — vague, no context, no evidence

Fix login bug

Changed the form to use onSubmit instead of onClick.
```

```markdown
# ALWAYS — specific, linked, proven

Fix crash on empty form submission (#247)

## What
Fix 500 error when saving settings with no changes.

## Why
Fixes #247. `updateSettings()` sends an empty PATCH body that the API
rejects. ~300 errors/day in Sentry since 2.4.0.

## How
Added early return in `handleSubmit()` when no fields changed.

## Testing
- Added test in `settings.test.ts`
- Verified test fails when guard clause is removed
- Full suite passes (247/247)
```

### Bad vs. Good Tests

```js
// NEVER — passes with or without the fix (tests nothing)
test('should handle invalid tokens', () => {
  const result = validateToken('bad-token');
  expect(result).toBeDefined();
});
```

```js
// ALWAYS — fails when the fix is reverted (proves the fix)
test('should reject expired tokens with TokenExpiredError', () => {
  const expired = createToken({ exp: Date.now() - 1000 });
  expect(() => validateToken(expired)).toThrow(TokenExpiredError);
});
```

### Bad vs. Good Scope

```diff
# NEVER — "drive-by refactor" mixed into a bug fix
- const x = getData()
+ const userData = getUserData()  // renamed for clarity
+ // also reformatted this whole file with prettier
+ // also upgraded lodash while I was in here
```

```diff
# ALWAYS — minimal diff, solves exactly one problem
- <div className="login-form">
+ <form className="login-form" onSubmit={handleSubmit}>
```

## How to Use This Skill

When a user asks for help with a PR:

1. **Understand the context** — ask what project this is for, whether they've
   contributed before, and what the change does
2. **Review the diff** — if they share code, check it against section 3 (code
   fit) and section 5 (scope). Flag unrelated changes or pattern mismatches
3. **Check for gaps** — walk through the checklist and flag what's missing.
   Use `references/review-checklist.md` as a quick-scan tool
4. **Help write the description** — use `references/pr-template.md` as a
   starting point and fill it in together
5. **Probe the tests** — if they have tests, ask: "does this test fail when
   you revert the fix?" If they haven't checked, tell them to check
6. **Visual changes?** — remind them about before/after evidence; if the
   project renders output programmatically, suggest generated artifacts with
   captions and a regeneration command
7. **Be honest** — it's better to tell someone their PR needs work before they
   submit it than to let them waste a maintainer's time and damage their
   reputation in the project
