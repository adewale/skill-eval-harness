# Materialized Skill Ablation Spec

Status: design spec, unimplemented. This closes the open `TODO.md` item
"Materialize true ablated skill files instead of instruction-simulated
ablations", discharges the "next improvement" recorded in `LESSONS_LEARNED.md`
(2026-06-09, *Ablations are not evidence until they are run*), and supplies the
materialized `ablation:<id>` runs that `docs/trace-aware-eval-spec.md` already
names as the release-grade baseline.

## Design principle

**An ablation is a hypothesis; a materialized ablation is the experiment.
Ablate along the skill format's seams, not along lines of text.**

A skill is not a prose blob. It is a specified artifact — YAML frontmatter plus
a Markdown body plus bundled resources — loaded into context in three
progressive-disclosure stages. The structure the spec defines *is* the
component taxonomy worth ablating. The harness materializes a real, altered
skill tree, points the `ablation:<id>` variant at it, and lets every runner
consume it exactly as it consumes `with_skill`. Removal logic lives in the
harness once, not in each runner.

## Grounding sources

External — the format under test (read these before extending the schema; the
field list evolves, so the materializer reads frontmatter generically rather
than hardcoding it):

- Agent Skills specification — `https://agentskills.io/specification`
- Claude Code skills docs & frontmatter reference — `https://code.claude.com/docs/en/skills`
- Skill authoring best practices — `https://platform.claude.com/docs/en/agents-and-tools/agent-skills/best-practices`

Internal — the contracts this builds on:

- `validate_manifest` ablation block (`skill_benchmark.py:255`) — today checks only `id` + `removed_component`.
- `variant_instruction` (`skill_benchmark.py:265`) — emits the per-arm instruction.
- `task_variants` / `prepared_task_rows` (`skill_benchmark.py:302`, `:314`) — emit task rows; `skill_paths` is currently identical for every variant.
- `safe_task_json` (`skill_benchmark.py:452`) — Jetty export; already stamps `ablation.mode = "instruction_simulated"` (`:471`).
- `copy_skill_source` (`examples/adewale-workspace/run_pi_smoke.py:100`) — the skill-tree copy shape (SKILL.md + `references`/`scripts`/`assets`) to reuse.
- `docs/vocabulary.md` — *Ablation*, *Variant*, *Token overhead*, *Trigger / no-trigger*.
- `LESSONS_LEARNED.md` — *trigger boundary* (descriptions), *ablations are not evidence until run*, *per-variant workspaces*.

## Current state: instruction-simulated ablations

Every variant — including `ablation:<id>` — receives the **same unmodified
skill**. `prepared_task_rows` emits the real `skill_paths` for all variants and
differentiates only the instruction string. The runner copies the full real
skill and appends "ignore component X … this is an instruction-simulated
ablation, not a materialized alternate skill file" (`run_pi_smoke.py:161`).

This is a prompt *about* the skill, not a changed skill. It is a useful
planning scaffold but weak evidence: the model still sees the full text and may
follow it anyway, and the result cannot be attributed to the component's
absence. Instruction-simulated mode remains supported as an explicit fallback;
it stops being the default for ablations that declare a mechanism.

## A skill is a three-layer artifact

Progressive disclosure loads a skill in three stages, and **the layer you cut
determines what the ablation measures and which cases can show it**:

| Layer | Contents | Loaded | Ablation tests | Discriminating cases |
|---|---|---|---|---|
| **Discovery** | `name`, `description`, `when_to_use` | startup, always | does the skill **trigger**? | trigger / no-trigger cases |
| **Execution** | `SKILL.md` body | on invocation | does **answer quality** degrade? | answer cases where `with_skill` > `without_skill` |
| **On-demand** | `references/`, `scripts/`, `assets/`, inline `` !`cmd` `` | only if the body reaches them | is the resource **reached and load-bearing**? | answer cases that need the resource |

Two consequences:

