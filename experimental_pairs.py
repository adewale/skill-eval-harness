"""Validated experimental identities and matched-arm construction.

Causal comparisons consume :class:`ExperimentalPair` values, never two
independently grouped lists.  A pair therefore cannot cross case, model,
repetition, or population boundaries and duplicate arms are rejected before a
metric is computed.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Iterable, Literal, Mapping

ArmName = Literal["with_skill", "without_skill"]


class ExperimentalPopulation(str, Enum):
    ANSWER = "answer"
    TRIGGER = "trigger"
    JUDGE = "judge"
    STATIC = "static"


@dataclass(frozen=True, order=True)
class ExperimentalPairKey:
    case_id: str
    model: str | None
    run_number: int
    population: str

    def __post_init__(self) -> None:
        if not isinstance(self.case_id, str) or not self.case_id.strip():
            raise ValueError("pair case_id must be a non-empty string")
        if self.model is not None and (not isinstance(self.model, str) or not self.model.strip()):
            raise ValueError("pair model must be null or a non-empty string")
        if isinstance(self.run_number, bool) or not isinstance(self.run_number, int) or self.run_number < 1:
            raise ValueError("pair run_number must be a positive integer")
        try:
            object.__setattr__(self, "population", ExperimentalPopulation(self.population).value)
        except ValueError as exc:
            raise ValueError(f"unknown experimental population: {self.population!r}") from exc

    @classmethod
    def from_row(cls, row: Mapping[str, Any], *, population: str) -> "ExperimentalPairKey":
        if "case_id" not in row:
            raise ValueError("experimental row is missing case_id")
        if not isinstance(row["case_id"], str):
            raise ValueError("experimental row case_id must be a string")
        if "run_number" not in row:
            raise ValueError("experimental row is missing run_number")
        row_population = row.get("population")
        if row_population is not None and row_population != population:
            raise ValueError(
                f"experimental row population {row_population!r} conflicts with {population!r}"
            )
        model = row.get("model")
        return cls(row["case_id"], model, row["run_number"], population)

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "model": self.model,
            "run_number": self.run_number,
            "population": self.population,
        }


@dataclass(frozen=True)
class ExperimentalArm:
    key: ExperimentalPairKey
    arm: ArmName
    payload: Any
    eligible: bool = True
    blocked_reason: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.key, ExperimentalPairKey):
            raise TypeError("experimental arm key must be ExperimentalPairKey")
        if self.arm not in ("with_skill", "without_skill"):
            raise ValueError(f"unknown experimental arm: {self.arm!r}")
        if not isinstance(self.eligible, bool):
            raise TypeError("experimental arm eligible must be boolean")
        if self.eligible and self.blocked_reason is not None:
            raise ValueError("eligible arm cannot carry a blocked reason")
        if not self.eligible and (not isinstance(self.blocked_reason, str) or not self.blocked_reason):
            raise ValueError("ineligible arm requires a blocked reason")


@dataclass(frozen=True)
class ExperimentalPair:
    key: ExperimentalPairKey
    with_skill: ExperimentalArm
    without_skill: ExperimentalArm

    def __post_init__(self) -> None:
        if self.with_skill.key != self.key or self.without_skill.key != self.key:
            raise ValueError("experimental pair arms must have exactly matching identities")
        if self.with_skill.arm != "with_skill" or self.without_skill.arm != "without_skill":
            raise ValueError("experimental pair must contain one arm of each kind")
        if not self.with_skill.eligible or not self.without_skill.eligible:
            raise ValueError("experimental pair cannot contain an ineligible arm")


@dataclass(frozen=True)
class BlockedExperimentalPair:
    key: ExperimentalPairKey
    reason: str

    def __post_init__(self) -> None:
        if not isinstance(self.key, ExperimentalPairKey):
            raise TypeError("blocked pair key must be ExperimentalPairKey")
        if not isinstance(self.reason, str) or not self.reason:
            raise ValueError("blocked experimental pair requires a reason")

    def to_dict(self) -> dict[str, Any]:
        return {**self.key.to_dict(), "reason": self.reason}


@dataclass(frozen=True)
class PairConstruction:
    pairs: tuple[ExperimentalPair, ...]
    blocked: tuple[BlockedExperimentalPair, ...]

    def diagnostics(self) -> dict[str, Any]:
        reason_counts: dict[str, int] = {}
        for item in self.blocked:
            reason_counts[item.reason] = reason_counts.get(item.reason, 0) + 1
        return {
            "eligible_pairs": len(self.pairs),
            "blocked_pairs": len(self.blocked),
            "blocked_reason_counts": dict(sorted(reason_counts.items())),
        }


def construct_pairs(arms: Iterable[ExperimentalArm]) -> PairConstruction:
    """Build matched pairs and reject duplicate observations for either arm."""
    indexed: dict[ExperimentalPairKey, dict[ArmName, ExperimentalArm]] = {}
    for observation in arms:
        slots = indexed.setdefault(observation.key, {})
        if observation.arm in slots:
            raise ValueError(
                "duplicate experimental arm for "
                f"{observation.key.to_dict()}: {observation.arm}"
            )
        slots[observation.arm] = observation

    pairs: list[ExperimentalPair] = []
    blocked: list[BlockedExperimentalPair] = []
    for key in sorted(indexed):
        slots = indexed[key]
        left = slots.get("with_skill")
        right = slots.get("without_skill")
        if left is None:
            blocked.append(BlockedExperimentalPair(key, "missing_with_skill"))
        elif right is None:
            blocked.append(BlockedExperimentalPair(key, "missing_without_skill"))
        elif not left.eligible:
            blocked.append(BlockedExperimentalPair(key, str(left.blocked_reason)))
        elif not right.eligible:
            blocked.append(BlockedExperimentalPair(key, str(right.blocked_reason)))
        else:
            pairs.append(ExperimentalPair(key, left, right))
    return PairConstruction(tuple(pairs), tuple(blocked))


def pairs_from_rows(
    rows: Iterable[Mapping[str, Any]],
    *,
    population: str,
    eligibility: Callable[[Mapping[str, Any]], tuple[bool, str | None]] | None = None,
) -> PairConstruction:
    """Parse untrusted result rows into arms, then construct validated pairs."""
    arms: list[ExperimentalArm] = []
    for row in rows:
        arm = row.get("variant")
        if arm not in ("with_skill", "without_skill"):
            continue
        key = ExperimentalPairKey.from_row(row, population=population)
        eligible, reason = eligibility(row) if eligibility is not None else (True, None)
        arms.append(ExperimentalArm(key, arm, row, eligible, reason))
    return construct_pairs(arms)
