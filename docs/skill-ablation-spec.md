# Materialized Skill Ablation Spec

Status: implemented and tested end to end (phases 1–8) — the materialization engine and `materialize-ablations` CLI; Pi smoke, Pi trigger (`--ablation`), and Jetty (`export-jetty`, recursive tree upload) all consume the materialized tree; `prepare`/`export-jetty` gain `--ablation-dir`; ablation rows route by case population and carry provenance; and the benchmark report adds `ablation_regressions` distinguishing "score regressed" from assertion-level "expected regression confirmed". `validate --check-ablations`, the `invalid_skill` mode, isolation warnings, and `audit-manifest` ablation-hygiene hints are also implemented. Revised after the PR #20 review — it
corrects a false claim that runners already consume materialized paths,
separates component semantics from case routing, constrains manifest-controlled
filesystem paths, and pins down the removal-vs-substitution boundary and the
evidence a regression actually requires.

This closes the `TODO.md` item "Materialize true ablated skill files", discharges
the `LESSONS_LEARNED.md` "next improvement" note (2026-06-09, *Ablations are not
evidence until they are run*), and supplies the materialized `ablation:<id>` runs
that `docs/trace-aware-eval-spec.md` names as the release-grade baseline.

## Design principle

**An ablation is a hypothesis; a materialized ablation is the experiment.
Ablate along the skill format's seams, not along lines of text. Removal and
substitution are different experiments and must not be conflated.**

The harness produces a real, altered skill tree and the runners execute against
it. Removal logic lives in the harness once. But materialization is not free:
the runners and the Jetty exporter currently reconstruct skill files from the
manifest, so they must be refactored to consume the materialized tree (see
*Runner integration*). The earlier draft's claim that they "already copy
whatever `skill_paths` resolves to" was wrong.

## Grounding sources

External — the format under test (read before extending the schema; the field
list evolves, so handling is data-driven, never a hardcoded list):

- Agent Skills specification — `https://agentskills.io/specification` (defines `name` and a non-empty `description` as **required**).
- Claude Code skills docs & frontmatter reference — `https://code.claude.com/docs/en/skills`.
- Skill authoring best practices — `https://platform.claude.com/docs/en/agents-and-tools/agent-skills/best-practices`.

Internal — the contracts this builds on:

- `validate_manifest` ablation block (`skill_benchmark.py:255`) — today checks only non-empty `id` + `removed_component`.
- `variant_instruction` / `task_variants` / `prepared_task_rows` (`skill_benchmark.py:265`, `:302`, `:314`) — `skill_paths` is currently the manifest's, identical for every variant.
- `safe_task_json` / `build_jetty_payload` (`skill_benchmark.py:452`, `:486`) — Jetty export; ablation reads `task["skill_paths"]`, uploads flat files via `JettyClient.upload` → `read_bytes` (`:706`) with a basename-only `remote_path_hint`.
- `copy_skill_to_config` (`skill_benchmark.py:47`) and `copy_skill_source` (`run_pi_smoke.py:100`) — both collapse a file-valued path to `<dir>/SKILL.md`, `rmtree` the destination, and whitelist only `references`/`scripts`/`assets`.
- `run_pi_trigger_eval.py` — the autonomous-trigger runner; copies from the manifest and takes no variant input.

## Current state: instruction-simulated ablations

Every variant — including `ablation:<id>` — receives the same unmodified skill.
The runner copies the full real skill and the instruction asks the model to
"ignore component X" (`run_pi_smoke.py:161`). This is a prompt *about* the skill,
not a changed skill: weak evidence, because the model still sees the full text.
It stays supported as a fallback (the derived `instruction_simulated` mode; see
*Vocabulary additions*), but it is no longer how an ablation that declares a
removal behaves.

## A skill is a structured artifact: component class vs case population

Two orthogonal axes, kept separate (the earlier draft fused them into one
overloaded `layer`):

**Component semantic class** — what kind of thing the component is. Selects the
mechanism and the integrity rules.

| Class | Contents | Loaded / used | Notes |
|---|---|---|---|
| `discovery` | `name`, `description`, `when_to_use`, `paths`, `disable-model-invocation`, `user-invocable` | startup; governs activation | `name` + non-empty `description` are required by spec |
| `runtime` | `allowed-tools`, `disallowed-tools`, `model`, `effort`, `context`, `agent`, `shell`, `hooks` | when active; configures behavior | the legal home for `model`/`effort` ablations |
| `instructions` | `SKILL.md` body prose, sections, lists | on invocation | the bulk of execution guidance |
| `resource` | `references/`, `scripts/`, `assets/` files | on demand, if the body reaches them | pointer vs content are separable |
| `preprocess` | inline `` !`command` `` blocks | **before** the body reaches the model | executes at render time, not on demand |

