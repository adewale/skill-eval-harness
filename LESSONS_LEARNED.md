# Lessons Learned — Skill Eval Harness

This file records durable lessons from building and using the shared skill evaluation harness across Adewale’s skill repos. Keep it practical: each lesson should change how the harness, manifests, or skill iteration process is run next time.

## 2026-06-09 — Eval generation must fail closed

**Problem:** Early task preparation could leak answer keys or silently proceed with missing hidden prompts.

**Lesson:** Generation tasks and grading materials must be separated by default.

**Rule:**
- Do not include `expected_behavior`, rubrics, or answer keys in prepared tasks unless `--include-answer-key` is explicit.
- Missing `prompt_ref` should fail unless `--allow-missing-prompts` is explicitly used for dry-run planning.
- Hidden holdout/holdback content should stay private until scoring.

## 2026-06-09 — `old_skill` and ablations must be explicit variants

**Problem:** Treating `old_skill` or ablations as implicit defaults makes benchmark rows ambiguous and can compare against nonexistent baselines.

**Lesson:** Every non-default comparison arm needs an explicit manifest/config contract.

**Rule:**
- Default variants are only `with_skill` and `without_skill`.
- `old_skill` requires populated `manifest.old_skill_paths` and `--include-old-skill`.
- Ablations are opt-in with `--include-ablations`.

## 2026-06-09 — Trigger evals are not normal answer evals

**Problem:** Trigger cases were originally stored as meta-prompts such as “Trigger decision eval. User prompt: …”, which tests the classifier prompt, not autonomous skill loading.

**Lesson:** Autonomous trigger tests must run the real user prompt.

**Rule:**
- Extract the embedded real user prompt before running Pi trigger checks.
- Keep trigger/no-trigger cases separate from answer-quality smoke runs.
- Trigger cases should assert skill discovery behavior, not final answer content.

## 2026-06-09 — Trigger detection must use real load evidence, not names

**Problem:** Bare skill names caused false positives. Example: reading `good-readme/README.md` looked like loading the `good-readme` skill.

**Lesson:** Detect skill loading from copied temp skill paths, not from names or repo filenames.

**Rule:**
- Match only the temp copied `SKILL.md` path or its temp skill directory.
- Seed the temp Pi config with auth/settings, but not the user’s installed skills.
- Nonzero/timeout trigger runs are failures, not silent no-trigger passes.

## 2026-06-09 — Strong models make many evals saturated

**Problem:** Several cases had `with_skill=1.0` and `without_skill=1.0`; this is not a skill failure, but it is weak evidence of skill lift.

**Lesson:** Saturation and skill quality are different measurements.

**Rule:**
- Track `with_skill` pass rate separately from discrimination/lift.
- Keep saturated/no-lift flags as eval-quality signals.
- Add harder fixtures or artifact-level checks when `without_skill` also passes.

## 2026-06-09 — “All saturated” must be defined before optimizing

**Problem:** “Saturated” can mean either “with-skill passes everything” or “no-skill also passes,” which leads to opposite actions.

**Lesson:** Define saturation as the target metric for the current loop.

**Rule used in this round:**
- Answer-quality saturation: every `with_skill` smoke row has objective pass rate `1.0`.
- Trigger saturation: every tune trigger/no-trigger case passes autonomous skill-discovery classification.
- Do not weaken evals just to make flags disappear.

## 2026-06-09 — Missing outputs are not failed/no-lift cases

**Problem:** Unrun or missing outputs were initially counted like failed rows, creating false no-lift flags.

**Lesson:** Distinguish “not measured” from “measured and failed.”

**Rule:**
- Missing outputs should be marked `missing_output` and excluded from no-lift/saturation comparisons.
- Timeouts should write explicit artifacts and metadata so the benchmark can grade or flag them consistently.

## 2026-06-09 — Smoke runs need hard bounds

**Problem:** A Slide Maker architecture case spent the whole timeout in planning/thinking and produced no final answer.

**Lesson:** Eval runners need stricter execution envelopes than normal agent work.

