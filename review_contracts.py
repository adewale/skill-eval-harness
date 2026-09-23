"""Eval-quality review: suspect verifiers and inverted model orderings.

A pass/fail number cannot say whether a failure was the model's or the
eval's. This module turns graded outcomes into two diagnostic findings that
point a human at the runs worth reading:

- ``VerifierSuspicion``: an assertion that may be rejecting a correct answer.
  Three closed signals, each derived from observed verdicts only — the
  assertion never passes anywhere (``never_passes``), it fails only on case
  or markdown formatting (``format_near_miss``), or a judge passed the same
  run the deterministic check failed (``oracle_disagreement``).
- ``ModelOrderInversion``: a weaker model, by an order the operator
  declared, fully passes more runs than a stronger one — often a harness
  that restricts the stronger model, or a verifier tuned to one model's
  phrasing.

Both are evidence for review, never verdicts: nothing here changes a pass
rate. Model capability order is never inferred from model names; it exists
only when the caller declares a ``ModelOrder``.
"""
from __future__ import annotations

import math
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Any

from manifest_contracts import CaseId, ExecutionVariant, ModelId, RunNumber

DIAGNOSTIC_EVIDENCE_CLASS = "diagnostic"
SIGNIFICANCE_ALPHA = 0.05
DEFAULT_MIN_OBSERVATIONS = 2
SUITE_SCOPE = "suite"


class VerifierSignal(str, Enum):
    NEVER_PASSES = "never_passes"
    FORMAT_NEAR_MISS = "format_near_miss"
    ORACLE_DISAGREEMENT = "oracle_disagreement"


class AssertionRole(str, Enum):
    """Which oracle produced a verdict: a deterministic check or a judge."""

    OBJECTIVE = "objective"
    QUALITATIVE = "qualitative"


_SIGNAL_MEANING = {
    VerifierSignal.NEVER_PASSES: (
        "no observed run of any arm or model passed this assertion; either the task "
        "is impossible as instructed or the verifier rejects correct answers"),
    VerifierSignal.FORMAT_NEAR_MISS: (
        "the failing answer passes when case and markdown formatting are ignored; "
        "the check may be stricter about presentation than the task asked for"),
    VerifierSignal.ORACLE_DISAGREEMENT: (
        "a judge passed the same run this deterministic check failed; one of the "
        "two oracles is wrong"),
}


def _label(value: Any, what: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{what} must be a non-empty string")
    return value


@dataclass(frozen=True)
class RunRef:
    """The stable identity of one graded run."""

    case_id: CaseId
    variant: ExecutionVariant
    run_number: RunNumber
    model: ModelId | None = None

    def __post_init__(self) -> None:
        for name, kind in (("case_id", CaseId), ("variant", ExecutionVariant), ("run_number", RunNumber)):
            if not isinstance(getattr(self, name), kind):
                raise TypeError(f"run {name} must be {kind.__name__}")
        if self.model is not None and not isinstance(self.model, ModelId):
            raise TypeError("run model must be ModelId or None")

    @classmethod
    def parse(cls, case_id: Any, variant: Any, run_number: Any, model: Any = None) -> RunRef:
        return cls(CaseId.parse(case_id), ExecutionVariant.parse(variant), RunNumber.parse(run_number),
                   None if model is None else ModelId.parse(model))

    def sort_key(self) -> tuple[str, str, str, int]:
        return (str(self.case_id), str(self.model or ""), str(self.variant), int(self.run_number))

    def to_dict(self) -> dict[str, Any]:
        return {"case_id": str(self.case_id), "model": None if self.model is None else str(self.model),
                "variant": str(self.variant), "run_number": int(self.run_number)}


@dataclass(frozen=True)
class AssertionOutcome:
    """One observed verdict. Only complete verdicts enter review: an
    unavailable assertion is not evidence against the verifier."""

    run: RunRef
    assertion: str
    role: AssertionRole
    gate: bool
    passed: bool
    format_near_miss: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.run, RunRef):
            raise TypeError("assertion outcome run must be RunRef")
        object.__setattr__(self, "assertion", _label(self.assertion, "assertion name"))
        if not isinstance(self.role, AssertionRole):
            raise TypeError("assertion outcome role must be AssertionRole")
        for name in ("gate", "passed", "format_near_miss"):
            if not isinstance(getattr(self, name), bool):
                raise TypeError(f"assertion outcome {name} must be boolean")
        if self.format_near_miss and (self.passed or self.role is not AssertionRole.OBJECTIVE):
            raise ValueError("only a failed objective assertion can be a formatting near miss")


