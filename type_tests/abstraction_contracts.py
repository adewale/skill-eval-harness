"""Static contracts for the harness's closed domain values.

This module is checked by ``ty`` but is not imported by the runtime or packaged.
The functions deliberately have no callers: their bodies prove that every
current union can be narrowed exhaustively and that its public fields retain
the intended precise types.
"""

from argparse import Namespace
from collections.abc import Mapping
from pathlib import Path
from typing import NoReturn

from typing_extensions import assert_type

from ablation_model import PreparedTask
from artifact_contracts import (
    ArtifactSetObservation,
    CompleteArtifactSet,
    IncompleteArtifactSet,
    InvalidArtifactCommit,
    LegacyArtifactSet,
    MissingArtifactCommit,
)
from cli_contracts import CLICommand, CLIInvocation
from experimental_pairs import (
    ExperimentalPair,
    ExperimentalPairKey,
    ExperimentalPopulation,
)
from grading_contracts import (
    AssertionObservation,
    FailedAssertion,
    JudgeTask,
    SatisfiedAssertion,
    SkippedAssertion,
    UnavailableAssertion,
)
from invocation_contracts import (
    InvocationRequest,
    InvocationResult,
    ProcessInvocationPlan,
    TimeoutSeconds,
)
from judge_verdict import (
    BooleanVerdict,
    ConsensusVerdict,
    DimensionVerdict,
    DynamicVerdict,
    JudgeVerdict,
    ScoredVerdict,
)
from manifest_contracts import (
    CaseId,
    CaseKind,
    ExecutionVariant,
    ModelId,
    RunNumber,
    Split,
)
from report_contracts import (
    CompleteReportCohort,
    EmptyReportCohort,
    NotApplicableReportCohort,
    PartialReportCohort,
    ReportCohort,
    UnitRate,
)
from runner_contracts import (
    AnswerOutcome,
    Completed,
    ProviderFailed,
    SpawnFailed,
    TimedOut,
)
from trace_contracts import (
    EventLogObservation,
    InvalidEventLog,
    LoadedEventLog,
    MissingEventLog,
)
from trigger_contracts import (
    CompleteTriggerResult,
    IncompleteTriggerResult,
    InvocationState,
    TriggerResult,
)
from trigger_reporting import (
    CompleteTriggerCohort,
    EmptyTriggerCohort,
    IncompleteTriggerCohort,
    TriggerCohort,
)


def _assert_never(value: NoReturn) -> NoReturn:
    raise AssertionError(f"unhandled closed-domain value: {value!r}")


def answer_outcome_is_exhaustive(outcome: AnswerOutcome) -> None:
    if isinstance(outcome, Completed):
        _answer: str = outcome.answer
    elif isinstance(outcome, TimedOut):
        _timeout_s: int | None = outcome.timeout_s
    elif isinstance(outcome, SpawnFailed):
        _reason: str = outcome.reason
    elif isinstance(outcome, ProviderFailed):
        _returncode: int = outcome.returncode
    else:
        _assert_never(outcome)


def trigger_result_is_exhaustive(result: TriggerResult) -> None:
    if isinstance(result, CompleteTriggerResult):
        _passed: bool = result.passed
        _triggered: bool = result.triggered
    elif isinstance(result, IncompleteTriggerResult):
        _state: str = result.state.value
    else:
        _assert_never(result)


def trigger_cohort_is_exhaustive(cohort: TriggerCohort) -> None:
    if isinstance(cohort, CompleteTriggerCohort):
        _pass_rate: float = cohort.pass_rate
    elif isinstance(cohort, IncompleteTriggerCohort):
        _total: int = cohort.total
    elif isinstance(cohort, EmptyTriggerCohort):
        _empty: EmptyTriggerCohort = cohort
    else:
        _assert_never(cohort)


def judge_verdict_is_exhaustive(verdict: JudgeVerdict) -> None:
    if isinstance(verdict, BooleanVerdict):
        _boolean_passed: bool = verdict.passed
    elif isinstance(verdict, ScoredVerdict):
        _score: float = verdict.score
    elif isinstance(verdict, DimensionVerdict):
        _dimension_score: float = verdict.score
    elif isinstance(verdict, DynamicVerdict):
        _dynamic_passed: bool = verdict.passed
    elif isinstance(verdict, ConsensusVerdict):
        _consensus_passed: bool = verdict.passed
    else:
        _assert_never(verdict)


