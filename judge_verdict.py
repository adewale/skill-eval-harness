"""Strict discriminated judge verdicts and stored-result boundary parsing."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import math
import statistics
from typing import Any, Mapping, TypeAlias


class VerdictKind(str, Enum):
    BOOLEAN = "boolean"
    SCORED = "scored"
    DIMENSIONS = "dimensions"
    DYNAMIC = "dynamic"
    CONSENSUS = "consensus"


def _number(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        raise ValueError(f"{label} must be a finite number")
    return float(value)


def _passed(value: Any) -> bool:
    if not isinstance(value, bool):
        raise ValueError("passed must be a boolean")
    return value


@dataclass(frozen=True)
class BooleanVerdict:
    passed: bool
    kind: VerdictKind = field(default=VerdictKind.BOOLEAN, init=False)

    def __post_init__(self) -> None:
        _passed(self.passed)


@dataclass(frozen=True)
class ScoredVerdict:
    score: float
    threshold: float
    passed: bool
    kind: VerdictKind = field(default=VerdictKind.SCORED, init=False)

    def __post_init__(self) -> None:
        score, threshold = _number(self.score, "score"), _number(self.threshold, "threshold")
        if _passed(self.passed) != (score >= threshold):
            raise ValueError("passed contradicts score and threshold")
        object.__setattr__(self, "score", score)
        object.__setattr__(self, "threshold", threshold)


@dataclass(frozen=True)
class DimensionVerdict:
    dimension_scores: tuple[tuple[str, float], ...]
    score: float
    threshold: float
    passed: bool
    kind: VerdictKind = field(default=VerdictKind.DIMENSIONS, init=False)

    def __post_init__(self) -> None:
        if not self.dimension_scores or len({name for name, _ in self.dimension_scores}) != len(self.dimension_scores):
            raise ValueError("dimension names must be non-empty and unique")
        values = []
        for name, value in self.dimension_scores:
            if not isinstance(name, str) or not name:
                raise ValueError("dimension name must be non-empty")
            number = _number(value, f"dimension {name}")
            if not 1 <= number <= 5:
                raise ValueError("dimension scores must be in [1,5]")
            values.append(number)
        derived = round(statistics.mean((value - 1) / 4 for value in values), 4)
        threshold = _number(self.threshold, "threshold")
        if not 0 <= threshold <= 1:
            raise ValueError("dimension threshold must be normalized to [0,1]")
        if abs(_number(self.score, "score") - derived) > 1e-9:
            raise ValueError("dimension aggregate score contradicts dimension_scores")
        if _passed(self.passed) != (derived >= threshold):
            raise ValueError("dimension passed contradicts score and threshold")
        object.__setattr__(self, "dimension_scores", tuple((name, float(value)) for name, value in self.dimension_scores))
        object.__setattr__(self, "score", derived)
        object.__setattr__(self, "threshold", threshold)


@dataclass(frozen=True)
class DynamicVerdict:
    criteria: tuple[tuple[str, bool], ...]
    minimum_criteria: int
    score: float
    passed: bool
    kind: VerdictKind = field(default=VerdictKind.DYNAMIC, init=False)

    def __post_init__(self) -> None:
        if (isinstance(self.minimum_criteria, bool) or not isinstance(self.minimum_criteria, int)
                or self.minimum_criteria < 1):
            raise ValueError("minimum_criteria must be a positive integer")
        if not self.criteria or len({name for name, _ in self.criteria}) != len(self.criteria):
            raise ValueError("dynamic criteria names must be non-empty and unique")
        if self.minimum_criteria > len(self.criteria):
            raise ValueError("minimum_criteria cannot exceed the number of criteria")
        for name, met in self.criteria:
            if not isinstance(name, str) or not name or not isinstance(met, bool):
                raise ValueError("dynamic criteria require non-empty names and boolean met")
        met_count = sum(1 for _, met in self.criteria if met)
        derived = round(met_count / len(self.criteria), 4)
        expected = len(self.criteria) >= self.minimum_criteria and met_count >= self.minimum_criteria
        if abs(_number(self.score, "score") - derived) > 1e-9 or _passed(self.passed) != expected:
            raise ValueError("dynamic score/passed contradict criteria")
        object.__setattr__(self, "score", derived)


@dataclass(frozen=True)
class ConsensusVerdict:
    passed: bool
    score: float | None = None
    kind: VerdictKind = field(default=VerdictKind.CONSENSUS, init=False)

    def __post_init__(self) -> None:
        _passed(self.passed)
        if self.score is not None:
            object.__setattr__(self, "score", _number(self.score, "score"))


JudgeVerdict: TypeAlias = BooleanVerdict | ScoredVerdict | DimensionVerdict | DynamicVerdict | ConsensusVerdict


_SEMANTIC_FIELDS = frozenset({
    "passed", "score", "threshold", "dimension_scores", "criteria", "minimum_criteria",
})
_ALLOWED_FIELDS = {
    VerdictKind.BOOLEAN: frozenset({"passed"}),
    VerdictKind.SCORED: frozenset({"passed", "score", "threshold"}),
    VerdictKind.DIMENSIONS: frozenset({"passed", "score", "threshold", "dimension_scores"}),
    VerdictKind.DYNAMIC: frozenset({"passed", "score", "criteria", "minimum_criteria"}),
    VerdictKind.CONSENSUS: frozenset({"passed", "score"}),
}


def verdict_from_dict(raw: Mapping[str, Any], *, strict_stored: bool = True,
                      expected_dimensions: tuple[str, ...] | None = None) -> JudgeVerdict:
    if not isinstance(raw, Mapping):
        raise TypeError("judge verdict must be an object")
    explicit = raw.get("verdict_kind")
    try:
        kind = VerdictKind(explicit) if explicit is not None else None
    except ValueError as exc:
        raise ValueError(f"unknown verdict_kind {explicit!r}") from exc
    has_dims, has_dynamic = "dimension_scores" in raw, "criteria" in raw
    if has_dims and has_dynamic:
        raise ValueError("judge verdict cannot mix dimensions and dynamic criteria")
    if kind is None:
        if raw.get("judge_model") == "consensus" or "judge_panel" in raw:
            kind = VerdictKind.CONSENSUS
        elif has_dims:
            kind = VerdictKind.DIMENSIONS
        elif has_dynamic:
            kind = VerdictKind.DYNAMIC
        elif raw.get("score") is not None:
            kind = VerdictKind.SCORED
        elif "passed" in raw:
            kind = VerdictKind.BOOLEAN
        else:
            raise ValueError("judge verdict has no recognized payload")
    present_semantic = {
        key for key in (set(raw) & _SEMANTIC_FIELDS)
        if not (key == "score" and raw.get(key) is None)
        and not (key == "threshold" and kind is VerdictKind.BOOLEAN and raw.get("score") is None)
    }
    foreign_fields = sorted(present_semantic - _ALLOWED_FIELDS[kind])
    if foreign_fields:
        raise ValueError(f"{kind.value} verdict cannot carry fields: {', '.join(foreign_fields)}")
    if kind is VerdictKind.BOOLEAN:
        return BooleanVerdict(_passed(raw.get("passed")))
    if kind is VerdictKind.SCORED:
        if raw.get("threshold") is None:
            if strict_stored:
                raise ValueError("stored scored verdict requires threshold")
            threshold = 1.0
        else:
            threshold = raw.get("threshold")
        score = _number(raw.get("score"), "score")
        passed = raw.get("passed", score >= _number(threshold, "threshold"))
        return ScoredVerdict(score, threshold, _passed(passed))
    if kind is VerdictKind.DIMENSIONS:
        dims = raw.get("dimension_scores")
        if not isinstance(dims, Mapping):
            raise ValueError("dimension_scores must be an object")
        pairs = tuple((name, _number(value, f"dimension {name}")) for name, value in dims.items())
        if not all(isinstance(name, str) and name for name, _ in pairs):
            raise ValueError("dimension names must be non-empty strings")
        if expected_dimensions is not None and set(name for name, _ in pairs) != set(expected_dimensions):
            raise ValueError("dimension_scores must exactly match the declared dimensions")
        normalized = round(statistics.mean((value - 1) / 4 for _, value in pairs), 4) if pairs else float("nan")
        threshold = raw.get("threshold")
        if threshold is None:
            raise ValueError("stored dimension verdict requires normalized threshold")
        passed = raw.get("passed", normalized >= _number(threshold, "threshold"))
        return DimensionVerdict(pairs, raw.get("score", normalized), threshold, _passed(passed))
    if kind is VerdictKind.DYNAMIC:
        criteria = raw.get("criteria")
        if not isinstance(criteria, list):
            raise ValueError("criteria must be a list")
        pairs = []
        for item in criteria:
            if not isinstance(item, Mapping):
                raise ValueError("criterion must be an object")
            pairs.append((item.get("name"), item.get("met")))
        minimum = raw.get("minimum_criteria")
        if minimum is None:
            raise ValueError("stored dynamic verdict requires minimum_criteria")
        met = sum(1 for _, value in pairs if value is True)
        score = round(met / len(pairs), 4) if pairs else float("nan")
        passed = raw.get("passed", len(pairs) >= minimum and met >= minimum)
        return DynamicVerdict(tuple(pairs), minimum, raw.get("score", score), _passed(passed))
    return ConsensusVerdict(_passed(raw.get("passed")), raw.get("score"))


def verdict_fields(verdict: JudgeVerdict) -> dict[str, Any]:
    out: dict[str, Any] = {"verdict_kind": verdict.kind.value, "passed": verdict.passed}
    if isinstance(verdict, ScoredVerdict):
        out.update(score=verdict.score, threshold=verdict.threshold)
    elif isinstance(verdict, DimensionVerdict):
        out.update(score=verdict.score, threshold=verdict.threshold,
                   dimension_scores={name: value for name, value in verdict.dimension_scores})
    elif isinstance(verdict, DynamicVerdict):
        out.update(score=verdict.score, minimum_criteria=verdict.minimum_criteria,
                   criteria=[{"name": name, "met": met} for name, met in verdict.criteria])
    elif isinstance(verdict, ConsensusVerdict) and verdict.score is not None:
        out["score"] = verdict.score
    return out


def validated_result_row(raw: Mapping[str, Any], *,
                         expected_dimensions: tuple[str, ...] | None = None) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise TypeError("judge result row must be an object")
    verdict = verdict_from_dict(
        raw, strict_stored=True, expected_dimensions=expected_dimensions)
    nonsemantic = {key: value for key, value in raw.items()
                   if key not in _SEMANTIC_FIELDS and key != "verdict_kind"}
    return {**nonsemantic, **verdict_fields(verdict)}
