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

**Update (2026-07-02):** the "next improvement" shipped. `materialize-ablations` writes real, altered skill trees (removal-only), the runners mount them, and the benchmark report's `ablation_regressions` block confirms a regression per cited case only against verified provenance (`docs/skill-ablation-spec.md`). Instruction-simulated ablations remain supported but `audit-manifest` now flags them as non-blind, raw-measurement-only.

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

## 2026-06-11 — No-skill baselines need filesystem isolation

**Problem:** A Pi smoke run with `without_skill` still found `skills/good-pr/SKILL.md` because the runner executed from the source repo. `--no-skills` stopped explicit skill loading, but grep/find/read could still discover the skill files and public eval manifests.

**Lesson:** A disabled-skill flag is not a variant boundary when the runner can read the source workspace.

**Rule:**
- Run each generation task from a per-variant temporary workspace.
- Copy skill files only for `with_skill`, `old_skill`, and materialized ablation variants.
- For `without_skill`, provide only fixture inputs and pass `--no-skills` when the runner supports it.
- Keep eval manifests, skill folders, and public answer scaffolding out of the no-skill workspace.
- Add variant-scoped process assertions: `skill_invoked=true` for `with_skill`; `skill_invoked=false` for `without_skill`.

## 2026-06-11 — Process assertions need real trace evidence

**Problem:** Final output grading could not show whether a skill was loaded, which commands ran, or whether a run stayed inside tool/token/time budgets. Manifest-only process assertions would either fail without evidence or tempt the harness to infer process from answer text.

**Lesson:** Process and efficiency claims are only valid when runner artifacts support them.

**Rule:**
- Runners should write raw `trace.jsonl` plus normalized `events.json` and `metrics.json`.
- Do not infer skill-load or command evidence from final `output.md` prose.
- Keep process/efficiency assertions fail-closed when required evidence is missing.
- Add production process assertions only after a real runner has emitted stable evidence for that case and variant.

## 2026-06-11 — Normalize observed runner shapes, not imagined schemas

**Problem:** Pi, Codex, and Jetty expose different event shapes. Codex emitted `item.completed` / `command_execution` and `turn.completed` usage records; Pi exposed `message_end` usage aliases; Jetty trajectory shapes are still only documented/mocked until live token validation.

**Lesson:** The normalized trace schema is an adapter boundary, not a reason to pretend every runner already emits the same evidence.

**Rule:**
- Preserve raw runner events before normalization.
- Add fixture tests for every event shape the adapter claims to support.
- Count completed command events, not in-progress starts or command output text.
- Mark documented/mocked Jetty trajectory import as useful but not production-grade until `JETTY_API_TOKEN` live validation confirms response shapes.
- Keep source-specific unknowns in docs or `TODO.md` instead of hiding them behind generic trace language.

## 2026-06-11 — Specs need a release-tense pass

**Problem:** After v0.4.1 shipped, the trace-aware spec still described some shipped behavior as future work. That made the docs internally inconsistent even though the code and changelog were correct.

**Lesson:** Specs become misleading when release state changes but tense and caveats do not.

**Rule:**
- After each release, scan docs for stale “proposal,” “next,” and “first implementation” language.
- Separate shipped behavior from follow-on work in specs.
- Keep live-validation caveats visible, especially for external adapters.
- Record docs-only corrections in `CHANGELOG.md` under `Unreleased`.

## 2026-06-30 — Multi-skill eval suites must be allowlisted and pinned

**Problem:** A broad filesystem scan for `*/evals/shared-benchmark.json` pulled in an unrelated top-level tool (`beautiful-mermaid`) when the intended scope was the pinned 10-skill study. That silently changed the matrix, cost, and interpretation of the run.

**Lesson:** Suite scope is part of the experiment contract. It must be explicit, reviewable, and reproducible before any model calls are made.

**Rule:**
- Use a suite allowlist (`examples/adewale-workspace/all-manifests.txt`) as the source of truth; never glob arbitrary repos for official runs.
- Run `skill-benchmark suite-run ... --tier preflight` before expensive tiers.
- Fail closed on top-level manifests not present in the allowlist unless the run is explicitly exploratory.
- Verify skill tree hashes with `examples/skill-pins.json`; if a skill repo is intentionally updated, refresh pins as a conscious act and rerun preflight.
- Save `RUN_SCOPE.json` with every suite run and treat it as the audit record for what was actually evaluated.

## 2026-06-30 — Cost is an eval-quality signal, not just an invoice

**Problem:** Per-run Pi metadata contained token and dollar cost, but the harness did not promote it into a suite-level artifact. The latest allowlisted full matrix had enough raw telemetry to show ~82.6M generation tokens and ~$175.21 in provider-reported Pi generation cost, but judge and trigger costs were not persisted.

