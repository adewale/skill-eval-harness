# Repo Effectiveness Audit

Date: 2026-06-11

This audit applies the `good-repo` skill to `adewale/skill-eval-harness` as a public Python CLI/package repo for Agent Skill evaluation.

## Snapshot

**Project:** `skill-eval-harness`  
**Class:** CLI / Python package for Agent Skill evals  
**Lifecycle:** active, public, versioned, early but usable  
**Score:** 86 / 100  
**Rating:** Strong

The repo has a clear README, install path, CI, releases, changelog, contribution guidance, issue templates, and tests that exercise the core CLI behavior. The main gaps are GitHub-side discoverability settings and a few optional maintenance surfaces that require owner choices.

## Scores by category

| Category | Score | Max | Evidence |
| --- | ---: | ---: | --- |
| Front door + README | 19 | 20 | `README.md` explains the purpose, core loop, quick start, expected outputs, commands, run contract, and non-goals. |
| Proof + examples | 13 | 15 | `tests/test_skill_benchmark.py`, CLI examples, `examples/adewale-workspace/`, and generated benchmark/report paths prove behavior; no separate gallery is needed for this CLI. |
| Adoption + DX | 14 | 15 | `uv` install commands, editable install, Python version, console scripts, and validation commands are documented. `pyproject.toml` now includes URLs, keywords, and classifiers. |
| Docs + architecture | 14 | 15 | `docs/`, `examples/`, tests, `TODO.md`, and `LESSONS_LEARNED.md` route readers to depth without bloating the README. |
| GitHub metadata + discoverability | 5 | 10 | GitHub API shows empty repo description, no topics, and no homepage URL. No separate docs site was found, so an empty homepage is acceptable unless one is created. |
| Trust + governance + maintenance | 12 | 15 | Root `LICENSE`, `CHANGELOG.md`, `CONTRIBUTING.md`, PR template, issue forms, releases, and lessons doc exist. `SECURITY.md`/support policy should wait for an owner contact and supported-version policy. |
| Automation + release hygiene | 9 | 10 | GitHub Actions runs Python 3.10/3.11/3.12 compile, unit tests, and CLI help. Tags/releases exist. Dependency automation is not currently needed because there are no runtime dependencies. |
| **Total** | **86** | **100** | Strong repo surface with a few owner-controlled metadata gaps. |

## Strengths

1. **Clear front door** — `README.md` quickly states what the harness does, how to install it, how to prepare tasks, and how to grade runs.
2. **Executable proof** — the test suite covers trace normalization, Jetty import/export, script assertions, judge command plumbing, trigger detection, and process/efficiency assertions.
3. **Trust signals** — license, changelog, releases, CI, contribution guidance, issue templates, and lessons learned are present and current.
4. **Honest external-adapter caveats** — Jetty live-token validation is still called out instead of being hidden behind generic adapter language.

## Priority improvements

### 1. Set GitHub description and topics

Observed with `gh repo view`: `description` is empty and `repositoryTopics` is null.

Smallest owner action:

```sh
gh repo edit adewale/skill-eval-harness \
  --description "Repo-agnostic Agent Skill evaluation harness for paired variants, trace artifacts, and runner adapters"

gh repo edit adewale/skill-eval-harness \
  --add-topic agent-skills \
  --add-topic evals \
  --add-topic benchmark \
  --add-topic codex \
  --add-topic jetty \
  --add-topic python-cli
```

Do not run these from automation without owner confirmation; they mutate remote GitHub settings.

### 2. Decide whether GitHub wiki should stay enabled

Observed with `gh repo view`: `hasWikiEnabled=true`.

The repo already uses tracked docs under `docs/`, `TODO.md`, and `LESSONS_LEARNED.md`. If the wiki is unused, disable it to keep documentation in version-controlled files.

Smallest owner action:

```sh
gh repo edit adewale/skill-eval-harness --enable-wiki=false
```

### 3. Add `SECURITY.md` only when a real policy exists

A vulnerability-reporting policy would make sense if this package becomes widely installed or handles untrusted runner traces at scale. Do not add a template policy until the owner can name:

- supported versions,
- reporting contact or private advisory workflow,
- expected response window.

Until then, this is a deferred trust improvement, not a launch blocker.

### 4. Consider a lightweight social preview later

A social preview is polish, not a blocker. If this repo becomes a launch target, use a small terminal-output or benchmark-summary card that shows the core loop: prepare, run, benchmark.

## File changes applied from this audit

- `pyproject.toml` now includes package keywords, PyPI classifiers, project URLs, and SPDX license metadata.
- This audit is saved as `docs/repo-effectiveness-audit.md` so future repo-surface work has a baseline.

## Manual GitHub settings still pending

These require explicit owner confirmation before mutation:

- Description: `Repo-agnostic Agent Skill evaluation harness for paired variants, trace artifacts, and runner adapters`
- Topics: `agent-skills`, `evals`, `benchmark`, `codex`, `jetty`, `python-cli`
- Wiki: disable if tracked docs remain the source of truth
- Social preview: optional launch polish

## Re-audit trigger

Run this audit again after any of these changes:

- a new public release,
- package publication beyond GitHub installs,
- live Jetty validation,
- a docs site or homepage is created,
- GitHub metadata is changed,
- security/support policy is added.
