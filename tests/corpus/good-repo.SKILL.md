---
name: good-repo
description: >
  Audit and configure the public GitHub repository surface: launch readiness,
  repo quality/adoption/trust, discoverability, contribution readiness, topics,
  homepage URL, description, issues/wiki/license/CI settings, owner/org-wide repo
  audits, repo popularity/adoption signals, README/package/GitHub metadata drift,
  URL/license/topics gaps, and Agent Skill repo packaging/evals. Use when the user
  asks for repo-level readiness, repo metadata/configuration, or visitor/contributor
  trust. Do not trigger for README-only writing, one PR descriptions/reviews,
  function-level code tests, or general implementation/architecture work; defer
  those to narrower specialists unless the user explicitly asks for repo-level
  readiness/proof/metadata judgment.
license: MIT
compatibility: Agent Skills clients including Codex, OpenCode, Pi, Gemini CLI, and Claude Code.
references:
  - references/hallmark-exemplar.md
  - references/good-pr-exemplar.md
  - references/repo-anatomy.md
  - references/github-official-baseline.md
  - references/community-profile-repolinter.md
  - references/popularity-signals.md
  - references/skill-repo-best-practices.md
  - references/quality-checklist.md
  - references/github-settings.md
  - references/anti-patterns.md
metadata:
  author: adewale
  version: "0.1.0"
---

# good-repo

`good-repo` is a repository effectiveness skill. It treats a GitHub repo as a product surface that must help visitors answer:

1. **What is this?**
2. **Why should I care?**
3. **Can I trust it?**
4. **Can I try it quickly?**
5. **Can I contribute or maintain it?**

## Quality and success feature model

A repo's quality is not measured by stars first. Stars are an outcome. Audit the features that make the right visitor understand, trust, try, use, and contribute:

1. **Clear front door** — GitHub description + README quickly explain what, who, why, and how to try.
2. **Proof that it works** — screenshots, demo, terminal output, runnable examples, evals, tests, notebooks, or sample responses fit the project class.
3. **Fast adoption path** — prerequisites, install, quick start, expected output, local dev path, package metadata.
4. **Accurate documentation** — README and docs match current code, links work, examples are real.
5. **Trust signals** — license, changelog/releases, roadmap/status, security/support where relevant, honest limits.
6. **Repo architecture** — files/folders match the project class and route depth cleanly.
7. **Automation and validation** — CI, tests, docs checks, evals, release validation prove the repo's promises.
8. **Contribution readiness** — contributing guidance, PR/issue templates, test commands, maintainer expectations.
9. **Discoverability** — GitHub description, homepage URL, topics, social preview, package keywords.
10. **Focus and positioning** — clear scope, non-goals, audience, and when not to use it.

When auditing, always include GitHub's own baseline surfaces: README, Community Profile files, description, homepage, topics, license, contribution guidance, security policy when relevant, CI/workflows, and large-file/repo-size risks. Load [`references/github-official-baseline.md`](references/github-official-baseline.md) when the user asks what GitHub itself recommends or when a finding should be grounded in official GitHub docs. Load [`references/community-profile-repolinter.md`](references/community-profile-repolinter.md) when using GitHub Community Profile, GitHub's community health percentage/API, or Repolinter-style policy checks.

When the user asks about repo popularity, stars, forks, growth, or adoption impact, load [`references/popularity-signals.md`](references/popularity-signals.md). Treat stars/forks/watchers as noisy outcomes, not intrinsic quality. State correlations carefully and avoid claiming that one repo hygiene change will cause popularity.

Always include the **homepage URL configuration check**: if the README, manifest, or docs expose a likely demo/docs/homepage URL, verify GitHub's repository `homepageUrl` is set to that URL. If it is empty or points elsewhere, flag it as a discoverability/configuration issue.

The reference exemplars are:

- [`nutlope/hallmark`](https://github.com/nutlope/hallmark) — productized skill repo with clear front door, proof gallery, live demo, install path, packaged skill, modular references, generation tests, roadmap, and license. Load [`references/hallmark-exemplar.md`](references/hallmark-exemplar.md) when explaining or applying the Hallmark pattern.
- [`adewale/good-pr`](https://github.com/adewale/good-pr) — small operational skill repo with real-pain origin, trigger-rich skill description, PR templates, self-review checklist, readiness script, and evals. Load [`references/good-pr-exemplar.md`](references/good-pr-exemplar.md) when explaining or applying the good-pr pattern.

This skill complements [`good-readme`](https://github.com/adewale/good-readme). When README creation/improvement is substantial, use `good-readme` if available; otherwise apply the README gate embedded in this skill.

---

## Verbs

| Invocation | What it does |
| --- | --- |
| *(default)* | Audit current repo, infer project class, score effectiveness, and propose fixes. |
| `good-repo audit [target]` | Read-only audit. Target can be current repo, local path, or public GitHub URL. No edits. |
| `good-repo configure` | Apply safe, non-destructive repo improvements after showing the file/settings plan. |
| `good-repo launch` | Prepare repo for public launch: README handoff, metadata, license, proof, quick start, examples, CI, contribution flow. |
| `good-repo maintain` | Check ongoing health: stale docs, broken links, missing changelog/release notes, abandoned issues/templates, drift. |
| `good-repo explain <repo>` | Explain why a repo is exemplary or weak, with transferable patterns. |
| `good-repo owner-audit <owner>` | Assess every public repo under a GitHub owner/profile; score, detect homepage drift, and summarize portfolio cleanup themes. |

If the user asks generally to "make this repo better," run the default audit first, then ask before editing. If they ask to assess a GitHub owner/profile, use `skills/good-repo/scripts/audit-github-owner.py <owner>` when available, then manually review the high-value repos and any homepage URL drift candidates.

---

## Trigger policy

### Strong triggers — load this skill

Use `good-repo` when the user asks about any of these:

- **Repo quality, success, or popularity** — "is this repo good?", "make this repo exemplary", "why isn't this repo getting adopted?", "will repo quality affect stars?", "repo effectiveness", "launch-ready".
- **GitHub launch/configuration** — topics, description, homepage URL, social preview, Community Profile health, issues/discussions/wiki, license detection, branch protection, releases, templates.
- **Repo audits** — current repo, public GitHub URL, OSS project, owner/org-wide assessment, random repo sample, score/recommendation table.
- **Metadata drift** — README vs package vs GitHub mismatch; live/demo/docs URL not configured as GitHub homepage; README license vs missing root `LICENSE`.
- **Adoption/trust gaps** — no quick start, no proof, no examples, stale screenshots, missing CI, missing changelog, missing contribution path.
- **Skill repo quality** — `SKILL.md` frontmatter, `skills/<name>/` layout, `.claude-plugin/marketplace.json`, `pi.skills`, references, scripts, evals.
- **Exemplar comparison** — Hallmark, good-pr, good-readme, ripgrep, Flask, or any repo used as a model.

### Soft triggers — load if repo-level judgment is needed

- README improvement that also asks about launch, metadata, proof, examples, or trust.
- PR/contribution process at the repository level, not drafting one PR.
- CI/release/dependency automation when the question is about public repo readiness.
- Tests/evals as repo proof, especially for Agent Skill repos.

### Do not trigger / defer to specialists

- **README-only writing or rewriting** → prefer `good-readme`; return after it for repo-level gaps.
- **One PR description or pre-submit PR review** → prefer `good-pr`.
- **Writing or improving code tests** → prefer `testing-best-practices`; use `good-repo` only to assess tests as public proof/CI signals.
- **General code review, architecture, or implementation** with no repo-public-surface question → do not trigger.
- **GitHub issue triage or project management** unless tied to repository quality/configuration.

### Trigger decision rule

If the user's real question is "will a visitor/user/contributor trust, try, adopt, or contribute to this repo?" load `good-repo`. If the question is only about a document, PR, test, or code change, prefer the narrower skill and use `good-repo` only for the repo-level wrapper.

---

## Safety rails

Before editing, state the exact files and GitHub settings you plan to change. Ask for confirmation before any of these:

- Changing remote GitHub settings (`gh repo edit`, topics, homepage, issue/discussion/wiki toggles).
- Adding or changing legal/governance files: `LICENSE`, `CODE_OF_CONDUCT.md`, `SECURITY.md`, CLA language.
- Adding branch protection, release automation, publishing workflows, or dependency update bots.
- Deleting files, rewriting README sections wholesale, or changing public install commands.
- Claiming metrics, users, testimonials, benchmarks, compatibility, or security status not evidenced in the repo.

Always distinguish **observed evidence** from **recommended additions**. Do not invent stars, logos, adoption claims, screenshots, roadmap commitments, or support channels.

---

## Default workflow

### 0. Recon

Read enough repo context before judging:

- `README*`, `package.json`, `pyproject.toml`, `Cargo.toml`, `go.mod`, or equivalent manifest.
- `LICENSE`, `CHANGELOG`, `ROADMAP`, `CONTRIBUTING`, `SECURITY`, `.github/`.
- `docs/`, `examples/`, `site/`, `demo/`, `skills/`, `scripts/`, `evals/`, `packages/`, `tests/`.
- For GitHub remotes, inspect metadata if `gh` is authenticated or the public API is available, including `description`, `homepageUrl`, `repositoryTopics`, license detection, feature toggles, and default branch.

Classify the repo:

- **Skill / agent package**
- **Library / package**
- **CLI / TUI tool**
- **Web app / SaaS / template**
- **Docs / knowledge repo**
- **Research / ML artifact**
- **Personal / portfolio / experiment**
- **Internal / private operational repo**

State the class because "excellent" differs by class.

### 0a. Proportionality gate

Before applying any public-launch or Community Profile checklist, name the repo's lifecycle and audience. For personal experiments, internal/private repos, forks/reference repos, and historical artifacts, recommendations must be **proportionate** and **right-sized** rather than governance theater.

For a tiny personal experiment, prioritize only the lightweight basics that fit the evidence:

- README purpose/status, install/run command, and expected result.
- Dependency, secret, privacy, or data-loss footguns if present.
- License decision only when the repo is public/reusable or the owner asks.
- One small smoke check only if the repo is actively maintained or claims repeatable behavior.

Explicitly defer heavy process until the condition exists: external contributors, security-sensitive code/service, package consumers, active releases, or maintainer capacity. Do not recommend `CODE_OF_CONDUCT.md`, `SECURITY.md`, issue forms, Dependabot, branch protection, release automation, or a large CI suite just to improve a score. If those surfaces are not applicable, mark them N/A or low-priority with the condition that would make them relevant.

### 1. README handoff

If README work is a major part of the task:

1. Prefer loading/running `good-readme` if it is installed or available in the project.
2. If it is not available, use the README subsection of [`references/quality-checklist.md`](references/quality-checklist.md).
3. Do not let README polish hide repo-level gaps. A great README cannot compensate for no license, no proof, no install path, or no runnable examples.

### 2. Score repo effectiveness

Load [`references/quality-checklist.md`](references/quality-checklist.md) and score out of 100:

- Front door + README — 20
- Proof + examples — 15
- Adoption path + developer experience — 15
- Docs + repo architecture — 15
- GitHub metadata + discoverability — 10
- Trust + governance + maintenance — 15
- Automation + release hygiene — 10

Use evidence-backed scoring. If a criterion is not applicable, award credit only when the repo's class makes it genuinely irrelevant; otherwise mark as missing.

### 3. Apply exemplar patterns where useful

Load exemplar references when relevant:

- [`references/hallmark-exemplar.md`](references/hallmark-exemplar.md) — when the user asks why Hallmark works, when designing a productized skill/package repo, or when proof/gallery/live-demo discipline matters.
- [`references/good-pr-exemplar.md`](references/good-pr-exemplar.md) — when designing a small operational skill, checklist/template workflow, or skill with scripts/evals.
- [`references/skill-repo-best-practices.md`](references/skill-repo-best-practices.md) — when auditing or configuring an Agent Skill repo's folder structure, package metadata, marketplace metadata, references, scripts, or evals.

Generalize the Hallmark moves:

- README is a concise landing page, not the whole manual.
- Visual or executable proof appears in the first screenful.
- The public API is named clearly (Hallmark's four verbs).
- Detailed rules live in modular references, not a giant README.
- Examples/recipes show real outputs for different scenarios.
- Package metadata makes installation and discovery unambiguous.
- A roadmap signals active judgment, not vague TODOs.

Generalize the good-pr moves:

- Start from real user/maintainer pain.
- Invert complaints into a concrete checklist.
- Provide both a blank template and a filled example.
- Add a quick self-review checklist for the workflow.
- Automate cheap mechanical checks with a small script.
- Add evals that test the non-obvious behavior the skill must catch.
- Keep the repo small when the workflow is small.

### 4. Produce prioritized fixes

Separate findings into:

- **Launch blockers** — missing license, no install/run path, broken README commands, no proof for visual/CLI tools, package not installable.
- **High-leverage fixes** — GitHub description/topics/homepage URL, quick start, demo asset, examples, CI smoke test, changelog.
- **Polish** — issue templates, badges, social preview, docs routing, release automation, discussions.
- **Defer** — anything that requires product decisions, legal choices, paid services, or significant code work.

Every recommendation should name the smallest concrete change: file path, section, GitHub setting, command, or asset.

### 5. Configure only after plan approval

For `configure` or `launch`:

1. Show a file/settings plan.
2. Ask for confirmation when required by the safety rails.
3. Make non-destructive edits.
4. Run focused validation: markdown link checks where possible, JSON parse checks, manifest checks, `git diff --check`, available tests/lints.
5. Return changed files, commands run, failures, and remaining manual GitHub settings.

---

## Output formats

### Audit output

```markdown
## Repo Effectiveness Audit

**Project:** <name>
**Class:** <repo class>
**Score:** <N> / 100
**Rating:** Exemplary | Strong | Adequate | Weak | Poor

### Snapshot
- README/front door: <one sentence>
- Proof: <one sentence>
- Adoption path: <one sentence>
- GitHub metadata: <one sentence>
- Trust/maintenance: <one sentence>

### Top strengths
1. <strength> — evidence: `<file>` / setting / URL
2. ...
3. ...

### Priority improvements
1. <impact> — <problem> — smallest fix: `<path or setting>`
2. ...
3. ...

### Scores by category
| Category | Score | Max |
| --- | ---: | ---: |
| Front door + README |  | 20 |
| Proof + examples |  | 15 |
| Adoption + DX |  | 15 |
| Docs + architecture |  | 15 |
| GitHub metadata |  | 10 |
| Trust + governance |  | 15 |
| Automation + release |  | 10 |
| **Total** |  | 100 |

### Suggested implementation plan
1. ...
```

### Configure output

```markdown
## Repo Configuration Complete

**Changed files**
- `<path>` — why

**Manual GitHub settings still needed**
- Description: `...`
- Topics: `...`
- Social preview: upload `...`

**Validation**
- `command` — pass/fail

**Remaining risks**
- ...
```

---

## Implementation defaults

When creating files, prefer these minimal, high-signal artifacts:

- `README.md` — via `good-readme` or its rubric.
- `LICENSE` — only with user approval or obvious existing license intent.
- `CHANGELOG.md` — for versioned packages.
- `ROADMAP.md` — only when there are real planned items.
- `CONTRIBUTING.md` — if accepting contributions; keep short unless project is large.
- `.github/PULL_REQUEST_TEMPLATE.md` — simple checklist.
- `.github/ISSUE_TEMPLATE/bug_report.yml` and `feature_request.yml` — only if issues are enabled.
- `.github/dependabot.yml` — for maintained dependency ecosystems.
- `.github/workflows/ci.yml` — smallest smoke test that proves install/build/test.
- `skills/<name>/scripts/*` — only for cheap mechanical checks that complement judgment.
- `evals/evals.json` — for skill repos; encode non-obvious expected behavior and regression cases.
- `docs/recipes.md` or `examples/README.md` — for products/skills/tools where examples sell the project.

Do not add boilerplate files that the project cannot maintain. A sparse but truthful repo beats a template-zombie repo.