1. The current rule "ablations skip trigger cases" (`README.md:569`) is correct
   *for execution and on-demand ablations* — but discovery ablations are the
   opposite: they run **only** against trigger cases. The repo already holds
   that this axis exists ("trigger evals are mostly description evals",
   `LESSONS_LEARNED.md:119`); this spec wires the existing trigger path to the
   ablation flow rather than inventing machinery.
2. An on-demand resource loads only if the body points at it, so a reference has
   two independently-removable parts (pointer vs content) that test different
   things. See *Mechanisms*.

## Vocabulary additions

- **Materialized ablation** — an `ablation:<id>` variant whose skill files are a
  real, altered copy of the skill, produced by the harness. Contrast with
  *instruction-simulated*.
- **Ablation layer** — `discovery` | `execution` | `on_demand`. Selects what the
  ablation tests and which case kinds it runs against.
- **Removal mechanism** — how the alteration is produced (`frontmatter_field`,
  `section`, `anchor`, `list_item`, `patch`, `reference`, `script`, `asset`,
  `instruction_simulated`).
- **Ablation provenance** — the recorded `{mode, layer, mechanism, target,
  diff_stat, skill_hash}` on every `ablation:<id>` run, so reports never conflate
  a materialized regression with a simulated one.

## Manifest schema

The ablation entry gains an optional `layer`, `mechanism`, and `target`.
Back-compat: an entry with no `mechanism` keeps today's behavior exactly —
`instruction_simulated`, execution layer. Existing manifests are untouched.

```jsonc
{
  "id": "no-regression-proof",
  "removed_component": "regression-proof requirement",   // kept: human-readable label
  "expected_regressions": ["Accepts weak tests that pass without the fix"],
  "layer": "execution",                                   // discovery | execution | on_demand
  "mechanism": "section",
  "target": { "skill_path": "SKILL.md", "heading": "## Regression-proof requirement" }
}
```

`target` shape per mechanism:

| Mechanism | Layer | `target` | Removes |
|---|---|---|---|
| `instruction_simulated` | execution | *(none; uses `removed_component`)* | nothing — prompt-level fallback |
| `frontmatter_field` | discovery / runtime | `{ "field": "allowed-tools" }` | one frontmatter key |
| `section` | execution | `{ "skill_path": "SKILL.md", "heading": "## …" }` | heading + body + nested subheadings |
| `anchor` | execution | `{ "anchor": "no-scope-check" }` | span between `<!-- ablation:ID:start/end -->` |
| `list_item` | execution | `{ "section": "## …", "contains": ["…"] }` | matching list items in a section |
| `patch` | execution / discovery | `{ "patch": "evals/ablations/<id>.patch" }` | any byte-level change (incl. one word) |
| `reference` | on_demand | `{ "path": "references/x.md", "remove": "pointer\|content\|both" }` | body link, file, or both |
| `script` / `asset` | on_demand | `{ "path": "scripts/x.py" }` | a bundled executable / asset file |

Discovery-layer examples: remove the whole `when_to_use`
(`frontmatter_field`), or excise one trigger phrase from `description` with a
`patch`. Both run against trigger cases only.

## Mechanisms and granularity

Granularity is two-dimensional: **which layer** × **how surgical**. Within the
body the unit ladder is section → subsection → list item → sentence/word
(patch). Worked before/after (from the prototype, on a representative
`good-pr` skill):

`section` — takes nested `###` children with it:

```diff
-## Regression-proof requirement
-For any bug-fix or security PR, require a test that **fails without the fix
-and passes with it**. …
-### Why  … ### How to check …
 ## Severity and verdict
```

`frontmatter_field` — structured, no diff fuzz (a seam prose ablation cannot reach):

```diff
 when_to_use: When the user asks to review a PR, diff, or patch …
-allowed-tools: Read, Grep, Bash(git diff:*)
 ---
```

`reference` with `remove: pointer` — file stays, body link goes, so the model never loads it:

```diff
-examples live in [the severity guide](references/severity.md).
+examples live in its calibration guide.
```