**Lesson:** Cost must be normalized and reported next to quality metrics so we can distinguish useful signal from expensive noise.

**Rule:**
- Preserve raw provider `usage`/`cost`, but also write normalized `usage_normalized` and `cost_normalized` blocks with source/provenance.
- Report suite totals by skill, case, variant, runner, and ablation.
- Track coverage: runs with token telemetry, runs with dollar telemetry, missing usage, and missing cost.
- Add budget gates for PR smoke, nightly tune, and release/full-ablation tiers.
- Use cost-quality findings: expensive saturated cases, expensive no-lift cases, judge-heavy cases that could be deterministic, and ablations with high spend but no confirmable expected regression.
- Do not mix missing cost with zero cost; offline/stub runners should mark telemetry as `not_applicable`.

**Update (2026-07-02):** implemented as issue #21. Every runner path writes normalized `usage_normalized`/`cost_normalized` blocks with explicit provenance (missing is marked, never zero); `benchmark`/`aggregate` carry a `cost_summary`; `cost-summary` writes the standalone ledger; `suite-run` gates on `--max-estimated-cost-usd`/`--max-estimated-tokens`; `token-overhead` reports dollar deltas and lift-per-dollar; and `audit-manifest --runs` emits the cost-quality findings above.

Latest allowlisted Pi generation cost snapshot (`safe-suite-20260630-201448-pi`; excludes judge and trigger cost because those paths did not persist usage/cost):

| Skill | Runs | Total tokens | Cost (USD) |
|---|---:|---:|---:|
| swiss-poster-skill | 1,272 | 44,808,350 | 124.34 |
| cfdoctor | 176 | 9,625,548 | 12.83 |
| testing-best-practices | 276 | 9,204,778 | 12.11 |
| slide-maker | 118 | 3,199,560 | 5.10 |
| good-readme | 117 | 3,933,828 | 4.15 |
| guardrails-skill | 164 | 2,283,690 | 3.92 |
| audit-skill | 138 | 4,270,346 | 3.79 |
| good-pr | 124 | 1,659,912 | 3.65 |
| good-repo | 107 | 2,647,480 | 3.55 |
| anti-slop-writing | 76 | 1,005,364 | 1.75 |
| **Total** | **2,568** | **82,638,856** | **175.21** |

## 2026-06-30 — The full matrix is slow because ablations and judges multiply calls

**Problem:** The full allowlisted suite took a long time because it was not one eval; it was thousands of model interactions. The matrix included 514 baseline Pi generation calls, 2,054 ablation generation calls, and 2,358 judge calls. Median Pi generation latency was about 27s, p90 about 42s, with some 180s timeouts.

**Lesson:** Runtime is dominated by multiplicative design choices: answer cases × variants × ablations × repeats × judges. The ablation matrix is especially expensive when every ablation applies to every answer case.

**Rule:**
- Run tiers deliberately: preflight/static first, smoke subset second, full tune only when needed, holdout/release last.
- Narrow ablation applicability to cases that can actually exercise the removed component.
- Prefer materialized ablations with structured expected regressions over broad instruction-simulated ablations.
- Keep judge assertions for properties deterministic checks cannot express; replace judge-heavy checks with script/artifact oracles when possible.
- Use historical cost summaries to select a cheap high-signal smoke subset and reserve expensive cases for nightly or release runs.

## 2026-07-02 — Make the measurement believable before scaling what you measure

**Problem:** The eval-framework roadmap wanted to scale what the harness reports — graded scores, multi-model lift, more assertion families. But the one number it already prints (lift = `with_skill` − `without_skill`) rested on three things that were *intended* but never *enforced*: that detectors do not fire falsely or stay silent falsely, that the `without_skill` baseline truly cannot read the skill, and that grading is deterministic and model-free. A graded or multi-model feature built on an unverified detector only scales an unverified result.

**Lesson:** Test the harness before extending it. A reported difference is only as trustworthy as the detectors, the baseline isolation, and the determinism underneath it — and those are cheap to make executable.

**Rule:**
- Ship the "confidence floor" first: paired should-fire/should-pass fixtures per objective detector (`tests/fixtures/detectors/`), a cross-runner `without_skill` isolation invariant, re-grade idempotence, and a guard that the core grade path calls no model and no network (`tests/test_confidence_floor.py`).
- A new objective assertion type does not land without its should-fire/should-pass fixture pair; a meta-test enforces this so a detector cannot be trusted on entry without one.
- A new runner registers its workspace builder so the baseline-isolation invariant covers it automatically; it must not hand-roll its own isolation check.
- Sequence the floor before the buckets: a believable small number beats an impressive unverified one.

## 2026-07-02 — A new scoring tier or fan-out axis must be threaded through every consumer

