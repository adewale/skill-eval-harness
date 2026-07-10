# Correctness-by-construction audit

This audit records the invariants that are now enforced structurally at the harness's trust
boundaries. Raw provider/task/result dictionaries remain wire formats; code parses them into frozen,
closed domain values before it makes execution, grading, comparison, or causal claims.

## Construction rule

```text
untrusted wire data
  -> strict parser / smart constructor
  -> one closed domain variant
  -> derived booleans and numeric claims
  -> validated persisted row
```

The rule has three consequences:

1. mutually exclusive states are variants, not independently writable booleans;
2. identities and units are validated before values are paired or aggregated; and
3. persisted JSON is parsed again when it crosses back into the process.

## Autonomous-trigger observations

The trigger path has one internal state pipeline:

```text
subprocess bytes
  -> InvocationOutcome
  -> PiStream (Pi only)
  -> TriggerDetection
  -> TriggerObservation
  -> persisted JSON row
```

- `InvocationOutcome` is a frozen, closed state machine. Completion, timeout, spawn failure,
  process failure, provider failure, protocol failure, and harness failure are mutually exclusive.
- `PiStream` parses provider status and cumulative telemetry together. A complete process with a
  terminal provider error, malformed JSON stream, or no final non-retrying `agent_end` is failed.
  Failed streams cannot carry numeric usage or cost.
- `TriggerDetection.triggered` is derived from typed evidence. Unknown lifecycle events are not
  evidence, and callers cannot independently set a trigger boolean.
- `TriggerObservation.pass` is derived from invocation completeness, expected polarity, and
  detection. Incomplete observations cannot carry measured usage or cost.
- Persisted rows are parsed through `TriggerObservation.from_row` before live smoke trusts them.

Recorded, sanitized Pi JSONL fixtures cover success, an exit-zero provider error, retry then
success, and exhausted retries. Exhaustive finite-state and trigger truth tables guard the boundary.

## Paired experimental identity

`experimental_pairs.py` owns `ExperimentalPairKey(case_id, model, run_number, population)` and the
only constructor for `ExperimentalPair`.

- A complete pair contains exactly one `with_skill` and one `without_skill` arm with the same key.
- Missing arms and ineligible arms become explicit `BlockedExperimentalPair` values.
- A duplicate arm for one identity raises before aggregation; it can no longer overwrite an earlier
  row in a dictionary.
- Lift, graded lift, paired reliability, paired cost, token-overhead run discovery, slice/case
  comparative flags, readiness comparisons, and answer-population ablation confirmation consume the
  validated construction. Pairing diagnostics expose eligible/blocked counts and reasons.
- Telemetry comparisons add the stricter measurement-basis check (including provenance, currency,
  billing scope, and recorded revision fields) before a numeric delta or ratio exists.

This prevents cross-model, cross-repetition, and cross-population rows from contributing to a value
called paired. Older result rows without a model remain in the explicit unlabeled-model population;
a missing repetition identity is rejected rather than inferred from file order.

## Answer-runner outcomes

`runner_contracts.py` replaces the mutable answer-runner outcome bag with the frozen union:

```text
Completed | TimedOut | SpawnFailed | ProviderFailed
```

`OutcomeContext` owns the closed provider enum, recursively freezes JSON-shaped context, and
validates finite non-negative elapsed time, usage, and cost. `Completed` requires a non-empty final
answer; raw trace bytes are never promoted to candidate output. Timeout return code 124, spawn return code 127, normal completion return code 0,
and provider failure are mutually exclusive. `write_runner_outcome` exhaustively adapts the union to
artifacts; backends cannot repair or mutate status booleans while writing files. `RunnerOutcome`
remains only as a strict compatibility factory that constructs one of the four variants.

## Imported judge verdicts

`judge_verdict.py` parses judge rows into one of:

```text
BooleanVerdict | ScoredVerdict | DimensionVerdict | DynamicVerdict | ConsensusVerdict
```

- `passed` is a real JSON boolean; strings and integer truthiness are rejected.
- Scored verdicts derive pass from finite `score >= threshold` and reject a contradictory stored
  boolean.
- Dimension verdicts validate non-empty names, the 1–5 score range, normalized aggregate, and
  threshold; when merged against an assertion, the supplied names must exactly match its declared
  dimension set.