**Rule:**
- Use minimal thinking for smoke runs.
- Add bounded-response instructions.
- Capture timeout output/metadata instead of aborting the whole round.
- For underspecified project-deck tasks, require bounded/no-write mode rather than open-ended build loops.

## 2026-06-09 — Fixture-backed cases are better than keyword-only prompts

**Problem:** Inline-only prompts can be solved from generic knowledge or by matching assertion keywords.

**Lesson:** Real files make evals more diagnostic.

**Rule:**
- Use `case.files` for source-backed drift, planted bugs, artifact audits, and repo-specific claims.
- Validate fixture paths in `validate`.
- Emit absolute `input_files` in `prepare` and tell runners to read them before answering.

## 2026-06-09 — Assertions should allow equivalent good behavior

**Problem:** Correct outputs failed when assertions expected overly narrow wording, e.g. `Decision: BLOCK` but the output used `**Decision: BLOCK.**`.

**Lesson:** Objective assertions should test behavior, not one phrasing.

**Rule:**
- Prefer semantically meaningful contains/regex variants.
- Calibrate only when the output clearly satisfies the intended behavior.
- Do not calibrate away genuine misses.

## 2026-06-09 — Frontmatter descriptions are the trigger boundary

**Problem:** Skills over-triggered or under-triggered because frontmatter descriptions were too broad or omitted common invocation language.

**Lesson:** Trigger evals are mostly description evals.

**Rule:**
- Include common positive trigger phrases in `description`.
- Include explicit negative boundaries when adjacent skills exist.
- Re-run autonomous trigger tests after every description change.

Examples from saturation work:
- `good-readme`: narrowed away full docs sites and launch-readiness audits.
- `audit-skill`: narrowed away conceptual security-audit explainers.
- `good-repo`: narrowed away function-level test-writing.
- `cfdoctor`: narrowed away generic Cloudflare status questions.
- `guardrails`: narrowed away README/prose-only edits.
- `anti-slop-writing`: added “tighten,” “talk intro,” and “generic launch copy.”

## 2026-06-09 — Ablations are not evidence until they are run

**Problem:** The manifests declare many ablations, but no benchmark report currently contains `ablation:<id>` rows.

**Lesson:** Ablations are useful planning scaffolds, but not proof of load-bearing instructions until measured.

**Rule:**
- Run ablations on discriminating cases where `with_skill=1.0` and `without_skill<1.0`.
- Treat no-regression ablations as evidence that either the component is redundant/unclear or current evals do not exercise it.
- Next improvement: materialize ablated skill variants or generate explicit ablation patches instead of only instruction-simulating removal.

## 2026-06-09 — Holdout and holdback still matter after tune saturation

**Problem:** Tune saturation can create false confidence.

**Lesson:** Public/tune success is not release proof.

**Rule:**
- Tune cases are for iteration.
- Holdout is for end-of-round scoring.
- Holdback stays hidden from skill/docs/evals until after scoring to detect overfitting.
- Do not claim release-quality proof until hidden prompts, private answer keys, and real fixtures are filled and scored.

## 2026-06-09 — Jetty should be an adapter, not a rewrite

**Problem:** Jetty has useful trajectory/runbook/sandbox execution features, but the harness manifest and grading contract should remain the source of truth.

**Lesson:** External runners should execute tasks and return artifacts; the local harness should own manifests, grading, and reports.

**Rule:**
- Add `export-jetty`, `run-jetty`, and `import-jetty-results` as adapters.
- Preserve local variants, splits, assertions, fixture files, and judge-result imports.
- Keep Jetty open questions in `TODO.md` until API-token-backed testing is done.

## 2026-06-09 — Do not fake ecosystem telemetry

**Problem:** It was possible to reverse-engineer `skills.sh` telemetry, but raw pokes would be dishonest.

**Lesson:** Marketplace/install metrics should only come from official user-like flows.

**Rule:**
- Use official CLI installs only.
- Do not forge install telemetry or call tracking endpoints directly.