**Problem:** Two cross-cutting additions caused silent errors far from where they were defined. Adding a `soft` severity tier (meant to feed only the graded score) still moved `combined_pass_rate`, still failed JUnit test cases, and still fed the held-out-vs-tune visibility report — because those consumers counted *all* qualitative rows, not the gated subset. Adding a `model` fan-out axis collided judge verdicts across models, because `judge_task_id` identified a run by `(case, variant, run)` and never learned the new axis, so the last-loaded verdict silently applied to every model. A PR review caught all of these after the features "worked" in isolation.

**Lesson:** A cross-cutting dimension (severity, model) touches every place that *aggregates* a run or *identifies* a run, not just the place that defines it. The blast radius is the set of report views and keys, not the feature's own function.

**Rule:**
- When you add a severity tier, audit every pass-rate/totals consumer (`grade_case_variant` totals, `build_benchmark_report`, JUnit/`report`, readiness signals, the visibility split) — soft must never move a pass rate anywhere.
- When you add a fan-out axis, audit `run_dir`, run discovery, *and* every identity key that names a run (`judge_task_id`), keeping the segment optional so single-axis records are unchanged.
- Distinguish populations that share a mechanism: a "soft objective" check (e.g. `similarity`) and a "soft judge" verdict both live in the soft bucket, but a judge-visibility report must filter by the actual shape (`qualitative_assertions`), never by a blended proxy (`soft_total`).
- A parse-then-persist step must forward the whole payload: the judge command dropped `dimension_scores`/`criteria` from its stored row, so graded-dimension judging silently degraded to failed plain verdicts. Have the producer and the merge derive the verdict through one shared owner, not two parallel code paths.

## 2026-07-02 — Prose-to-code references rot on every large change; make them executable

**Problem:** A single large merge stranded roughly fifty `name:line` references in `TODO.md` and the specs — every function reference the docs used to anchor prose to code drifted by tens to hundreds of lines. Nothing detected it, so the docs read as precise while pointing at the wrong lines.

**Lesson:** Any hand-maintained pointer from prose to code drifts silently the moment the code moves. Precision that is not checked becomes confident misinformation.

**Rule:**
- A unit test resolves every documented `name:line` / `` `name` (`:line`) `` reference to the actual definition line and fails on drift (`tests/test_doc_refs.py`); fixing a failure is mechanical (the message carries the correct line).
- Every new field, command, assertion type, or flag is reflected in `README.md` and `CHANGELOG.md` in the same change that adds it — the changelog is the release contract, not an afterthought.
- Keep new manifest surfaces additive with behavior-preserving defaults, and regression-test that a pre-change manifest grades to identical pass rates, so "we upgraded the harness" never silently means "your old evals now score differently."
- After a batch lands, give the docs a release-tense and status pass (per the 2026-06-11 lesson) and annotate — do not silently rewrite — any earlier "next improvement" note that has since shipped.

**Update (2026-07-06):** the reference guard now has a sibling for the other rot vector. `tests/test_doc_links.py` resolves every relative doc link — file target and `#anchor`, fence- and inline-code-aware — so the docs restructure (moat-first README, `docs/commands.md` split out) could move sections without silently stranding a cross-link. Prose→code and prose→doc are the same failure mode; both are now executable.

## 2026-07-06 — A concept with N copies drifts; consolidate to one owner and guard the fork

**Problem:** Several concepts had been implemented more than once and the copies had silently diverged, each divergence a real behavioral bug rather than a style nit. The answer runners (`run-codex`, `run-claude`, `run_subagent_tasks`) each hand-rolled the raw-result→run-contract adaptation, and the Codex empty-output path wrote `metadata.json` without the normalized usage/cost blocks the others always wrote (and `schema_version: 1` where the subagent wrote 2). The trigger runners (`skill-pi-trigger-eval`, `skill-trigger-matrix`) had re-forked repo-root resolution, manifest loading, skill-tree mounting, the Pi argv, and subprocess-timeout handling — so a manifest outside `evals/` mounted from a different root than `benchmark` used, and YAML/`dataset_files` manifests that worked in the harness silently broke in the runners. The case/model/variant/run walk existed as four hand-synced copies; the timeout encoding as eight `1800` literals with three different failure shapes (one lost the `timed_out` flag entirely); `ablation:<id>` parsing as 14 inline `split(":", 1)`/`startswith` sites; the token-usage alias table as three drifted copies that could classify the same provider payload differently.

**Lesson:** Duplication is not a cosmetic problem — every copy is a place the behavior can quietly disagree, and the disagreement surfaces as wrong metadata, a wrong mount root, or a dropped manifest, not as a failing test. A concept needs exactly one owner, and the "one owner" property has to be enforced at the source level or it re-forks on the next change.

