"""Static contracts for the harness's closed domain values.

This module is checked by ``ty`` but is not imported by the runtime or packaged.
The functions deliberately have no callers: their bodies prove that every
current union can be narrowed exhaustively and that its public fields retain
the intended precise types.
"""

from collections.abc import Mapping
from pathlib import Path
from typing import NoReturn

from typing_extensions import assert_type

from ablation_model import PreparedTask
from experimental_pairs import (
    ExperimentalPair,
    ExperimentalPairKey,
    ExperimentalPopulation,
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
from runner_contracts import (
    AnswerOutcome,
    Completed,
    ProviderFailed,
    SpawnFailed,
    TimedOut,
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
