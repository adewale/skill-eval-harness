"""Closed, identity-stable coverage cohorts for report aggregation."""
from __future__ import annotations

import math
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from numbers import Real
from types import MappingProxyType
from typing import Any, Literal, TypeAlias, TypeVar

from json_contracts import freeze_json_mapping


class ReportCoverageState(str, Enum):
    EMPTY = "empty"
    COMPLETE = "complete"
    PARTIAL = "partial"
    NOT_APPLICABLE = "not_applicable"


class UnitRate(float):
    """One finite observed rate in the closed interval [0, 1]."""

    def __new__(cls, value: Real) -> UnitRate:
        if (
            isinstance(value, bool)
            or not isinstance(value, Real)
            or not math.isfinite(float(value))
            or not 0 <= float(value) <= 1
        ):
            raise ValueError("report rate must be a finite number in [0, 1]")
        return float.__new__(cls, float(value))


@dataclass(frozen=True)
class ReportAttempt:
    identity: str
    row: Mapping[str, Any]
    observed: bool
    applicable: bool = True
    reason: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.identity, str) or not self.identity.strip():
            raise ValueError("report attempt identity must be non-empty")
        if not isinstance(self.row, Mapping):
            raise TypeError("report attempt row must be a mapping")
        if not isinstance(self.observed, bool) or not isinstance(self.applicable, bool):
            raise TypeError("report attempt disposition must be boolean")
        if self.observed and not self.applicable:
            raise ValueError("a non-applicable report attempt cannot be observed")
        if self.observed and self.reason is not None:
            raise ValueError("an observed report attempt cannot carry a reason")
        if not self.observed and (
            not isinstance(self.reason, str) or not self.reason.strip()
        ):
            raise ValueError("an unobserved report attempt requires a reason")
        object.__setattr__(self, "row", freeze_json_mapping(
            self.row, f"report attempt {self.identity}"))


def _validate_attempts(attempts: tuple[ReportAttempt, ...]) -> None:
    if not isinstance(attempts, tuple) or not all(
        isinstance(item, ReportAttempt) for item in attempts
    ):
        raise TypeError("report cohort attempts must be ReportAttempt values")
    identities = [item.identity for item in attempts]
    if len(set(identities)) != len(identities):
        raise ValueError("report cohort cannot repeat an attempt identity")


class _CohortCounts:
    attempts: tuple[ReportAttempt, ...]

    @property
    def rows(self) -> tuple[Mapping[str, Any], ...]:
        return tuple(item.row for item in self.attempts if item.observed)

    @property
    def attempted(self) -> int:
        return len(self.attempts)

    @property
    def applicable(self) -> int:
        return sum(item.applicable for item in self.attempts)

    @property
    def not_applicable(self) -> int:
        return self.attempted - self.applicable

    @property
    def blocked(self) -> int:
        return sum(item.applicable and not item.observed for item in self.attempts)


@dataclass(frozen=True)
class EmptyReportCohort(_CohortCounts):
    attempts: tuple[ReportAttempt, ...] = field(default=(), init=False)
    state: Literal[ReportCoverageState.EMPTY] = field(
        default=ReportCoverageState.EMPTY, init=False)


@dataclass(frozen=True)
class CompleteReportCohort(_CohortCounts):
    attempts: tuple[ReportAttempt, ...]
    state: Literal[ReportCoverageState.COMPLETE] = field(
        default=ReportCoverageState.COMPLETE, init=False)

    def __post_init__(self) -> None:
        _validate_attempts(self.attempts)
        if not self.attempts or not any(item.applicable for item in self.attempts):
            raise ValueError("complete report cohort needs an applicable attempt")
        if any(item.applicable and not item.observed for item in self.attempts):
            raise ValueError("complete report cohort cannot contain blocked attempts")


@dataclass(frozen=True)
class PartialReportCohort(_CohortCounts):
    attempts: tuple[ReportAttempt, ...]
    reason_counts: Mapping[str, int]
    state: Literal[ReportCoverageState.PARTIAL] = field(
        default=ReportCoverageState.PARTIAL, init=False)

    def __post_init__(self) -> None:
        _validate_attempts(self.attempts)
        if not self.attempts or not any(
            item.applicable and not item.observed for item in self.attempts
        ):
            raise ValueError("partial report cohort needs a blocked applicable attempt")
        expected = Counter(
            item.reason for item in self.attempts
            if item.applicable and not item.observed
        )
        if dict(self.reason_counts) != dict(expected):
            raise ValueError("partial report reason counts do not match its attempts")
        object.__setattr__(self, "reason_counts", MappingProxyType(dict(expected)))

    @property
    def reason(self) -> str:
        return ", ".join(sorted(self.reason_counts))


@dataclass(frozen=True)
class NotApplicableReportCohort(_CohortCounts):
    attempts: tuple[ReportAttempt, ...]
    state: Literal[ReportCoverageState.NOT_APPLICABLE] = field(
        default=ReportCoverageState.NOT_APPLICABLE, init=False)

    def __post_init__(self) -> None:
        _validate_attempts(self.attempts)
        if not self.attempts or any(item.applicable for item in self.attempts):
            raise ValueError("not-applicable cohort cannot contain applicable attempts")