@dataclass(frozen=True)
class VerifierSuspicion:
    case_id: CaseId
    assertion: str
    signal: VerifierSignal
    runs: tuple[RunRef, ...]
    detail: str

    def __post_init__(self) -> None:
        if not isinstance(self.case_id, CaseId):
            raise TypeError("suspicion case_id must be CaseId")
        object.__setattr__(self, "assertion", _label(self.assertion, "assertion name"))
        if not isinstance(self.signal, VerifierSignal):
            raise TypeError("suspicion signal must be VerifierSignal")
        if not isinstance(self.runs, tuple) or not self.runs or not all(isinstance(run, RunRef) for run in self.runs):
            raise ValueError("a suspicion must name at least one RunRef")
        if any(run.case_id != self.case_id for run in self.runs):
            raise ValueError("every suspect run must belong to the suspicion's case")
        if len(set(self.runs)) != len(self.runs):
            raise ValueError("suspect runs must be unique")
        object.__setattr__(self, "detail", _label(self.detail, "suspicion detail"))

    def to_dict(self) -> dict[str, Any]:
        return {"case_id": str(self.case_id), "assertion": self.assertion, "signal": self.signal.value,
                "meaning": _SIGNAL_MEANING[self.signal], "detail": self.detail,
                "runs": [run.to_dict() for run in self.runs]}


def formatting_relaxed_text(text: str) -> str:
    """Remove presentation only: markdown emphasis and code markers, leading
    list/quote/heading markers, typographic quotes and dashes, and runs of
    horizontal whitespace. Line structure is kept, so line anchors still mean
    what their author intended."""
    if not isinstance(text, str):
        raise TypeError("text must be a string")
    text = text.translate({0x2018: "'", 0x2019: "'", 0x201C: '"', 0x201D: '"', 0x2013: "-", 0x2014: "-"})
    lines = []
    for line in text.splitlines():
        line = re.sub(r"^\s*(?:[>#]+|[-*+]|\d+[.)])\s+", "", line)
        line = re.sub(r"[*`~]", "", line)
        line = re.sub(r"[ \t]+", " ", line).strip()
        lines.append(line)
    return "\n".join(lines)


def verifier_suspicions(
    outcomes: Iterable[AssertionOutcome], *, min_observations: int = DEFAULT_MIN_OBSERVATIONS,
) -> tuple[VerifierSuspicion, ...]:
    if isinstance(min_observations, bool) or not isinstance(min_observations, int) or min_observations < 1:
        raise ValueError("min_observations must be a positive integer")
    rows = list(outcomes)
    if not all(isinstance(row, AssertionOutcome) for row in rows):
        raise TypeError("verifier review consumes AssertionOutcome values")
    found: list[VerifierSuspicion] = []

    by_assertion: dict[tuple[CaseId, str], list[AssertionOutcome]] = {}
    for row in rows:
        if row.role is AssertionRole.OBJECTIVE:
            by_assertion.setdefault((row.run.case_id, row.assertion), []).append(row)
    for (case_id, name), observed in sorted(by_assertion.items()):
        runs = tuple(sorted({row.run for row in observed}, key=RunRef.sort_key))
        if len(runs) >= min_observations and not any(row.passed for row in observed):
            arms = sorted({str(run.variant) for run in runs})
            models = sorted({str(run.model) for run in runs if run.model is not None})
            detail = f"failed {len(runs)} of {len(runs)} observed runs across arms {arms}"
            if models:
                detail += f" and models {models}"
            found.append(VerifierSuspicion(case_id, name, VerifierSignal.NEVER_PASSES, runs, detail))
        near = tuple(sorted({row.run for row in observed if row.format_near_miss}, key=RunRef.sort_key))
        if near:
            found.append(VerifierSuspicion(
                case_id, name, VerifierSignal.FORMAT_NEAR_MISS, near,
                f"{len(near)} failing run(s) pass once case and markdown formatting are ignored"))

    by_run: dict[RunRef, list[AssertionOutcome]] = {}
    for row in rows:
        by_run.setdefault(row.run, []).append(row)
    disagreements: dict[tuple[CaseId, str], dict[RunRef, set[str]]] = {}
    for run, observed in by_run.items():
        judges = sorted({row.assertion for row in observed if row.role is AssertionRole.QUALITATIVE and row.passed})
        if not judges:
            continue
        for row in observed:
            if row.role is AssertionRole.OBJECTIVE and row.gate and not row.passed:
                disagreements.setdefault((run.case_id, row.assertion), {}).setdefault(run, set()).update(judges)
    for (case_id, name), per_run in sorted(disagreements.items()):
        runs = tuple(sorted(per_run, key=RunRef.sort_key))
        judges = sorted({judge for names in per_run.values() for judge in names})
        found.append(VerifierSuspicion(
            case_id, name, VerifierSignal.ORACLE_DISAGREEMENT, runs,
            f"judge(s) {judges} passed {len(runs)} run(s) this gate failed"))
    return tuple(sorted(found, key=lambda s: (str(s.case_id), s.assertion, s.signal.value)))


