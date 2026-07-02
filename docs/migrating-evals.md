# Migrating evals between manifest versions

A versioned, agent-runnable guide. Each section states what changed, what
`skill-benchmark migrate` does automatically, and the ordered steps a human or
agent takes to finish the judgment calls. The governing rule throughout: every
new field is optional with a behavior-preserving default, so an unmigrated
manifest keeps grading identically — migration makes the defaults explicit and
opts into the new measurement, it never rescues a broken suite.

## Version 1 → 2

### What changed (all additive)

| Area | New surface | Default if absent |
|---|---|---|
| Severity (spec 2.2) | `severity: critical\|gate\|soft` (or `critical`/`gate`/`soft`/`atLeast` shorthands) per assertion | objective types are `gate`, judge/similarity are `soft` — exactly version-1 behavior |
| Oracle tiers (spec 1.7) | `oracle: strong\|demo\|live` per assertion | by type: deterministic text/process/efficiency `strong`, `script` `demo`, judge `live` |
| Graded judge shapes (spec 2.2) | `graded_dimensions` (anchored 1-5) and `dynamic_rubric` on judge assertions | plain binary verdict |
| Reference floors (spec 2.2) | `reference_score` (0-1) / `reference_graded_score` (1-5) per case | no floor |
| New assertion types | `golden_output`, `similarity` (ratio or opt-in embedding), `structured_output`, `tool_call`, `factuality` preset | n/a — new capabilities |
| Model axis (spec 2.1) | `prepare --models a,b,c`, model segment in run dirs | single-model layout unchanged |
| Multi-turn (spec 3.1) | `turns` on a case | single-shot unchanged |
| Datasets (spec 2.5/3.3) | `datasets` + case `template`; YAML manifests + `dataset_files` JSONL | plain cases unchanged |

### What `migrate` does automatically (mechanical)

```sh
skill-benchmark migrate evals/shared-benchmark.json --check   # dry run: diff + checklist, no writes
skill-benchmark migrate evals/shared-benchmark.json           # rewrite JSON in place, re-validate
```

- bumps `version` 1 → 2;
- stamps the default `severity` on every assertion that declares none;
- stamps the default `oracle` tier on every assertion that declares none;
- adds a `_migrate_todo: "graded? …"` marker beside every binary judge rubric;
- prints a unified diff and the judgment-call checklist (`--out-checklist` saves it as JSON);
- `--check` writes nothing. YAML manifests are never rewritten in place — apply the printed diff by hand.

### What is deliberately left to you (or your agent)

1. **Graded dimensions** — for each checklist entry `[graded dimensions]`,
   decide whether the binary judge rubric should become anchored
   `graded_dimensions` (`{name, scale: "1-5", rubric: "5 = …observable…; 1 = …"}`).
   Anchors must name observable behavior, not vibes. Leave binary deliberately
   where pass/fail is the honest measurement. Spec 2.2.
2. **Reference floors** — for each `[reference floor]` entry, optionally set
   `reference_score`/`reference_graded_score` so a regression below your
   accepted exemplar is flagged. Spec 2.2.
3. **Oracle honesty** — for each `[oracle tier]` entry, confirm the `script`
   oracle's stamped `demo` tier: promote to `strong` only for a verified
   rendered-artifact oracle (it builds/renders and inspects the result), or to
   `live` if it touches real resources. Spec 1.7.
4. **Critical prohibitions** — mark absorbing-barrier failure modes
   (`severity: "critical"`) sparingly, and pair every one with a
   negative/false-positive case proving the skill still does the reasonable
   thing (over-constraint backfires). Spec 2.2.

### The ordered agent runbook

1. `skill-benchmark migrate <manifest> --check` — read the diff and checklist.
2. `skill-benchmark migrate <manifest>` (JSON) or apply the diff (YAML).
3. Work the checklist top to bottom, editing the manifest; delete each
   `_migrate_todo` as you decide it.
4. `skill-benchmark validate <manifest>` — must pass.
5. Re-grade an existing run dir (`skill-benchmark benchmark … --runs <old runs>`)
   and confirm pass rates are identical to the pre-migration report — the
   defaults are behavior-preserving, so any drift means a judgment call
   changed semantics; review it.
6. Only then adopt the new measurement (graded dimensions, floors, `--strict`)
   in your next paid run.