**Case population** — *derived* from the classes present, not declared:

- `discovery` components → **trigger** cases (does the skill activate?).
- `runtime` / `instructions` / `resource` / `preprocess` components → **answer**
  cases (does behavior degrade once active?).

An ablation's population is the population of its components; the *layer
cohesion* gate forbids mixing the two populations. This gives `model`/`effort` a
defined class (`runtime`), routing (answer), and provenance slot — which the old
three-value `layer` enum could not express.

## Vocabulary additions

- **Materialized ablation** — an `ablation:<id>` whose skill files are a real,
  altered copy, produced by the harness. Contrast *instruction-simulated*.
- **Component** — one `{class, mechanism, target}` edit, scoped to one skill
  root. An ablation removes one or more components as a unit.
- **Component class** — `discovery | runtime | instructions | resource | preprocess`.
- **Case population** — `trigger | answer`, derived from the classes.
- **Mode** — *derived*, not declared: `materialized` iff the entry declares a
  removal (`mechanism`+`target` or `components`); otherwise `instruction_simulated`.
- **Skill root** — one entry of the manifest's `skill_paths`. Every component
  names the root it edits; roots are materialized separately and never merged.
- **Ablation provenance** — `{mode, population, skill_hash, isolation_warnings,
  components:[{class, mechanism, skill_root, target, removed_bytes}]}` on every
  materialized run.

## Manifest schema