# --------------------------------------------------------------------------- #
# Model-order inversions
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class ModelOrder:
    """Models from weakest to strongest, as the operator declares them."""

    models: tuple[ModelId, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.models, tuple) or not all(isinstance(model, ModelId) for model in self.models):
            raise TypeError("model order must be a tuple of ModelId")
        if len(self.models) < 2:
            raise ValueError("model order needs at least two models, weakest first")
        if len(set(self.models)) != len(self.models):
            raise ValueError("model order must not repeat a model")

    @classmethod
    def parse(cls, raw: Any) -> ModelOrder:
        if isinstance(raw, str):
            items: Sequence[Any] = [item.strip() for item in raw.split(",")]
        elif isinstance(raw, (list, tuple)):
            items = raw
        else:
            raise ValueError("model order must be a comma-separated string or a list")
        return cls(tuple(ModelId.parse(item) for item in items))

    def rank(self, model: ModelId | None) -> int | None:
        return None if model is None or model not in self.models else self.models.index(model)

    def to_list(self) -> list[str]:
        return [str(model) for model in self.models]


@dataclass(frozen=True)
class PassCount:
    runs: int
    passed: int

    def __post_init__(self) -> None:
        for name in ("runs", "passed"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"pass count {name} must be a non-negative integer")
        if self.runs == 0 or self.passed > self.runs:
            raise ValueError("pass count needs at least one run and at most runs passes")

    @property
    def rate(self) -> float:
        return self.passed / self.runs

    def plus(self, other: PassCount) -> PassCount:
        return PassCount(self.runs + other.runs, self.passed + other.passed)

    def to_dict(self) -> dict[str, Any]:
        return {"runs": self.runs, "passed": self.passed, "rate": round(self.rate, 4)}


def fisher_one_sided_p(weaker: PassCount, stronger: PassCount) -> float:
    """Exact one-sided Fisher p-value for "the weaker model passes more often":
    the hypergeometric probability of the weaker model seeing at least its
    observed passes when both models share one pass rate."""
    if not isinstance(weaker, PassCount) or not isinstance(stronger, PassCount):
        raise TypeError("fisher test consumes PassCount values")
    total = weaker.runs + stronger.runs
    successes = weaker.passed + stronger.passed
    denominator = math.comb(total, weaker.runs)
    upper = min(successes, weaker.runs)
    tail = sum(math.comb(successes, k) * math.comb(total - successes, weaker.runs - k)
               for k in range(weaker.passed, upper + 1))
    return min(1.0, tail / denominator)


@dataclass(frozen=True)
class ModelOrderInversion:
    scope: str
    variant: ExecutionVariant
    weaker: ModelId
    stronger: ModelId
    weaker_count: PassCount
    stronger_count: PassCount

    def __post_init__(self) -> None:
        object.__setattr__(self, "scope", _label(self.scope, "inversion scope"))
        if not isinstance(self.variant, ExecutionVariant):
            raise TypeError("inversion variant must be ExecutionVariant")
        if not isinstance(self.weaker, ModelId) or not isinstance(self.stronger, ModelId):
            raise TypeError("inversion models must be ModelId")
        if self.weaker == self.stronger:
            raise ValueError("an inversion compares two different models")
        if not isinstance(self.weaker_count, PassCount) or not isinstance(self.stronger_count, PassCount):
            raise TypeError("inversion counts must be PassCount")
        if self.weaker_count.rate <= self.stronger_count.rate:
            raise ValueError("an inversion requires the weaker model to pass strictly more often")

    @property
    def p_value(self) -> float:
        return fisher_one_sided_p(self.weaker_count, self.stronger_count)

    @property
    def significant(self) -> bool:
        return self.p_value <= SIGNIFICANCE_ALPHA

    def to_dict(self) -> dict[str, Any]:
        return {"scope": self.scope, "variant": str(self.variant), "weaker": str(self.weaker),
                "stronger": str(self.stronger), "weaker_pass": self.weaker_count.to_dict(),
                "stronger_pass": self.stronger_count.to_dict(),
                "p_value": round(self.p_value, 6), "significant": self.significant}


@dataclass(frozen=True)
class RunPass:
    """Whether one scorable run passed every objective gate."""

    run: RunRef
    passed: bool

    def __post_init__(self) -> None:
        if not isinstance(self.run, RunRef):
            raise TypeError("run pass run must be RunRef")
        if not isinstance(self.passed, bool):
            raise TypeError("run pass passed must be boolean")