- Dynamic verdicts require uniquely named boolean criteria, a valid minimum, and an aggregate that
  agrees with the criteria.
- Stored result loading rejects missing, conflicting, and duplicate task IDs. Repetition/panel
  merging requires one task identity; panel models must be non-empty and unique. Consensus is
  serialized as its own verdict kind.
- Provider output that violates semantic invariants is retained only as diagnostic raw payload; the
  stored verdict is one valid failed boolean verdict.

## Draft versus executable tasks

`PreparedTaskDraft` is the permissive planning value. It can render or inspect a partial task but
cannot be passed to a runner. `PreparedTaskDraft.validate()` / `PreparedTask.from_row()` is the only
transition to executable `PreparedTask`.

The executable type requires non-empty identifiers, a closed split and execution variant, an
explicit positive integer repetition, list-of-string path fields, a safe non-root relative run
directory, no skill paths on `without_skill`, and matching typed ablation provenance/tree identity. Runner boundaries convert
constructor failures to explicit CLI input errors instead of starting a subprocess. The author-facing
case `kind` remains descriptive; the closed execution population is derived separately as answer or
trigger.

## Jetty lifecycle

`jetty_contracts.py` maps provider aliases once into the closed lifecycle:

```text
Queued | Running | Succeeded | Failed | TimedOut | ProtocolInvalid
```

The poller, executor, importer, trace writer, and normalized metadata consume that state. Timeout is
not an ordinary provider failure, and success requires a non-empty trajectory identity. Unknown/missing states and a stored lifecycle discriminator that
conflicts with the compatibility `status` field become `ProtocolInvalid`. A completed trajectory is
successful only when it also contains `output.md`; completed-without-output therefore fails closed as
a protocol error and cannot receive return code zero.

Dry-run payload generation is planning, not a Jetty execution lifecycle. Non-executable submitted
payloads are protocol-invalid rather than ordinary provider failures.

## Normalized trace-event lifecycle

`trace_contracts.py` owns `EventState`:

```text
COMPLETED | IN_PROGRESS | FAILED | UNKNOWN
```

`parse_event_state` records whether state came from provider status, an intrinsically terminal/start
event kind, explicit legacy adaptation, or remained unknown. Missing/misspelled status is never
optimistically completed. Command grading, tool/file counts, skill-invocation metrics, and retry
logic consume completed events only. Start/end fixture pairs prove a real operation counts once;
unknown lifecycle records cannot become phantom tool calls.

## Ablation provenance vocabulary

Answer-population ablation confirmation uses the same exact case/model/repetition pairs, requires
symmetric named-assertion coverage, and applies a paired sign-flip test to per-pair score deltas.
Fewer than six unanimous matched pairs cannot clear the two-sided p≤0.05 floor.

`ablation_model.py` closes provenance over `AblationMode`, `Population`, `ComponentClass`, and
`Mechanism`. Strict wire parsers reject unknown strings, booleans masquerading as scalar values,
missing required fields, unsafe/non-slug identifiers, unedited materialized trees, mixed component
populations, and population/component contradictions.
Mode-specific constructors preserve the distinction between materialized, instruction-simulated,
and invalid-skill experiments. `MaterializedArm` still requires a genuinely edited tree and matching
provenance, so a canonical tree cannot be labeled as a materialized removal.

## Test proof

The suite uses four complementary proof styles:

- exhaustive state/truth tables for finite lifecycle and verdict combinations;
- model-gap tests that directly attempt contradictory constructors;
- sanitized provider-shaped fixtures at external protocol boundaries; and
- integration tests that feed mismatched models/repetitions, duplicates, missing arms, malformed
  verdicts, incomplete traces, and completed-without-output Jetty records through real consumers.

Live provider checks remain explicit `--live`; deterministic CI does not require credentials or
network access.

## Residual risks

These constructions make the represented states stronger; they do not invent evidence a provider or
legacy artifact never recorded. In particular:

- legacy rows may lack revision/configuration provenance even when their case/model/run key is
  present; telemetry comparison remains blocked when its required basis is absent;
- Jetty aliases and response shapes still need token-backed live validation before production claims;
- `RunnerOutcome` is retained as a compatibility factory, so new code should construct the explicit
  union variants directly; and
- provider payload dictionaries are preserved for diagnostics, but no downstream decision should
  bypass the typed adapter to read them directly.