The ablation entry gains an optional removal declaration in one of two forms:
single-component `mechanism` + `target`, or a `components` list. **Mode is
derived** — an entry with no removal declaration is `instruction_simulated`
(today's behavior, untouched); `instruction_simulated` is not a mechanism value.
Every component names a `skill_root` and carries a `class`.

```jsonc
{
  "id": "no-regression-proof",                 // unique, slug: ^[a-z0-9][a-z0-9-]*$
  "removed_component": "regression-proof requirement",   // human label
  "expected_regressions": [                     // structured; see Provenance
    { "summary": "Accepts weak tests that pass without the fix",
      "cases": ["pos-security-meaningless-test"],
      "assertions": ["detect-weak-test"] }
  ],
  "mechanism": "section",
  "class": "instructions",
  "target": { "skill_root": "skills/good-pr/SKILL.md", "heading": "## Regression-proof requirement" }
}
```

`target` shape per mechanism — `skill_root` is **required on every component**:

| Mechanism | Class | `target` (besides `skill_root`) | Operation |
|---|---|---|---|
| `frontmatter_field` | discovery / runtime | `{ "field": "allowed-tools" }` | delete one key |
| `section` | instructions | `{ "heading": "## …" }` | delete heading + body + nested subheadings |
| `anchor` | instructions | `{ "anchor": "no-scope-check" }` | delete span between `<!-- ablation:ID:start/end -->` |
| `list_item` | instructions | `{ "section": "## …", "contains": ["…"] }` | delete matching list items |
| `patch` | instructions / discovery | `{ "patch": "evals/ablations/<id>.patch" }` | **deletion-only** unified diff (no `+` lines) |
| `reference` | resource | `{ "path": "references/x.md", "remove": "pointer\|content\|both" }` | unlink (drop target, keep visible text) / delete file / both |
| `script` / `asset` | resource | `{ "path": "scripts/x.py" }` | delete a bundled file |
| `preprocess` | preprocess | `{ "contains": ["git diff"] }` | delete matching inline `` !`cmd` `` / ```` ```! ```` blocks |

## Mechanisms and granularity

Granularity is two-dimensional: **which class** × **how surgical**. Within
`instructions` the unit ladder is section → subsection → list item → sentence
(deletion-only patch). Worked before/after (from the prototype, on a
representative `good-pr` skill):

`section` — deletes the heading and its nested `###` children:

```diff
-## Regression-proof requirement
-For any bug-fix or security PR, require a test that **fails without the fix
-and passes with it**. …
 ## Severity and verdict
```

`frontmatter_field` — a structured seam prose ablation cannot reach:

```diff
-allowed-tools: Read, Grep, Bash(git diff:*)
```

`reference` with `remove: pointer` — **unlink**: drop the target, keep the
visible words, so no new prose is introduced (the file stays, undiscovered):

```diff
-examples live in [the severity guide](references/severity.md).
+examples live in the severity guide.
```

`patch` (deletion-only) — finest grain; delete one sentence:

```diff
-and passes with it**. A test that passes on the unpatched code proves nothing.
+and passes with it**.
```

## Removal versus substitution

An `ablation:<id>` is **removal-only**. Every mechanism produces a net deletion;
the *net-deletion* gate enforces it (a component must remove content and add
none). Specifically: a `patch` component's hunks may contain only context and
`-` lines; `reference` pointer removal unlinks rather than rewording.

Replacement-bearing edits — `replace_with`, `set`, or a patch with `+` lines —
are **substitution**, a different experiment (counterfactual / A-B, the granular
sibling of the existing `old_skill` variant). They are out of scope here and
tracked as the `swap:<id>` variant in `TODO.md`; the prototype's
`Mentally → Optionally` edit is a *swap* example, not an ablation. Keeping the
two apart is why an ablation cannot smuggle new instructions in through `patch`.

## Multi-component ablations

One ablation may remove several components, to test **joint** load-bearingness —
redundant guidance stated in two places, or a capability spanning seams — which
single removals cannot detect. Declare a `components` list; the single
`mechanism`/`target` form is sugar for a one-element list.

Rules:

- **One case population per ablation.** Components must not mix the `discovery`
  class with `runtime`/`instructions`/`resource`/`preprocess` (different case
  populations; a regression spanning both is unattributable). The answer-population
  classes may be freely combined. Enforced by *layer cohesion*.
- **Joint attribution only.** A multi-component ablation scores the cluster; pair
  it with single-component ablations to attribute the effect to a part.
- **Order-independent.** Components resolve against the original tree and must be
  pairwise disjoint, so declaration order never changes the result.

## Materialization engine

`materialize_ablation(manifest, repo_root, ablation, out_root) -> dict[skill_root, Path]`:

1. For each `skill_root` referenced by the ablation, copy that root's **complete
   directory** into a fresh temp dir — the whole tree, not a `SKILL.md` plus a
   three-directory whitelist, because the format permits arbitrary files. Roots
   are kept **separate** (keyed by their relative path), never merged into one
   `<skill_name>/SKILL.md`. This replaces `copy_skill_source` /
   `copy_skill_to_config`, which collapse and overwrite roots.
2. Resolve every component against the **original** copy of its named root to a
   concrete edit (a span → deletion). A `patch` resolves to the line range its
   hunks touch.
3. Require component edits pairwise disjoint within a root (gate); apply them in
   one pass per file, back to front so earlier offsets stay valid. Resolving
   against the original — never another component's output — makes the result
   order-independent and byte-deterministic.
4. Run the gates. Refuse on any failure.
5. Atomically rename the temp dir into `out_root/<id>/` (never `rmtree` a
   manifest-named destination). Return the per-root materialized paths.

Requirements: **parse, do not regex** (YAML frontmatter incl. multi-line
scalars; a CommonMark sectioner — a `##` inside a ``` fence is code, not a
heading; anchors are HTML blocks; pointers are link nodes; the patch applier is a
strict pure-python deletion-only unified-diff applier whose exact-context match
*is* the drift detector). **Deterministic / idempotent** (no clocks/randomness;
re-run is byte-identical). **Generic over frontmatter** (data-driven class map,
not a hardcoded field list).

## ID and path safety

Manifest content controls filesystem writes, so it is constrained:

- **Unique slug IDs.** Ablation `id` must be unique within the manifest and match
  `^[a-z0-9][a-z0-9-]*$`, so `ablation:<id>` is collision-free and path-safe.
  (Today `id` is only checked non-empty.)
- **No traversal.** Reject absolute paths and any `..` segment in `skill_root`,
  `target.path`, and `patch`.
- **Containment after symlink resolution.** Resolve real paths and verify each
  target is contained within its declared `skill_root`, and the root within the
  manifest repo. An ablation may never edit a file outside its skill root.
- **Atomic, non-destructive output.** Materialize into a fresh temp dir, then
  rename; never delete a pre-existing manifest-named path.

## Correctness gates

Run inside materialization; abort with a specific message on any failure.

1. **Net-deletion per component.** Every component must remove content and add
   none (deletion-only). A component that matches nothing fails the whole
   ablation — a stale target hidden in a cluster is a bug, not a silent pass.
2. **Component disjointness.** No two components edit overlapping regions of the
   same file; refuse, naming both.
3. **Layer cohesion.** No mixing the `discovery` case population with the answer
   population.
4. **Required-field preservation.** The result must remain a valid skill: `name`
   present, `description` present and non-empty. A discovery ablation *weakens*
   description/when_to_use content (e.g. drops a trigger phrase) but keeps the
   required fields. Emptying a required field is rejected here and only allowed
   under the separate *invalid-skill* mode.
5. **Patch applies cleanly, deletions only.** Exact-context match; reject hunks
   with `+` lines (those are swaps).
6. **Containment.** Path/ID safety above holds.
7. **Isolation report.** Record per-component `removed_bytes`; warn (not fail,
   collected in `isolation_warnings`) when one component removes an implausibly
   large fraction (>60%) of its file.

## Runner integration (required refactors)

Pointing the prepared row at a materialized tree is **not** sufficient; each
executor must be changed to consume it:

- **Pi smoke** (`run_pi_smoke.py:materialize_runtime_workspace`) rebuilds sources
  from `manifest["skill_paths"]`. It must instead copy the row's materialized
  per-root trees, preserving each root's structure.
- **Pi trigger** (`run_pi_trigger_eval.py:copy_skill_to_config`) is the
  autonomous-trigger adapter: `--ablation <id>` materializes a discovery ablation
  and mounts the altered tree so the eval measures whether it still loads. It
  rejects answer-population ablations. Discovery ablations are measured **only**
  here — the forced-load generic runners cannot observe autonomous loading.
- **Jetty** (`build_jetty_payload` + `JettyClient.upload`) uploads individual
  files with a basename-only `remote_path_hint` (duplicate `SKILL.md` basenames
  collide) and `read_bytes` per file (no recursion). It must recursively
  enumerate each materialized root preserving relative paths, and the runbook +
  `safe_task_json` must mount/read skill files for `ablation:*` (and later
  `swap:*`), not only `with_skill`.

A cross-runner test asserts the materialized content actually reaches the model
(not the original skill) for each runner.

## Prepare and variant wiring

- `prepared_task_rows`: a materialized `ablation:<id>` row carries the per-root
  materialized paths (a structured `skill_files` mapping, not a flat list), so
  runners can rebuild the tree. (This also fixes the latent `old_skill` quirk of
  emitting current paths and leaning on prose.)
- `variant_instruction`: materialized rows get the plain "use the loaded skill as
  is" text; the "simulate removing X" prose remains only for the
  instruction-simulated mode.
- Case routing follows the **derived population**: `prepared_task_rows` emits
  rows only for **answer-population** ablations (on non-trigger cases). Discovery
  (trigger-population) ablations are *not* emitted for the generic forced-load
  runners — they are measured by the autonomous-trigger adapter
  (`run_pi_trigger_eval.py --ablation`), which is the only place autonomous skill
  loading can be observed.

## Provenance and reporting

- Each run record carries the provenance object above (per-component class,
  skill_root, mechanism, removed_bytes; derived population; mode; skill_hash).
  `benchmark` never compares a materialized arm against a simulated one without
  labeling which is which.
- **Two distinct report claims, not one.** `expected_regressions` is structured
  (each names `cases` and `assertions`/rubric IDs). The report distinguishes:
  - **score regressed** — the arm's aggregate objective pass rate dropped (what
    the current reporting computes).
  - **expected regression confirmed** — the *named* assertion(s) on the *named*
    case(s) flipped pass→fail in the ablation arm.
  A score drop is necessary, not sufficient: an arm can lose points on an
  unrelated assertion, or show the named failure while its aggregate rate is
  unchanged. `build_ablation_regression_report` computes both from the graded
  results — it marks `expected_regression_confirmed` only when the named
  assertion(s) flip pass→fail, and otherwise reports just `score_regressed`.
- Token-overhead tie-in (`vocabulary.md:84`) and weighting materialized above
  simulated in `negative_delta_cases` (`trace-aware-eval-spec.md:290`) as before.

## Invalid-skill experiments (separate mode)

Removing a required field (`name`, or `description` entirely) produces a skill a
conformant client may reject outright — so a measured "trigger failure" would be
a parser/validation failure, not evidence the removed text was load-bearing.
These are a distinct, explicitly-flagged mode with client-specific
interpretation, reported separately and never as an ordinary trigger ablation.
Opt in with `"invalid_skill": true` on the ablation: the run is tagged
`mode: invalid_skill`, bypasses the required-field gate, and is flagged in the
`ablation_regressions` report.

## Validation additions

`validate_manifest` (`skill_benchmark.py:255`), per ablation:

- `id` unique and slug-formatted; keep `removed_component`.
- If a removal is declared: exactly one of `mechanism`+`target` or `components`;
  each component has a `class` in the enum, a `skill_root` that resolves to a
  manifest `skill_paths` entry, and a `target` matching its mechanism's keys;
  referenced paths/patches exist; **no absolute/`..` paths**; layer cohesion
  holds. The apply-time gates (net-deletion, disjointness, clean deletion-only
  patch, containment) run under an opt-in `--check-ablations` dry run, keeping
  plain `validate` file-surgery-free.
- `audit-manifest` hints: an ablation with no discriminating case; a `reference`
  ablation whose `path` is not pointed at by the body; an `expected_regressions`
  entry whose `cases`/`assertions` do not exist.

## Phased TDD plan

1. **Schema + validation + safety.** Parse `class`/`mechanism`/`skill_root`/
   `target`/`components`; unique-slug IDs; reject traversal; containment checks.
   Pure-function tests, no materialization.
2. **Full-root copy.** Replace the collapsing copiers with a complete-tree,
   multi-root, atomic-rename copy; tests for arbitrary files and two roots not
   overwriting.
3. **Materializer — instruction mechanisms.** `section` (fence-aware), `anchor`,
   `list_item`, deletion-only `patch`, with all gates. Meta-fixtures.
4. **Materializer — frontmatter / resource / preprocess.** `frontmatter_field`,
   `reference` (pointer-unlink/content/both), `script`/`asset`, inline command;
   required-field-preservation and net-deletion gate tests.
5. **Multi-component composition.** Resolve-against-original, disjointness, layer
   cohesion, back-to-front apply; redundant-guidance cluster + overlap-refusal
   fixtures.
6. **Runner integration.** Refactor Pi smoke, Pi trigger (add variant input),
   and Jetty (recursive upload, runbook mount) to consume the tree; cross-runner
   "materialized content actually reaches the model" test.
7. **Prepare/variant wiring + provenance.** Rows carry per-root paths; derived
   population routing; provenance recorded; export stamps `materialized`.
8. **Reporting.** Structured `expected_regressions`; "score regressed" vs
   "expected regression confirmed" (assertion-level deltas).

## Test fixtures

A self-contained fixture skill with **two skill roots**, multi-field frontmatter
(discovery + runtime), nested headings, a fenced code block with a heading-shaped
line (the fence trap), an anchored span, a body-linked reference, a script, and
an arbitrary extra file outside the three whitelisted dirs. Each mechanism gets a
should-materialize fixture (plus a multi-component cluster). Should-refuse
fixtures: empty/no-op component, overlapping components, discovery+answer mix, a
required field emptied in a normal ablation, a `patch` with `+` lines, a `..`
path, a duplicate ID, and a target outside its skill root.

## Acceptance criteria

- A manifest with no removal declaration behaves byte-for-byte as today.
- Each mechanism materializes through the gates; each refusal fixture fails with
  a specific message.
- Two skill roots are preserved separately; arbitrary files survive the copy.
- A multi-component ablation is order-independent and refuses on overlap, layer
  mix, or any `+`-bearing patch.
- For every runner (Pi smoke, Pi trigger, Jetty) the materialized content — not
  the original skill — provably reaches the model.
- Materialization is deterministic; no model/network in the materialize/grade
  path (CF.4); no write escapes `out_root/<id>`.
- A run is graded and labeled with provenance; "expected regression confirmed"
  is reported only when assertion-level evidence supports it.

## Open questions — verify, do not assume

- How single- and multi-component ablations are linked for attribution: shared
  `removed_component` label vs an explicit `attributes_to: [<id>…]`. Decide with
  the reporting view.
- Whether `skill_root` is keyed by the `skill_paths` string or a stable index if
  a manifest ever lists the same path twice.
- Context-loading thresholds (description truncation; retained tokens) are
  docs-stated; measure, and keep them out of gate logic.

## Out of scope (deferred)

- **Substitution / `swap:<id>`** — replacement-bearing component edits (the A-B
  counterfactual). Tracked in `TODO.md`; shares this materialization machinery
  but needs A-B (not lift-vs-baseline) reporting. This is where `model`/`effort`
  swaps and high-vs-low-freedom instruction swaps land.
- Auto-generating ablation patches from a skill's structure.
- Self-generated or model-rewritten skill variants as release proof.
- Reporting that separates model-driven from instruction-driven lift
  (`runtime`-class ablations are enabled; the attribution analysis is the
  per-model work, `TODO.md` 3.2).
