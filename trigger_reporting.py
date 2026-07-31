"""Proof-carrying aggregation for autonomous-trigger observations.

The wire row is deliberately the last representation produced. Rates exist only
on ``CompleteTriggerCohort``; an incomplete or empty cohort has no numeric rate
field to accidentally serialize as a quality result.
"""
from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any, NoReturn, TypeAlias

from trigger_contracts import (
    CompleteTriggerResult,
    IncompleteTriggerResult,
    InvocationState,
    TriggerExpectation,
    TriggerObservation,
)


def _natural(value: int, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")


@dataclass(frozen=True)
class CompleteTriggerCohort:
    """Every attempted observation produced a measured quality result."""

    total: int
    passed: int
    triggered: int

    def __post_init__(self) -> None:
        for name, value in (("total", self.total), ("passed", self.passed), ("triggered", self.triggered)):
            _natural(value, name)
        if self.total == 0:
            raise ValueError("a complete cohort must contain at least one observation")
        if self.passed > self.total or self.triggered > self.total:
            raise ValueError("complete cohort successes cannot exceed total observations")

    @property
    def pass_rate(self) -> float:
        return self.passed / self.total

    @property
    def trigger_rate(self) -> float:
        return self.triggered / self.total


@dataclass(frozen=True)
class IncompleteTriggerReason:
    """A counted, closed reason why a trigger observation is incomplete."""

    state: InvocationState
    count: int

    def __post_init__(self) -> None:
        if not isinstance(self.state, InvocationState) or self.state is InvocationState.COMPLETE:
            raise ValueError("incomplete cohort reasons require a non-complete invocation state")
        _natural(self.count, "incomplete reason count")
        if self.count == 0:
            raise ValueError("incomplete cohort reason counts must be positive")


@dataclass(frozen=True)
class IncompleteTriggerCohort:
    """At least one attempt lacks the evidence required for a rate."""

    total: int
    observed: int
    passed: int
    triggered: int
    reasons: tuple[IncompleteTriggerReason, ...]

    def __post_init__(self) -> None:
        for name, value in (
            ("total", self.total),
            ("observed", self.observed),
            ("passed", self.passed),
            ("triggered", self.triggered),
        ):
            _natural(value, name)
        if self.total == 0 or self.observed >= self.total:
            raise ValueError("an incomplete cohort requires at least one unobserved attempt")
        if self.passed > self.observed or self.triggered > self.observed:
            raise ValueError("incomplete cohort successes cannot exceed observed attempts")
        if (not isinstance(self.reasons, tuple) or not self.reasons
                or not all(isinstance(reason, IncompleteTriggerReason)
                           for reason in self.reasons)):
            raise TypeError("an incomplete cohort requires an immutable tuple of typed reasons")
        if len({reason.state for reason in self.reasons}) != len(self.reasons):
            raise ValueError("incomplete cohort reason keys must be unique")
        if sum(reason.count for reason in self.reasons) != self.total - self.observed:
            raise ValueError("incomplete reason counts must account for every unobserved attempt")


@dataclass(frozen=True)
class EmptyTriggerCohort:
    """No observation was attempted; this is not a zero-rate measurement."""


TriggerCohort: TypeAlias = CompleteTriggerCohort | IncompleteTriggerCohort | EmptyTriggerCohort


def summarize_trigger_cohort(observations: Sequence[TriggerObservation]) -> TriggerCohort:
    if not observations:
        return EmptyTriggerCohort()

    measured: list[CompleteTriggerResult] = []
    reasons: Counter[InvocationState] = Counter()
    for observation in observations:
        result = observation.result
        if isinstance(result, CompleteTriggerResult):
            measured.append(result)
        elif isinstance(result, IncompleteTriggerResult):
            reasons[result.state] += 1
        else:
            _assert_never(result)

    passed = sum(result.passed for result in measured)
    triggered = sum(result.triggered for result in measured)
    if reasons:
        return IncompleteTriggerCohort(
            total=len(observations),
            observed=len(measured),
            passed=passed,
            triggered=triggered,
            reasons=tuple(
                IncompleteTriggerReason(state, count)
                for state, count in sorted(reasons.items(), key=lambda item: item[0].value)
            ),
        )
    return CompleteTriggerCohort(len(observations), passed, triggered)


def trigger_cohort_as_dict(cohort: TriggerCohort) -> dict[str, Any]:
    if isinstance(cohort, CompleteTriggerCohort):
        return {
            "measurement_status": "complete",
            "total": cohort.total,
            "complete": cohort.total,
            "incomplete": 0,
            "observed": cohort.total,
            "incomplete_observations": 0,
            "passed": cohort.passed,
            "failed": cohort.total - cohort.passed,
            "pass_rate": cohort.pass_rate,
            "observed_pass_rate": cohort.pass_rate,
            "triggered": cohort.triggered,
            "trigger_rate": cohort.trigger_rate,
            "observed_trigger_rate": cohort.trigger_rate,
        }
    if isinstance(cohort, IncompleteTriggerCohort):
        return {
            "measurement_status": "incomplete",
            "total": cohort.total,
            "complete": cohort.observed,
            "incomplete": cohort.total - cohort.observed,
            "observed": cohort.observed,
            "incomplete_observations": cohort.total - cohort.observed,
            "passed": cohort.passed,
            "failed": cohort.observed - cohort.passed,
            "triggered": cohort.triggered,
            "incomplete_reasons": {
                reason.state.value: reason.count for reason in cohort.reasons
            },
        }
    if isinstance(cohort, EmptyTriggerCohort):
        return {
            "measurement_status": "empty",
            "total": 0,
            "complete": 0,
            "incomplete": 0,
            "observed": 0,
            "incomplete_observations": 0,
            "passed": 0,
            "failed": 0,
            "triggered": 0,
        }
    return _assert_never(cohort)


def trigger_cohort_exit_code(cohort: TriggerCohort) -> int:
    return 0 if isinstance(cohort, CompleteTriggerCohort) else 1


def _query_summary_as_dict(query_id: str, query: str, expectation: TriggerExpectation,
                           observations: Sequence[TriggerObservation]) -> dict[str, Any]:
    block = trigger_cohort_as_dict(summarize_trigger_cohort(observations))
    renamed = {
        "query_id": query_id,
        "query": query,
        "should_trigger": expectation.should_trigger,
        "runs": block.pop("total"),
        "complete": block.pop("complete"),
        "incomplete": block.pop("incomplete"),
        "passed_runs": block.pop("passed"),
        "failed_runs": block.pop("failed"),
        "triggered_runs": block.pop("triggered"),
    }
    block.pop("observed")
    block.pop("incomplete_observations")
    return {**renamed, **block}


def summarize_trigger_matrix(observations: Iterable[TriggerObservation]) -> list[dict[str, Any]]:
    """Group typed observations and serialize only through the cohort owner."""
    cells: dict[tuple[str, str | None], list[TriggerObservation]] = {}
    for observation in observations:
        cells.setdefault((observation.agent, observation.model), []).append(observation)

    matrix: list[dict[str, Any]] = []
    for (agent, model), cell_observations in sorted(
        cells.items(), key=lambda item: (item[0][0], str(item[0][1])),
    ):
        grouped_queries: dict[
            tuple[str, str, TriggerExpectation], list[TriggerObservation]
        ] = {}
        for observation in cell_observations:
            query_id = (
                observation.identity.query_id
                if observation.identity is not None else observation.query
            )
            grouped_queries.setdefault(
                (query_id, observation.query, observation.expectation), [],
            ).append(observation)
        query_summaries = [
            _query_summary_as_dict(
                query_id, query, expectation, query_observations,
            )
            for (
                query_id, query, expectation
            ), query_observations in grouped_queries.items()
        ]
        summary = trigger_cohort_as_dict(summarize_trigger_cohort(cell_observations))
        summary["should_trigger"] = trigger_cohort_as_dict(summarize_trigger_cohort([
                observation for observation in cell_observations
                if observation.expectation is TriggerExpectation.TRIGGER
            ]))
        summary["should_not_trigger"] = trigger_cohort_as_dict(summarize_trigger_cohort([
                observation for observation in cell_observations
                if observation.expectation is TriggerExpectation.DO_NOT_TRIGGER
            ]))
        matrix.append({
            "agent": agent,
            "model": model,
            "summary": summary,
            "queries": query_summaries,
        })
    return matrix


def _assert_never(value: NoReturn) -> NoReturn:
    raise AssertionError(f"unhandled trigger-reporting variant: {value!r}")