def prepared_task_identity_is_precise(task: PreparedTask) -> None:
    _case_id: CaseId = task.case_id
    _split: Split = task.split
    _kind: CaseKind = task.kind
    _variant: ExecutionVariant = task.variant_truth
    _run_number: RunNumber = task.run_number
    _visible_variant: ExecutionVariant = task.model_facing_variant()


def experimental_pair_identity_is_precise(key: ExperimentalPairKey) -> None:
    _case_id: CaseId = key.case_id
    _model: ModelId | None = key.model
    _run_number: RunNumber = key.run_number
    _population: ExperimentalPopulation = key.population


def experimental_pair_payload_type_is_preserved(
    pair: ExperimentalPair[Path],
) -> None:
    assert_type(pair.with_skill.payload, Path)
    assert_type(pair.without_skill.payload, Path)


def provider_invocation_types_are_precise(
    request: InvocationRequest,
    plan: ProcessInvocationPlan,
    result: InvocationResult,
) -> None:
    _request_model: ModelId | None = request.model
    _request_timeout: TimeoutSeconds = request.timeout_s
    _argv: tuple[str, ...] = plan.argv
    _environment: Mapping[str, str] | None = plan.environment
    _state: InvocationState = result.invocation_state


def artifact_set_observation_is_exhaustive(
    observation: ArtifactSetObservation,
) -> None:
    if isinstance(observation, LegacyArtifactSet):
        _legacy: LegacyArtifactSet = observation
    elif isinstance(observation, MissingArtifactCommit):
        _missing_reason: str = observation.reason
    elif isinstance(observation, InvalidArtifactCommit):
        _invalid_reason: str = observation.reason
    elif isinstance(observation, IncompleteArtifactSet):
        _incomplete_reason: str = observation.reason
    elif isinstance(observation, CompleteArtifactSet):
        _inventory: Mapping[str, str] = observation.inventory_sha256
    else:
        _assert_never(observation)


def event_log_observation_is_exhaustive(observation: EventLogObservation) -> None:
    if isinstance(observation, MissingEventLog):
        _missing_reason: str = observation.reason
    elif isinstance(observation, InvalidEventLog):
        _invalid_reason: str = observation.reason
    elif isinstance(observation, LoadedEventLog):
        _events: tuple[Mapping[str, object], ...] = observation.events
        _schema_version: int = observation.schema_version
    else:
        _assert_never(observation)


def assertion_observation_is_exhaustive(
    observation: AssertionObservation,
) -> None:
    if isinstance(observation, SatisfiedAssertion):
        _satisfied: SatisfiedAssertion = observation
    elif isinstance(observation, FailedAssertion):
        _failed: FailedAssertion = observation
    elif isinstance(observation, UnavailableAssertion):
        _observed_passed: bool | None = observation.observed_passed
    elif isinstance(observation, SkippedAssertion):
        _skip_reason: str = observation.skip_reason
    else:
        _assert_never(observation)


def judge_task_identity_is_precise(task: JudgeTask) -> None:
    _case_id: CaseId = task.case_id
    _variant: ExecutionVariant = task.variant
    _run_number: RunNumber = task.run_number
    _model: ModelId | None = task.model
    _assertion: Mapping[str, object] = task.assertion


def report_cohort_is_exhaustive(cohort: ReportCohort) -> None:
    if isinstance(cohort, EmptyReportCohort):
        _empty: EmptyReportCohort = cohort
    elif isinstance(cohort, CompleteReportCohort):
        _complete_rows: tuple[Mapping[str, object], ...] = cohort.rows
    elif isinstance(cohort, PartialReportCohort):
        _reason: str = cohort.reason
    elif isinstance(cohort, NotApplicableReportCohort):
        _attempted: int = cohort.attempted
    else:
        _assert_never(cohort)


def report_rate_is_precise(rate: UnitRate) -> None:
    _rate: float = rate


def cli_invocation_types_are_precise(invocation: CLIInvocation) -> None:
    _command: CLICommand = invocation.command
    _paths: Mapping[str, Path | tuple[Path, ...]] = invocation.paths
    _split: Split | None = invocation.split
    _variants: tuple[ExecutionVariant, ...] = invocation.variants
    _model: ModelId | None = invocation.model
    _models: tuple[ModelId, ...] = invocation.models
    _judge_models: tuple[ModelId, ...] = invocation.judge_models
    _namespace: Namespace = invocation.to_legacy_namespace()
