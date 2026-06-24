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
- **Removal mechanism** — how a single component's alteration is produced
  (`frontmatter_field`, `section`, `anchor`, `list_item`, `patch`, `reference`,
  `script`, `asset`, `instruction_simulated`).
- **Component** — one `{mechanism, target}` removal. An ablation removes one or
  more components as a unit; multiple components test *joint* load-bearingness.
- **Ablation provenance** — the recorded `{mode, layer, components, diff_stat,
  skill_hash}` on every `ablation:<id>` run, so reports never conflate a
  materialized regression with a simulated one.

## Manifest schema

The ablation entry gains an optional `layer` plus a removal declaration in one
of two forms: the single-component `mechanism` + `target` shown below, or a
`components` list (see *Multi-component ablations*). Back-compat: an entry with
no removal declaration keeps today's behavior exactly — `instruction_simulated`,
execution layer. Existing manifests are untouched.

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

## Multi-component ablations

A single ablation may remove more than one component, to test **joint**
load-bearingness — the interaction single removals cannot see. Two cases it is
for:

- **Redundant guidance.** The same rule stated in the body *and* a reference:
  removing either alone regresses nothing because the other still carries it, so
  both single-component ablations read as "redundant/unclear" and miss it;
  removing both reveals the rule was load-bearing all along.
- **A capability that spans seams.** Instructions, a worked example, and a helper
  script in three places; ablate the cluster to measure the capability rather
  than a fragment.

Declare components as a list; the single-component `mechanism`/`target` form is
exactly sugar for a one-element list.

```jsonc
{
  "id": "no-test-discipline",
  "removed_component": "test-strength guidance (body section + checklist bullet + its reference)",
  "expected_regressions": ["Accepts weak tests that pass without the fix"],
  "layer": "execution",
  "components": [
    { "mechanism": "section",   "target": { "heading": "## Regression-proof requirement" } },
    { "mechanism": "list_item", "target": { "section": "## Review checklist", "contains": ["fail on the pre-change code"] } },
    { "mechanism": "reference", "target": { "path": "references/severity.md", "remove": "both" } }
  ]
}
```

Rules:

- **One case population per ablation.** Components may not mix `discovery` with
  `execution`/`on_demand`: the first changes triggering (measured on trigger
  cases), the others change post-trigger behavior (measured on answer cases), and
  a regression spanning both is unattributable. `execution` and `on_demand`
  components may be combined — they share the answer-case population. Enforced by
  the *layer cohesion* gate.
- **Joint attribution only.** A multi-component ablation scores the cluster, not
  its parts. To learn which part carries the effect, also declare the
  single-component ablations and compare.
- **Order-independent.** Components resolve against the original skill and must be
  pairwise disjoint, so declaration order never changes the result (see
  *Materialization engine*).

## Materialization engine

A reusable `materialize_ablation(manifest, repo_root, ablation, out_dir) ->
list[Path]`:

1. Copy the real skill tree into `out_dir/<id>/` (reuse the `copy_skill_source`
   shape: SKILL.md + `references`/`scripts`/`assets`).
2. Resolve every declared component independently against the **original** copy
   to a concrete edit (a span → replacement, per file). A `patch` component
   resolves to the line range its hunks touch.
3. Require the component edits pairwise disjoint (gate), then apply them in one
   pass per file, processing spans back to front so earlier offsets stay valid.
   Resolving against the original — never against another component's output —
   makes the result order-independent and byte-deterministic.
4. Run the gates (below). Refuse on any failure.
5. Return the ablated skill paths.

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

1. **Per-component effect.** The combined diff must be non-empty *and every
   declared component must remove something*. A component that matches nothing
   fails the whole ablation — a stale target hidden inside an otherwise-effective
   cluster is a manifest bug, not a silent pass. (Materialized analog of
   *ablations are not evidence until run*.)
2. **Component disjointness.** No two components may edit overlapping regions of
   the same file; overlap is ambiguous — refuse, naming both components.
3. **Layer cohesion.** An ablation must not mix `discovery` components with
   `execution`/`on_demand` components: different case populations, unattributable
   regression. See *Multi-component ablations*.
4. **Layer integrity.** An `execution`/`on_demand` ablation must not touch
   discovery frontmatter (`name`, `description`, `when_to_use`) — otherwise it
   silently becomes a trigger experiment. Touching them is allowed only when
   `layer: discovery`.
5. **Spec-valid frontmatter.** The result must still parse and must not leave a
   required field empty. Removing `description` is only legal as a discovery
   ablation, never collateral damage.
6. **Patch applies cleanly.** Exact-context match; a drifted patch errors and
   forces a re-derive.
7. **Isolation report.** Record the diff stat; flag (warn, not fail) an ablation
   whose diff is implausibly large for its declared components.

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

- Every `ablation:<id>` run record carries `{mode, layer, components, diff_stat,
  skill_hash}` (a single-component ablation records a one-element `components`).
  `benchmark` never compares a materialized arm against a simulated one without
  labeling which is which.
- **Per-ablation regression view.** For each ablation on each discriminating
  case, report whether the `expected_regressions` actually appeared — i.e.
  whether the arm dropped below `with_skill`. This closes hypothesis → evidence
  and is the payload that makes an ablation "run". A multi-component arm is
  scored as one unit (joint effect); pair it with single-component ablations to
  attribute the effect to a part.
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
- If a removal is declared: exactly one of `mechanism`+`target` or `components`
  is present; `layer` ∈ the enum; each component's `target` matches its
  mechanism's required keys; referenced paths/patches exist and resolve under the
  manifest repo; layer-cohesion holds across components. The apply-and-diff gates
  (per-component effect, disjointness, clean patch) run at materialize time (and
  under an opt-in `--check-ablations` dry run), not in plain `validate`, so
  `validate` stays file-surgery-free.
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
4. **Multi-component composition.** Resolve-against-original, the disjointness
   and layer-cohesion gates, back-to-front apply. Fixtures: a redundant-guidance
   cluster that regresses only when all parts are removed, and an overlapping
   pair that must refuse.
5. **Prepare/variant wiring.** Rows point at the materialized tree; instruction
   switches; discovery→trigger / execution→non-trigger case routing.
6. **Export + provenance.** Jetty export uploads the tree and stamps
   `materialized`; run records carry `components` + full provenance.
7. **Reporting.** Per-ablation regression view + token-overhead column +
   materialized/simulated weighting.

## Test fixtures

A self-contained fixture skill with: multi-field frontmatter (discovery +
runtime fields), nested headings, a fenced code block containing a
heading-shaped line (the fence-awareness trap), an anchored span, a bundled
reference reached by a body link, and a script. Each mechanism gets a
should-materialize fixture (plus a multi-component cluster), and the gates get
should-refuse fixtures: empty diff, a no-op component inside a cluster,
overlapping components, a discovery+execution mix, a discovery field touched in
an execution-layer ablation, a drifted patch, and a missing reference pointer.

## Acceptance criteria

- A manifest with no removal declaration behaves byte-for-byte as today.
- Each declared mechanism produces an ablated tree that passes all gates, and
  each refusal fixture fails with a specific message.
- A multi-component ablation removes all its components into one materialized
  tree, is order-independent, and refuses on overlap or a discovery/non-discovery
  layer mix.
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
- How single- and multi-component ablations are linked for attribution in the
  report. The draft pairs them by a shared `removed_component` label; an explicit
  `attributes_to: [<id>…]` field may be cleaner. Decide when the reporting view
  lands.

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
