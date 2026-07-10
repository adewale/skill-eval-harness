# Telemetry availability and comparability

> **Status:** proposed implementation plan. This supersedes the informal rule that
> missing telemetry is “marked, never zero” with a complete measurement contract
> for producers, artifacts, reports, comparisons, exports, and budget decisions.
>
> **Related:** [`architecture.md`](architecture.md),
> [`trace-aware-eval-spec.md`](trace-aware-eval-spec.md),
> [`agent-backend-interface-spec.md`](agent-backend-interface-spec.md), and
> [`agent-cli-control-plane.md`](agent-cli-control-plane.md).

## Why this exists

The current per-run normalizers correctly distinguish a missing cost or token value
from a measured zero. Some downstream folds do not: an empty sum, a display fallback,
or `value or 0` can turn unknown telemetry into free spend, zero latency, or a valid
looking efficiency result. The same risk applies beyond dollars: trace-derived counts,
artifact evidence, durations, grades, estimates, rankings, budgets, and exported
reports all need to distinguish **observed zero** from **not observed**.

The goal is not to make every field optional. It is to make each observed quantity and
each claim derived from one explicit about what evidence supports it.

## Goals and non-goals

### Goals

1. Preserve a real zero exactly as an available observation.
2. Represent unavailable, not-applicable, partial, and blocked states explicitly in
   machine-readable artifacts and human-readable reports.
3. Make invalid paired deltas and lift-per-unit ratios unconstructable in the typed
   interior.
4. Centralize parsing, provenance, aggregation, comparison, and rendering so runners
   and reports cannot drift.
5. Keep quality scoring, execution failure, and telemetry availability as separate
   dimensions.
6. Read legacy run directories without inventing provenance or comparability.

### Non-goals

- Infer telemetry that a provider did not expose.
- Treat unavailable telemetry as an execution failure or a failed grade.
- Mix answer, trigger, judge, and static populations just because their units match.
- Add live provider calls to deterministic CI.
- Redesign objective or qualitative scoring except where a derived comparison must
  disclose that an arm is unscorable.

## Canonical domain model

`telemetry.py` will own frozen domain objects, smart constructors, the wire parser,
legacy adaptation, aggregation, comparison, and presentation helpers. Raw JSON is
accepted only at the boundary and is converted before the rest of the harness uses it.

```text
Measurement[T] =
    Available(value, provenance, basis)
  | Unavailable(reason)
  | NotApplicable(reason)

Aggregate[T] =
    Complete(value, observed_count)
  | Partial(known_subtotal_or_stats, observed_count, unavailable_count, reason_counts)
  | Unavailable(unavailable_count, reason_counts)
  | NotApplicable(reason)

Comparison[T] =
    Comparable(value, basis)
  | Blocked(reason, left_state, right_state)
```

### Measurement rules

- `Available` is the **only** state allowed to carry a value. `0` is valid there.
- `Unavailable` means the measurement could have existed but was not observed, parsed,
  or trusted. It has a stable reason such as `runner_does_not_report_cost`,
  `trace_absent`, `malformed_provider_value`, or `legacy_field_absent`.
- `NotApplicable` means the measurement does not conceptually apply, such as monetary
  cost for an offline stub. It is not a zero-dollar run.
- Provenance applies only to available values: `provider_reported`,
  `trace_normalized`, `price_table_estimated`, `estimated`, or `legacy_unverified`.
  `missing` is no longer provenance; it is an unavailable state.
- Money is exact (`Decimal` internally and a canonical decimal string on the wire),
  non-negative, and always carries an ISO currency. A non-USD amount must not be
  called `cost_usd`.
- Token/count values and milliseconds are non-negative integers. Rates are finite and
  within `[0, 1]` when available.
- A count or boolean derived from a trace is available only when the observation is
  complete. A completed trace with zero tool calls is different from no trace.

### Basis and population

An available measurement records enough basis to determine whether arithmetic is
meaningful: population (`answer`, `trigger`, `judge`, or `static`), case, repetition,
variant, runner/adapter, provider, model/configuration fingerprint where available,
skill/manifest revision, billing scope, currency, pricing model/version, trace
aggregation mode, and artifact schema version.

A comparison policy declares which dimensions may differ. A normal with/without
experiment may intentionally differ in variant and materialized skill tree, but not in
case, repetition, model configuration, billing scope, or population. This avoids both
false matches and an overly rigid raw-dictionary equality check.

## Scope

| Surface | Required treatment |
|---|---|
| Token telemetry | Input/output/cache/reasoning/total usage, source, and trace aggregation mode. |
| Cost telemetry | Parts, total, currency, billing scope, price/version, and provenance. |
| Runtime telemetry | Duration, command/tool/file/error/retry counts, and observation completeness. |
| Artifact evidence | Raw trace, events, metrics, parser status, and evidence availability. |
| Run state | Completion, nonzero exit, timeout, no output, and observation completeness remain outcome facts, not zero-valued metrics. |
| Grades and rates | Valid zero, no applicable assertions, and unscorable arms remain distinct. |
| Static estimates | Profiled skill/reference tokens are `static` estimates; they may be shown beside runtime values but never merged with them. |
| Derived claims | Totals, statistics, rankings, deltas, ratios, trends, audit findings, and budgets use typed aggregates/comparisons. |
| Presentations | JSON, Markdown, JUnit, GitHub summaries, Anthropic export, and the viewer render availability instead of coercing it. |