ReportCohort: TypeAlias = (
    EmptyReportCohort
    | CompleteReportCohort
    | PartialReportCohort
    | NotApplicableReportCohort
)
RowT = TypeVar("RowT", bound=Mapping[str, Any])
Disposition: TypeAlias = tuple[bool | None, str | None]


def _cohort_from_attempts(attempts: tuple[ReportAttempt, ...]) -> ReportCohort:
    if not attempts:
        return EmptyReportCohort()
    if not any(item.applicable for item in attempts):
        return NotApplicableReportCohort(attempts)
    blocked = [item for item in attempts if item.applicable and not item.observed]
    if blocked:
        return PartialReportCohort(
            attempts, Counter(str(item.reason) for item in blocked))
    return CompleteReportCohort(attempts)


def report_cohort(
    attempted_rows: Sequence[RowT],
    *,
    identity: Callable[[RowT], str],
    eligibility: Callable[[RowT], Disposition],
) -> ReportCohort:
    """Classify each stable attempt exactly once before computing headlines.

    Eligibility returns ``(True, None)`` for observed, ``(False, reason)`` for
    unavailable, and ``(None, reason)`` for not applicable. No second list and
    no Python object identity are involved.
    """
    attempts: list[ReportAttempt] = []
    for row in attempted_rows:
        if not isinstance(row, Mapping):
            raise TypeError("report cohort rows must be mappings")
        observed, reason = eligibility(row)
        if observed is not True and observed is not False and observed is not None:
            raise TypeError("report eligibility must be True, False, or None")
        attempts.append(ReportAttempt(
            identity(row), row, observed is True,
            applicable=observed is not None,
            reason=reason,
        ))
    return _cohort_from_attempts(tuple(attempts))


def metric_cohort(
    cohort: ReportCohort,
    key: str,
    *,
    applicability: Callable[[Mapping[str, Any]], bool] | None = None,
) -> ReportCohort:
    """Refine row coverage to the availability of one particular rate."""
    attempts: list[ReportAttempt] = []
    for attempt in cohort.attempts:
        if not attempt.applicable:
            attempts.append(ReportAttempt(
                attempt.identity, attempt.row, False, False, attempt.reason))
            continue
        applicable = (
            True if applicability is None
            else applicability(attempt.row)
        )
        if not isinstance(applicable, bool):
            raise TypeError("metric applicability must be boolean")
        if not applicable:
            attempts.append(ReportAttempt(
                attempt.identity, attempt.row, False, False,
                f"{key}_not_applicable"))
            continue
        value = attempt.row.get(key)
        has_rate = value is not None
        if has_rate:
            UnitRate(value)
        observed = attempt.observed and has_rate
        reason = None if observed else (
            attempt.reason if not attempt.observed else f"missing_{key}")
        attempts.append(ReportAttempt(
            attempt.identity, attempt.row, observed, True, reason))
    return _cohort_from_attempts(tuple(attempts))


def observed_rows(cohort: ReportCohort) -> tuple[Mapping[str, Any], ...]:
    return cohort.rows


def attempted_rows(cohort: ReportCohort) -> tuple[Mapping[str, Any], ...]:
    return tuple(item.row for item in cohort.attempts)


def attempted_count(cohort: ReportCohort) -> int:
    return cohort.attempted


def blocked_count(cohort: ReportCohort) -> int:
    return cohort.blocked


def observed_rates(cohort: ReportCohort, key: str) -> tuple[UnitRate, ...]:
    rates: list[UnitRate] = []
    for row in cohort.rows:
        value = row.get(key)
        if value is None:
            raise ValueError(f"observed report row is missing {key}")
        rates.append(UnitRate(value))
    return tuple(rates)


def diagnostic_rates(cohort: ReportCohort, key: str) -> tuple[UnitRate, ...]:
    """Valid raw values, including blocked attempts, for named diagnostics only."""
    return tuple(
        UnitRate(value)
        for attempt in cohort.attempts
        if (value := attempt.row.get(key)) is not None
    )


def headline_value(cohort: ReportCohort, observed: Any) -> Any:
    """Publish a headline only for a complete metric denominator."""
    return observed if isinstance(cohort, CompleteReportCohort) else None


def coverage_fields(cohort: ReportCohort) -> dict[str, Any]:
    fields: dict[str, Any] = {
        "attempted_runs": cohort.attempted,
        "runs": len(cohort.rows),
        "blocked_runs": cohort.blocked,
        "availability": cohort.state.value,
    }
    if cohort.not_applicable:
        fields["not_applicable_runs"] = cohort.not_applicable
    if isinstance(cohort, PartialReportCohort):
        fields["reason"] = cohort.reason
        fields["blocked_reason_counts"] = dict(cohort.reason_counts)
    return fields
