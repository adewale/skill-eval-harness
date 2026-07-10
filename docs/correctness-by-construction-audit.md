# Correctness-by-construction audit

This audit distinguishes invariants now enforced structurally from remaining places where
free-form dictionaries or strings can still represent contradictory states.

## Applied to autonomous-trigger evaluation

The trigger path now has one internal state pipeline:

```text
subprocess bytes
  -> InvocationOutcome
  -> PiStream (Pi only)
  -> TriggerDetection
  -> TriggerObservation
  -> persisted JSON row
```

- `InvocationOutcome` is a frozen, closed state machine. Completion, timeout, spawn failure,
  process failure, provider failure, and harness failure are mutually exclusive. A non-zero
  completion requires the explicit bounded-agent-window transition.
- `PiStream` parses provider status and cumulative telemetry together. A complete process with
  a terminal provider error, malformed JSON stream, or no terminal agent event becomes a failed
  observation. Failed streams cannot carry numeric usage or cost.
- `TriggerDetection.triggered` is derived from typed evidence. Unknown lifecycle events are not
  evidence, and callers cannot independently set a trigger boolean.
- `TriggerObservation.pass` is derived from invocation completeness, expected polarity, and
  detection. Incomplete observations are structurally forbidden from carrying measured usage or
  cost.
- Persisted rows are parsed back through `TriggerObservation.from_row` before the live smoke trusts
  them. This re-establishes the internal contract at the disk boundary.
- Smoke targets and default models live in the capability registry. Blank environment overrides
  resolve to the registered fallback instead of creating an empty model identifier.

Recorded, sanitized Pi JSONL fixtures cover successful cumulative lifecycle events and an exit-zero
terminal provider error. The finite invocation-state and trigger truth tables are exhaustively tested.

## Next candidates

### P0: paired experimental identity

`build_paired_summary`, `paired_case_rates`, and ablation regression aggregation still average arms
before proving that the same case, model, repetition, population, and skill revision exist on both
sides. Introduce a validated `ExperimentalPairKey` and a discriminated result such as
`CompletePair | MissingLeft | MissingRight | DuplicateIdentity | PopulationMismatch`. Compute lift,
reliability, and causal confirmation from `CompletePair` values only.

**Illegal state today:** two different models or disjoint repetition sets can contribute to a value
called “paired.”

### P0: answer-runner outcomes

`RunnerOutcome` and the answer backends still use a mutable optional-field bag. Replace it with a
frozen union such as `Completed | TimedOut | SpawnFailed | ProviderFailed`, a closed provider enum,
and typed telemetry. Make artifact writing consume that union without mutating it.

**Illegal state today:** timeout with return code zero, success with both answer and error, unknown
provider falling through a known-provider failure policy, or non-finite elapsed/cost values.

### P1: imported judge verdicts

`load_judge_results` and `judge_verdict_passed` consume untyped rows and use truthiness in places.
Add strict discriminated verdicts for boolean, scored, dimension-scored, and dynamic-rubric results;
reject duplicate task IDs and contradictory score/threshold/pass fields.

**Illegal state today:** `{"passed": "false"}` can behave as passing, and duplicate contradictory
verdicts can resolve by file order.

### P1: prepared execution tasks

`PreparedTask.from_row` currently permits partial prompt fixtures and executable tasks through the
same type. Split `PreparedTaskDraft` from a validated `PreparedTask`; require closed variant/split/kind
values, a positive repetition, non-empty identifiers, and a safe relative run path.

**Illegal state today:** a value typed as prepared can contain missing IDs, an unknown variant, a
string run number, or an unsafe run directory.

### P2: Jetty lifecycle

Parse provider aliases once into a closed `JettyState`, then use lifecycle-specific values:
`Pending`, `Completed` (requiring trajectory/output), `Failed` (requiring a reason), `TimedOut`,
`DryRun`, and `ProtocolError`.

**Illegal state today:** completed without output, failed without an error, or unknown provider state
with apparently complete artifacts.

### P2: normalized trace-event lifecycle

Event type is now conservative, but event status remains a loose string with an optimistic completed
default. Add `Completed | InProgress | Failed | Unknown` and let each provider adapter state whether
emission itself proves completion.

**Illegal state today:** missing or misspelled event status can be consumed as a completed command.

### P2: ablation provenance vocabulary

Close remaining provenance strings (`mode`, population, component class, mechanism) with enums and
non-empty identifier types. Give each provenance variant its own constructor.

**Illegal state today:** semantically unknown modes or populations can serialize as valid-looking
attestations.