`patch` — finest grain; weaken a single directive that no section/anchor could isolate:

```diff
-Mentally revert the fix and ask whether the new test still passes. If it does,
+Optionally revert the fix and ask whether the new test still passes. If it does,
```

## Materialization engine

A reusable `materialize_ablation(manifest, repo_root, ablation, out_dir) ->
list[Path]`:

1. Copy the real skill tree into `out_dir/<id>/` (reuse the `copy_skill_source`
   shape: SKILL.md + `references`/`scripts`/`assets`).
2. Apply the mechanism to the copy.
3. Run the gates (below). Refuse on any failure.
4. Return the ablated skill paths.

Requirements:

- **Parse, do not regex.** Frontmatter is parsed as YAML (multi-line `>`/`|`
  descriptions exist); the body is parsed with a CommonMark-aware sectioner. A
  `##` inside a ```` ``` ```` fence is code, not a heading — a fence-naive matcher
  mis-stops on it and orphans the block (demonstrated in the prototype). Anchor
  comments are HTML blocks; reference pointers are link nodes. The patch applier
  is a strict pure-python unified-diff applier (no new dependency); exact-context
  matching means a stale patch refuses to apply — that strictness *is* the
  drift detector.
- **Deterministic / idempotent.** No clocks or randomness; re-running yields
  byte-identical output. (Aligns with CF.3 re-grade idempotence.)
- **Multi-file aware.** Mechanisms may target `SKILL.md` or any bundled file.
- **Generic over frontmatter.** Field handling is data-driven against a
  discovery/runtime/metadata classification, not a hardcoded field list, so it
  survives spec additions.

Materialized trees are generated artifacts. They land under `--ablation-dir`
(default: beside the prepared tasks file) and should be git-ignored.

## Correctness gates

Run inside materialization; this is where the "evidence, not assertion" stance
lives. Any failure aborts the ablation with a specific message.

1. **Non-empty diff.** A byte-identical result is a silent no-op and fails. The
   materialized analog of *ablations are not evidence until run*.
2. **Layer integrity.** An `execution`/`on_demand` ablation must not touch
   discovery frontmatter (`name`, `description`, `when_to_use`) — otherwise it
   silently becomes a trigger experiment. Touching them is allowed only when
   `layer: discovery`.
3. **Spec-valid frontmatter.** The result must still parse and must not leave a
   required field empty. Removing `description` is only legal as a discovery
   ablation, never collateral damage.
4. **Patch applies cleanly.** Exact-context match; a drifted patch errors and
   forces a re-derive.
5. **Isolation report.** Record the diff stat; flag (warn, not fail) an ablation
   whose diff is implausibly large for its declared single component.

## Prepare and variant wiring

- `prepared_task_rows`: for a materialized `ablation:<id>` row, set `skill_paths`
  to the materialized tree (this also straightens the latent `old_skill` quirk
  where the row emits current paths and leans on prose).
- `variant_instruction`: materialized rows get the plain `with_skill`-style "use
  the loaded skill as-is" text. The "simulate removing X" prose remains only for
  `instruction_simulated`.
- Case selection by layer: `discovery` ablations run **only** against trigger
  cases; `execution`/`on_demand` continue to **skip** trigger cases
  (`prepared_task_rows:332`).
- Runner/export: `run_pi_smoke.py` and `safe_task_json` upload the materialized
  tree (they already copy whatever `skill_paths` resolves to) and stamp
  `mode: "materialized"`.

## Provenance and reporting

- Every `ablation:<id>` run record carries `{mode, layer, mechanism, target,
  diff_stat, skill_hash}`. `benchmark` never compares a materialized arm against
  a simulated one without labeling which is which.
- **Per-ablation regression view.** For each ablation on each discriminating
  case, report whether the `expected_regressions` actually appeared — i.e.
  whether the arm dropped below `with_skill`. This closes hypothesis → evidence
  and is the payload that makes an ablation "run".
- **Token-overhead tie-in.** Execution/on-demand ablations compose with the
  existing token-overhead signal (`vocabulary.md:84`): report tokens reclaimed
  by the removal alongside its lift delta, so a no-regression ablation reads as
  "costs N tokens, buys 0 lift → candidate for cut" (feeds the pruning work,
  `TODO.md` 1.9 / 2.10).
- Extend `negative_delta_cases` (`trace-aware-eval-spec.md:290`) to weight a
  materialized regression above a simulated one.

## Validation additions

`validate_manifest` (`skill_benchmark.py:255`), for each ablation:

- Keep `id` + `removed_component`.
- If `mechanism` is set: `layer` ∈ the enum; `target` matches the mechanism's
  required keys; referenced paths/patches exist and resolve under the manifest
  repo. The apply-and-diff gates run at materialize time (and under an opt-in
  `--check-ablations` for a dry run), not in plain `validate`, so `validate`
  stays file-surgery-free.
- Add to `audit-manifest` hints: an ablation with no discriminating case is a
  declared-but-unrunnable hypothesis; a `reference` ablation whose `path` is not
  pointed at by the body is dead on arrival.

## Phased TDD plan

1. **Schema + validation.** Parse `layer`/`mechanism`/`target`; validate shape
   and path existence. No materialization yet. Pure-function tests.
2. **Materializer — body mechanisms.** `section` (fence-aware), `anchor`,
   `list_item`, `patch`, with all gates. Meta-fixtures (CF.1 style): a fixture
   skill + each mechanism, asserting the diff removes the target and nothing
   else, and that re-run is byte-identical.
3. **Materializer — frontmatter + on-demand.** `frontmatter_field`, `reference`
   (pointer/content/both), `script`/`asset`. Layer-integrity gate tests.
4. **Prepare/variant wiring.** Rows point at the materialized tree; instruction
   switches; discovery→trigger / execution→non-trigger case routing.
5. **Export + provenance.** Jetty export uploads the tree and stamps
   `materialized`; run records carry full provenance.
6. **Reporting.** Per-ablation regression view + token-overhead column +
   materialized/simulated weighting.

## Test fixtures

A self-contained fixture skill with: multi-field frontmatter (discovery +
runtime fields), nested headings, a fenced code block containing a
heading-shaped line (the fence-awareness trap), an anchored span, a bundled
reference reached by a body link, and a script. Each mechanism gets a
should-materialize fixture and the gates get should-refuse fixtures
(empty diff, discovery field touched in execution layer, drifted patch,
missing reference pointer).

## Acceptance criteria

- A manifest with no `mechanism` behaves byte-for-byte as today.
- Each declared mechanism produces an ablated tree that passes all gates, and
  the four refusal fixtures each fail with a specific message.
- Materialization is deterministic across runs.
- A materialized `ablation:<id>` run is graded, labeled with provenance, and
  appears in the per-ablation regression view.
- No model call and no network in the materialize/grade path (CF.4).

## Open questions — verify, do not assume

- Exact context-loading thresholds (description listing truncation; retained
  tokens after compaction) are docs-stated; measure rather than trust, and keep
  them out of gate logic.
- `paths`-glob and nested-reference behavior are under-specified upstream; the
  `reference` mechanism handles one level and warns on deeper nesting.
- Whether to support multi-component ablations (remove two sections at once).
  Default: one component per ablation id; compose via separate ids until there
  is demand.

## Out of scope (deferred)

- Auto-generating ablation patches from a skill's section structure. The
  `audit-manifest` ablation-plan hints already suggest candidates; turning a
  suggestion into a materialized patch stays manual until the manual path is
  proven.
- Self-generated or model-rewritten skill variants as release proof — a research
  control later, consistent with `trace-aware-eval-spec.md`.
- Ablating runtime frontmatter (`model`, `effort`) as a *confound control* is
  enabled by the `frontmatter_field` mechanism, but the reporting that
  separates "model-driven lift" from "instruction-driven lift" is left to the
  per-model work (`TODO.md` 3.2).