@dataclass(frozen=True)
class ModelOrderCheck:
    order: ModelOrder
    inversions: tuple[ModelOrderInversion, ...]
    compared_pairs: int
    unordered_models: tuple[str, ...]
    unobserved_models: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "availability": "complete", "evidence_class": DIAGNOSTIC_EVIDENCE_CLASS,
            "order_weakest_first": self.order.to_list(), "compared_pairs": self.compared_pairs,
            "unordered_models": list(self.unordered_models), "unobserved_models": list(self.unobserved_models),
            "inversions": [inversion.to_dict() for inversion in self.inversions],
            "significant_inversions": sum(1 for inversion in self.inversions if inversion.significant),
        }


def model_order_check(passes: Iterable[RunPass], order: ModelOrder) -> ModelOrderCheck:
    """Compare every declared weaker/stronger pair per (case, arm), and at
    suite level per arm pooled only over cases both models ran, so pooling
    never compares different case mixes."""
    if not isinstance(order, ModelOrder):
        raise TypeError("model order check requires a declared ModelOrder")
    rows = list(passes)
    if not all(isinstance(row, RunPass) for row in rows):
        raise TypeError("model order check consumes RunPass values")
    if len({row.run for row in rows}) != len(rows):
        raise ValueError("each run may be counted once")
    counts: dict[tuple[CaseId, ExecutionVariant, ModelId], PassCount] = {}
    seen_models: set[str] = set()
    for row in rows:
        if row.run.model is not None:
            seen_models.add(str(row.run.model))
        if order.rank(row.run.model) is None or row.run.model is None:
            continue
        key = (row.run.case_id, row.run.variant, row.run.model)
        one = PassCount(1, 1 if row.passed else 0)
        counts[key] = counts[key].plus(one) if key in counts else one
    inversions: list[ModelOrderInversion] = []
    compared = 0
    cases = sorted({case for case, _, _ in counts})
    variants = sorted({variant for _, variant, _ in counts})
    for variant in variants:
        for i, weaker in enumerate(order.models):
            for stronger in order.models[i + 1:]:
                pooled_weak: PassCount | None = None
                pooled_strong: PassCount | None = None
                for case in cases:
                    weak = counts.get((case, variant, weaker))
                    strong = counts.get((case, variant, stronger))
                    if weak is None or strong is None:
                        continue
                    compared += 1
                    pooled_weak = weak if pooled_weak is None else pooled_weak.plus(weak)
                    pooled_strong = strong if pooled_strong is None else pooled_strong.plus(strong)
                    if weak.rate > strong.rate:
                        inversions.append(ModelOrderInversion(str(case), variant, weaker, stronger, weak, strong))
                if pooled_weak is not None and pooled_strong is not None and pooled_weak.rate > pooled_strong.rate:
                    inversions.append(ModelOrderInversion(
                        SUITE_SCOPE, variant, weaker, stronger, pooled_weak, pooled_strong))
    ordered = {str(model) for model in order.models}
    return ModelOrderCheck(
        order=order,
        inversions=tuple(sorted(inversions, key=lambda item: (
            item.scope != SUITE_SCOPE, item.scope, str(item.variant), str(item.weaker), str(item.stronger)))),
        compared_pairs=compared,
        unordered_models=tuple(sorted(seen_models - ordered)),
        unobserved_models=tuple(sorted(ordered - seen_models)),
    )


def model_order_not_declared() -> dict[str, Any]:
    return {"availability": "not_applicable", "reason": "no model order declared (benchmark --model-order weakest,...,strongest)"}


def suspicion_summary(suspicions: Sequence[VerifierSuspicion]) -> dict[str, Any]:
    signals = {signal.value: 0 for signal in VerifierSignal}
    for suspicion in suspicions:
        signals[suspicion.signal.value] += 1
    queue: dict[RunRef, set[str]] = {}
    for suspicion in suspicions:
        for run in suspicion.runs:
            queue.setdefault(run, set()).add(f"{suspicion.assertion}:{suspicion.signal.value}")
    return {
        "availability": "complete", "evidence_class": DIAGNOSTIC_EVIDENCE_CLASS,
        "signals": signals,
        "suspicions": [suspicion.to_dict() for suspicion in suspicions],
        "review_queue": [{**run.to_dict(), "suspects": sorted(names)}
                         for run, names in sorted(queue.items(), key=lambda item: item[0].sort_key())],
    }


def verdict_mapping(row: Mapping[str, Any]) -> tuple[bool, bool] | None:
    """(passed, gate) from a graded assertion entry, or None when the verdict
    is not a complete boolean — an unavailable check is not evidence."""
    passed = row.get("passed")
    if not isinstance(passed, bool) or row.get("availability", "complete") != "complete":
        return None
    return passed, row.get("severity", "gate") in {"gate", "critical"}
