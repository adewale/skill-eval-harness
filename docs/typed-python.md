# Typed Python boundary map

`ty` is a design gate for the harness, not a score-chasing pass over arbitrary dictionaries. The
useful question at each seam is: what untrusted or optional state enters, which validated value
leaves, and can consumers handle every state without a fallback branch?

## What the gate covers

One `ty check --error-on-warning` checks:

- every packaged top-level runtime module (`*.py`);
- repository tooling under `scripts/`;
- shipped runnable examples under `examples/`; and
- type-only precision and exhaustiveness proofs under `type_tests/`.

Runtime tests remain outside `ty`'s source set deliberately. Many are negative contract tests that
must call typed constructors with malformed strings, booleans, mutable containers, or incomplete
objects to prove those values are rejected at runtime. Treating those deliberate violations as
production inputs would weaken the API types or require hundreds of suppressions. Tests are still
Ruff-checked, byte-compiled, and executed on every supported Python version.

## Boundary inventory

| Incoming seam | Typed value leaving the seam | Owner |
|---|---|---|
| argparse namespace | `ValidatedLegacyCLIInvocation`, `CLICommand` | `cli_contracts.py` |
| manifest and prepared-row identities | `CaseId`, `Split`, `CaseKind`, `ExecutionVariant`, `ModelId`, `RunNumber` | `manifest_contracts.py` |
| experimental result rows | `ExperimentalPairKey`, `ExperimentalPair`, blocked-pair reasons | `experimental_pairs.py` |
| provider process inputs and completion | `InvocationRequest`, `ProcessInvocationPlan`, `InvocationResult` | `invocation_contracts.py` |
| native answer backend completion | `Completed`, `TimedOut`, `SpawnFailed`, `ProviderFailed` | `runner_contracts.py` |
| committed run directory | `ArtifactSetObservation` | `artifact_contracts.py` |
| normalized event file | `EventLogObservation`, `EventState` | `trace_contracts.py` |
| trigger process and evidence | `TriggerResult`, `TriggerObservation`, `TriggerCohort` | `trigger_contracts.py`, `trigger_reporting.py` |
| judge subprocess and verdict JSON | `JudgeInvocation`, `JudgeVerdict` | `judge_contracts.py`, `judge_verdict.py` |
| assertion and deferred judge rows | `AssertionObservation`, `JudgeTask` | `grading_contracts.py` |
| provider telemetry | availability/provenance/comparability domain values | `telemetry.py` |
| report population and rates | `ReportCohort`, `UnitRate` | `report_contracts.py` |
| rendered human text and matching | normalized text/assertion values | `text_contracts.py` |
| Gemini JSON and stream JSONL | frozen provider response/event values | `gemini_contracts.py` |
| Jetty attempts and imported results | lifecycle, receipt, and result observations | `jetty_contracts.py` |

Strict JSON syntax is shared by `json_contracts.py`; backend registrations and their runtime
materialization are owned by `agent_capabilities.py`. The broader engineering explanation remains
in [`abstractions.md`](abstractions.md), and the failure models and construction proofs remain in
[`correctness-by-construction-audit.md`](correctness-by-construction-audit.md).

## Drift prevention

`tests/test_type_coverage.py` keeps three different inventories separate and requires:

1. every top-level runtime module to be in the wheel's `py-modules` inventory;
2. `ty` to retain its runtime, tooling, example, and static-proof globs;
3. the explicit `TRIGGER_IDENTITY_MODULES` conservative module inventory to be packaged, versioned,
   and to contain the trigger entrypoints and shared process/pair owners without pulling their
   standalone CLI, grading, judge, report, Jetty, or unsupported-Gemini-trigger modules into causal
   identity;
4. every `*_contracts.py` boundary module to be named by the abstraction documentation; and
5. Linux and Windows CI to promote `ty` warnings to failures.

Packaging, static analysis, and causal identity answer different questions; filesystem equality
between them would make every unrelated report or CLI module edit invalidate trigger comparability.
The inventory is conservative at module granularity: because `skill_benchmark.py` still combines
trigger and non-trigger orchestration, every edit to that file intentionally invalidates trigger
identity. When adding a Python module, let the packaging/docs failures enumerate integration points
and update a semantic identity only when that module can change the named evidence surface. When
adding a closed union or refined scalar, add a runtime malformed-input test and a static narrowing
or precision proof in `type_tests/abstraction_contracts.py`. Do not make a diagnostic disappear by
broadening the domain to `Any`, adding a blanket ignore, or excluding the file.