**Rule:**
- One owner per concept, named for the behavior: `write_runner_outcome`/`RunnerOutcome` (run-contract adaptation), `discovered_run_units` (the run walk), `mount_skill_tree`/`run_argv_with_timeout`/`pi_argv` (trigger-runner plumbing), `DEFAULT_RUNNER_TIMEOUT_S` + the `timed_out: true`/returncode-124 convention (timeout encoding), `ABLATION_VARIANT_PREFIX`/`is_ablation_variant()`/`ablation_id_of()` (ablation parsing), one `USAGE_ALIASES` table, `is_trigger_case`/`is_judge_only_case` (population boundaries).
- Consolidation that touches artifact shape must be behavior-preserving on the numbers: grading, pass rates, cost totals, and verdicts stay byte-identical; the only intended effect is that all runners now emit *one* shape. State that explicitly so a reviewer knows added keys are zero/empty and readers tolerate them.
- Add a source-level guard that forbids re-forking, not just a behavior test: `tests/test_consolidation_guards.py` source-checks that no answer runner hand-rolls a run-level contract write, that the trigger helpers *are* the harness's functions (identity, not equality), that the runner `--timeout` flags share `DEFAULT_RUNNER_TIMEOUT_S`, and that the evidence-class literal is not re-spelled.
- A helper named for one caller lies once three callers share it: `codex_skill_workspace`/`codex_task_prompt` became `build_skill_workspace`/`build_task_prompt` because `run-codex`, `run-claude`, and `run-subagent` all mount and prompt through them. Name for behavior, not for the first caller.
- When two implementations must legitimately differ, make the difference a named, documented decision, not two private copies: `discover_on_disk_run_rows` sits beside `discovered_run_units` because the suite ledger deliberately bills every on-disk arm while grading is variant-scoped.

## 2026-07-06 — Autonomous activation is its own measurement, not a corner of answer-lift

**Problem:** The harness's headline number is answer-lift (`with_skill` − `without_skill`), and every answer runner *force-loads* the skill to isolate answer quality from discovery. But force-loading measures whether the skill helps once found; it says nothing about whether the agent finds it on its own — and that discovery behavior varies by agent and by model, which the force-loaded path structurally cannot see.

**Lesson:** Whether a skill activates is a different construct from whether it helps, measured on a different population with opposite polarity, and it varies across the (agent, model) grid. Folding it into the answer-lift path would either force-load (and measure nothing about discovery) or contaminate the answer measurement. It belongs beside answer-lift as a first-class, un-force-loaded measurement, not inside it.

**Rule:**
- Measure activation per (agent, model) cell with the skill mounted where the agent discovers skills on its own — never force-loaded — and report trigger rates split by should-fire / should-not-fire polarity (`skill-trigger-matrix` / `run_trigger_matrix.py`).
- Give each run a clean discovery environment: a fresh `CLAUDE_CONFIG_DIR` per run so personal skills stay out while the agent's built-ins stay in. A leaked config dir turns "did it discover the skill" into "did it already have the skill."
- Keep the discovery population out of the answer graders: `is_trigger_case` routes `kind: "trigger"` cases away from `benchmark`/`grade`/the judge collector so a discovery output is never graded or judge-billed as an answer, and the report stamps the `raw_autonomous_trigger_measurement` evidence class (owned once as `TRIGGER_MEASUREMENT_EVIDENCE_CLASS`).
- Ship an offline deterministic adapter (`stub`) that decides from the mounted description, so a weakened description measurably under-triggers in CI without a model call; other agents register via an `AgentAdapter` subclass rather than re-forking the mount/detect loop (per the one-owner lesson above).

## 2026-07-07 — Native agent parity is mostly subprocess contracts, not model prompting

**Problem:** Adding Claude/Codex parity looked like "call another CLI", but the live run exposed three non-prompting failure modes: Claude can return a JSON envelope with `is_error:true` and exit 0; Codex structured output requires provider-strict JSON Schema (`additionalProperties:false`, nullable optionals); and Codex answer runs need an isolated, config-light invocation (`--skip-git-repo-check --ephemeral --ignore-user-config --ignore-rules --sandbox read-only -`) or local user config/MCPs leak into eval cost and stderr.

**Lesson:** Treat every agent backend as an adapter contract: argv construction, cwd, timeout, error envelope semantics, final-message extraction, schema adaptation, telemetry normalization, and failure-body writing are the product. The prompt is only one field in that contract.

**Rule:**
- Native answer runners enter through one `run-agent` path and write through `RunnerOutcome`/`write_runner_outcome`; compatibility commands (`run-claude`, `run-codex`) stay thin wrappers.
- A provider-reported error envelope is an infrastructure failure even when the process exits 0; never grade a quota message as an answer.
- Codex judge verdicts come from `--output-last-message`, while stdout JSONL is telemetry; adapt schemas for the provider, then validate returned verdicts against the harness's canonical schema.
- Run live parity checks with the smallest models first, and record quota/auth/config failures as eval-run artifacts rather than losing the row.