## Aggregation and comparison policy

### Aggregates

- A fully observed set produces `Complete(value)`; all observed zero values produce
  `Complete(0)`, not unavailable.
- A mixed set produces `Partial(known_subtotal, …)`. The numeric member is explicitly
  named `known_subtotal`, never `total`.
- An all-unavailable set produces `Unavailable`, not zero or an empty statistic that
  callers can accidentally re-sum.
- Aggregation groups compatible basis buckets. Different currencies, populations, or
  billing scopes stay separate; no implicit foreign-exchange conversion occurs.
- Unknown values are excluded from spend rankings and marked unranked, rather than
  sorted as the cheapest spend.

### Deltas and ratios

Raw deltas may be available when two compatible values exist, including a negative
cost delta (a saving). A ratio is stricter:

```text
LiftPerUnit.create(pair) succeeds only when:
  - both arms are scorable;
  - numerator and denominator observations are available and comparable;
  - the denominator is strictly positive; and
  - the declared pair policy accepts both bases.
```

Otherwise it returns `Blocked`, with reasons including `missing_left`, `missing_right`,
`not_applicable`, `currency_mismatch`, `population_mismatch`, `pair_key_mismatch`,
`basis_mismatch`, `provenance_mismatch`, `unscorable_arm`,
`non_positive_denominator`, and `insufficient_sample`.

`objective_lift_per_dollar` and `objective_lift_per_1k_total_tokens` are constructed
only through this API. A cost saving or zero incremental cost can be reported as a
delta but cannot masquerade as an efficiency ratio.

## Architecture and ownership

### New owner: `telemetry.py`

It owns:

1. domain types and validation;
2. aliases and provider/trace precedence;
3. parsing and serialization of the schema-versioned telemetry envelope;
4. legacy v1/v2 adaptation;
5. compatible aggregation and statistics;
6. pair matching, comparison, and ratio construction; and
7. JSON/Markdown display helpers.

`skill_benchmark.py`, `run_pi_trigger_eval.py`, and `run_trigger_matrix.py` become
consumers of this API. Direct reads of `usage_normalized`, `cost_normalized`,
`cost_usd`, `total_tokens`, and telemetry-specific `metric_number(... ) or 0` fallbacks
outside the boundary adapter are removed.

### Registries

Two registries make complete application enforceable:

- **Signal registry:** unit, applicability, allowed provenance, required basis fields,
  and display policy for every telemetry signal.
- **Surface registry:** every CLI/backend/export is declared as a telemetry producer,
  consumer, presentation surface, pass-through, or telemetry-free command.

Tests derive expected coverage from argparse/backend registries, so adding a new CLI or
agent without a telemetry decision fails deterministically.

### Artifacts

New runs write `telemetry_schema_version: 3` and one canonical telemetry envelope into
both `metadata.json` and `metrics.json`; raw provider data remains preserved for audit.
A writer records precedence and parser errors as data. Metadata/metrics must agree on
the normalized envelope.

## Implementation sequence

### Phase 0 — decisions, inventory, and characterization

- Publish this contract and decide supported currencies, FX policy, provenance
  equivalence rules, required basis fields, and deprecation timing.
- Inventory all raw telemetry reads, empty sums, `or 0` fallbacks, rank keys, and
  inline delta/ratio arithmetic.
- Capture reviewed golden fixtures for complete legacy inputs only. Do not characterize
  existing unknown-to-zero behavior as compatibility.

### Phase 1 — typed boundary and regression tests

- Add `telemetry.py`, schema v3, smart constructors, legacy parser, aggregate types,
  comparison policy, and presentation helpers.
- Add focused failing tests for each known defect before changing behavior.
- Retain temporary compatibility re-exports while producer and consumer paths migrate.

### Phase 2 — migrate every producer

Route these paths through the canonical artifact writer:

- `run-agent`, `run-claude`, `run-codex`, `run-subagent`, and `run-jetty`;
- `import-jetty-results` and `import-trace`;
- native/shell/consensus judge paths;
- `skill-pi-trigger-eval` and `skill-trigger-matrix`.

`RunnerOutcome`, judge outcomes, and agent capabilities carry structured telemetry and
basis rather than loose `float | None` cost fields and provider dictionaries. The
capability registry describes support per signal, including why a signal is unavailable.

### Phase 3 — migrate every consumer and derived claim

Replace hand-written folds in benchmark reports, aggregate reports, suite cost ledgers,
variant summaries, audit findings, historical estimates, trends, exports, and pairwise
analysis. In particular migrate:

- `benchmark`, `aggregate`, `cost-summary`, and `token-overhead`;
- `audit-manifest`, `suite-run`, and `trend`;
- `grade`, `report`, `export-anthropic`, and `render-viewer`;
- paired quality/token/cost deltas, lift-per-unit metrics, rankings, and budget gates.

Quality populations remain separate from operational spend populations, but both report
coverage and basis. A dollar budget fails closed when compatible complete history is not
available.

### Phase 4 — compatibility, documentation, and removal of bypasses

- Add `migrate-telemetry --check|--write` (or an explicit telemetry mode on `migrate`)
  with atomic writes, dry-run output, backups, and idempotence.
- Read old artifacts through the adapter. Legacy numeric values are
  `legacy_unverified` and are not eligible for causal ratios unless repaired with a
  trustworthy basis.
- Update command help, examples, vocabulary, architecture, backend interface docs, and
  user journeys. Document `0`, unavailable, N/A, partial, and blocked rendering.
- Remove duplicate downstream missing/zero checks only after the boundary and its tests
  prove the invariant.

## CLI matrix

| Surface | Result |
|---|---|
| Answer/import runners | Emit canonical availability, provenance, and basis data; no fabricated zero defaults. |
| Trigger runners | Record whether observation is complete; absence of a trace cannot become zero tokens, zero tools, or a false negative trigger result. |
| Judge runners | Keep judge spend/population separate and expose unavailable shell-wrapper telemetry honestly. |
| `benchmark`, `aggregate`, `cost-summary` | Emit complete/partial/unavailable compatible buckets rather than ambiguous totals. |
| `token-overhead` | Keep cost and token channels independent; expose pair-level blocked reasons and eligible counts. |
| `audit-manifest`, `suite-run`, `trend` | Unknown/incompatible history cannot pass thresholds, rank cheaply, or produce a dollar estimate. |
| Exports/viewer | Omit numeric fields where external schemas require it and add explicit availability annotations; never substitute zero. |
| `profile-skill` | Clearly label static estimates and prevent mixing with runtime spend. |

## Test strategy

This work follows the techniques in
[`adewale/testing-best-practices`](https://github.com/adewale/testing-best-practices).
Tests favor real temporary run trees and provider-shaped fixtures over mocks of the
telemetry helpers.

| Technique | Application |
|---|---|
| Red-green-refactor | A regression test first for every zero-coercion or invalid-ratio path. |
| Characterization/golden fixtures | Preserve valid legacy complete-input behavior while approving intentional partial/unavailable output changes. |
| Correctness by construction | Smart-constructor model-gap tests reject unavailable-with-value, negative/non-finite values, incomplete basis, and invalid ratio construction. |
| Property-based tests | Parser never crashes on arbitrary JSON; encode/decode round-trips; unavailable rows do not change a known subtotal; ratios cannot exist with invalid denominators. |
| Exhaustive tests | Enumerate availability × provenance × compatibility × denominator sign × scorable state. |
| Mathematical properties | Compatible exact-money aggregation is permutation-invariant, associative, and commutative; incompatible buckets cannot aggregate. |
| Contract fixtures | Redacted Claude/Codex/Pi/Jetty/judge streams cover zero, absent, malformed, cumulative, and schema-drift cases without live CI. |
| CLI E2E/smoke | Local fake-runner workflows exercise every producer and changed consumer against real files. |
| Documentation-code sync | Command/backend/signal registries must agree with the canonical contract and command docs. |
| Mutation-style gap analysis | Nightly targeted mutations remove guards, alter `> 0`, swap arms, ignore basis/currency, or coerce unavailable to zero. |

The fast suite must remain deterministic: no live credentials, sleeps, wall-clock
assertions, weak truthy-only oracles, or mock-only integration tests.

## Acceptance criteria

The implementation is complete only when:

1. no telemetry consumer bypasses the canonical parser/typed domain;
2. every registered producer writes a valid v3 envelope and metadata/metrics agree;
3. every CLI/backend/export is represented in the surface registry;
4. measured zero, unavailable, not-applicable, partial, and blocked states render
   differently in JSON and human-facing output;
5. no aggregate labels a partial numeric as a total;
6. every ratio carries eligible/blocked counts and stable blocked reasons;
7. unknown/incompatible values cannot affect rankings, audits, trends, or budget passes
   as if they were zero;
8. legacy artifacts remain readable without acquiring false provenance; and
9. property, fixture-contract, CLI E2E, doc-sync, and targeted mutation gates pass.

## Follow-on: cross-lab CLI wrappers

The same pattern applies beyond telemetry. Native wrappers around Claude, Codex, Gemini,
Vibe, Jetty, and future lab CLIs should use explicit availability/capability and
comparability contracts for prompt transport, final-answer channels, schema support,
tool policy, configuration isolation, skill discovery, trace completeness, failure
semantics, and telemetry.

The aim is not a lowest-common-denominator wrapper: thin provider adapters retain their
native controls, while a typed shared contract makes unsupported or incomparable surfaces
explicit. This extends the control-plane approach in
[`agent-cli-control-plane.md`](agent-cli-control-plane.md) and is tracked in
[`TODO.md`](../TODO.md).
