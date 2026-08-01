"""Validated experimental identities and matched-arm construction.

Causal comparisons consume :class:`ExperimentalPair` values, never two
independently grouped lists.  A pair therefore cannot cross case, model,
repetition, or population boundaries and duplicate arms are rejected before a
metric is computed.
"""
from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Any, Generic, TypeVar

from manifest_contracts import CaseId, ModelId, RunNumber

PayloadT = TypeVar("PayloadT")


class ExperimentalArmId(str):
    """One wire arm name selected by an explicit contrast."""

    def __new__(cls, value: str) -> ExperimentalArmId:
        if not isinstance(value, str):
            raise TypeError("experimental arm id must be a string")
        if not value.strip():
            raise ValueError("experimental arm id must be non-empty")
        return str.__new__(cls, value)


class ExperimentalFactor(str, Enum):
    ACTIVATION = "activation"
    SKILL_SET = "skill_set"
    CONTENT_REVISION = "content_revision"


@dataclass(frozen=True, order=True)
class FactorCoordinate:
    factor: ExperimentalFactor
    level: str

    def __post_init__(self) -> None:
        try:
            object.__setattr__(self, "factor", ExperimentalFactor(self.factor))
        except ValueError as exc:
            raise ValueError(f"unknown experimental factor: {self.factor!r}") from exc
        if not isinstance(self.level, str) or not self.level.strip():
            raise ValueError("experimental factor level must be non-empty")


@dataclass(frozen=True)
class TreatmentCoordinate:
    factors: tuple[FactorCoordinate, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.factors, tuple) or not all(
            isinstance(item, FactorCoordinate) for item in self.factors
        ):
            raise TypeError("treatment factors must be a tuple of FactorCoordinate values")
        ordered = tuple(sorted(self.factors))
        if len({item.factor for item in ordered}) != len(ordered):
            raise ValueError("treatment coordinate cannot repeat a factor")
        object.__setattr__(self, "factors", ordered)


@dataclass(frozen=True)
class ContrastSpec:
    """One declared binary comparison and both of its treatment coordinates."""

    contrast_id: str
    treatment_arm: ExperimentalArmId
    control_arm: ExperimentalArmId
    treatment: TreatmentCoordinate
    control: TreatmentCoordinate

    def __post_init__(self) -> None:
        if not isinstance(self.contrast_id, str) or not self.contrast_id.strip():
            raise ValueError("experimental contrast id must be non-empty")
        object.__setattr__(
            self, "treatment_arm", ExperimentalArmId(self.treatment_arm))
        object.__setattr__(self, "control_arm", ExperimentalArmId(self.control_arm))
        if self.treatment_arm == self.control_arm:
            raise ValueError("experimental contrast arms must be distinct")
        if not isinstance(self.treatment, TreatmentCoordinate) or not isinstance(
            self.control, TreatmentCoordinate
        ):
            raise TypeError("experimental contrast coordinates must be TreatmentCoordinate")
        if self.treatment == self.control:
            raise ValueError("experimental contrast coordinates must be distinct")


SKILL_PRESENCE_CONTRAST = ContrastSpec(
    contrast_id="skill_presence",
    treatment_arm=ExperimentalArmId("with_skill"),
    control_arm=ExperimentalArmId("without_skill"),
    treatment=TreatmentCoordinate((
        FactorCoordinate(ExperimentalFactor.ACTIVATION, "forced"),
        FactorCoordinate(ExperimentalFactor.SKILL_SET, "all"),
        FactorCoordinate(ExperimentalFactor.CONTENT_REVISION, "current"),
    )),
    control=TreatmentCoordinate((
        FactorCoordinate(ExperimentalFactor.ACTIVATION, "none"),
        FactorCoordinate(ExperimentalFactor.SKILL_SET, "none"),
        FactorCoordinate(ExperimentalFactor.CONTENT_REVISION, "current"),
    )),
)


class ExperimentalPopulation(str, Enum):
    ANSWER = "answer"
    TRIGGER = "trigger"
    JUDGE = "judge"
    STATIC = "static"

    @classmethod
    def parse(cls, value: object) -> ExperimentalPopulation:
        if not isinstance(value, str):
            raise ValueError("experimental population must be a string")
        try:
            return cls(value)
        except ValueError as exc:
            raise ValueError(f"unknown experimental population: {value!r}") from exc


@dataclass(frozen=True)
class ExperimentalPairKey:
    case_id: CaseId
    model: ModelId | None
    run_number: RunNumber
    population: ExperimentalPopulation

    def __post_init__(self) -> None:
        object.__setattr__(self, "case_id", CaseId.parse(self.case_id))
        object.__setattr__(
            self, "model", None if self.model is None else ModelId.parse(self.model)
        )
        object.__setattr__(self, "run_number", RunNumber.parse(self.run_number))
        object.__setattr__(
            self, "population", ExperimentalPopulation.parse(self.population)
        )

    @classmethod
    def parse(
        cls,
        case_id: object,
        model: object,
        run_number: object,
        population: object,
    ) -> ExperimentalPairKey:
        return cls(
            CaseId.parse(case_id),
            None if model is None else ModelId.parse(model),
            RunNumber.parse(run_number),
            ExperimentalPopulation.parse(population),
        )

    @classmethod
    def from_row(
        cls,
        row: Mapping[str, Any],
        *,
        population: ExperimentalPopulation,
    ) -> ExperimentalPairKey:
        parsed_population = ExperimentalPopulation.parse(population)
        if "case_id" not in row:
            raise ValueError("experimental row is missing case_id")
        if not isinstance(row["case_id"], str):
            raise ValueError("experimental row case_id must be a string")
        if "run_number" not in row:
            raise ValueError("experimental row is missing run_number")
        row_population = row.get("population")
        if row_population is not None and row_population != parsed_population.value:
            raise ValueError(
                "experimental row population "
                f"{row_population!r} conflicts with {parsed_population.value!r}"
            )
        model = row.get("model")
        return cls.parse(row["case_id"], model, row["run_number"], parsed_population)

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "model": self.model,
            "run_number": self.run_number,
            "population": self.population.value,
        }


@dataclass(frozen=True)
class ExperimentalArm(Generic[PayloadT]):
    key: ExperimentalPairKey
    arm: ExperimentalArmId
    payload: PayloadT
    eligible: bool = True
    blocked_reason: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.key, ExperimentalPairKey):
            raise TypeError("experimental arm key must be ExperimentalPairKey")
        object.__setattr__(self, "arm", ExperimentalArmId(self.arm))
        if not isinstance(self.eligible, bool):
            raise TypeError("experimental arm eligible must be boolean")
        if self.eligible and self.blocked_reason is not None:
            raise ValueError("eligible arm cannot carry a blocked reason")
        if not self.eligible and (not isinstance(self.blocked_reason, str) or not self.blocked_reason):
            raise ValueError("ineligible arm requires a blocked reason")


@dataclass(frozen=True)
class ExperimentalPair(Generic[PayloadT]):
    key: ExperimentalPairKey
    contrast: ContrastSpec
    treatment: ExperimentalArm[PayloadT]
    control: ExperimentalArm[PayloadT]

    def __post_init__(self) -> None:
        if not isinstance(self.key, ExperimentalPairKey):
            raise TypeError("experimental pair key must be ExperimentalPairKey")
        if not isinstance(self.contrast, ContrastSpec):
            raise TypeError("experimental pair contrast must be ContrastSpec")
        if self.treatment.key != self.key or self.control.key != self.key:
            raise ValueError("experimental pair arms must have exactly matching identities")
        if (self.treatment.arm != self.contrast.treatment_arm
                or self.control.arm != self.contrast.control_arm):
            raise ValueError("experimental pair arms do not match its contrast")
        if not self.treatment.eligible or not self.control.eligible:
            raise ValueError("experimental pair cannot contain an ineligible arm")

    @property
    def with_skill(self) -> ExperimentalArm[PayloadT]:
        """Compatibility projection for the default skill-presence contrast."""
        if self.contrast != SKILL_PRESENCE_CONTRAST:
            raise AttributeError("with_skill is only defined for the skill-presence contrast")
        return self.treatment

    @property
    def without_skill(self) -> ExperimentalArm[PayloadT]:
        """Compatibility projection for the default skill-presence contrast."""
        if self.contrast != SKILL_PRESENCE_CONTRAST:
            raise AttributeError("without_skill is only defined for the skill-presence contrast")
        return self.control


@dataclass(frozen=True)
class BlockedExperimentalPair:
    key: ExperimentalPairKey
    reason: str
    contrast_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.key, ExperimentalPairKey):
            raise TypeError("blocked pair key must be ExperimentalPairKey")
        if not isinstance(self.reason, str) or not self.reason:
            raise ValueError("blocked experimental pair requires a reason")
        if not isinstance(self.contrast_id, str) or not self.contrast_id.strip():
            raise ValueError("blocked experimental pair requires a contrast id")

    def to_dict(self) -> dict[str, Any]:
        return {
            "contrast_id": self.contrast_id,
            **self.key.to_dict(),
            "reason": self.reason,
        }


@dataclass(frozen=True)
class PairConstruction(Generic[PayloadT]):
    contrast: ContrastSpec
    pairs: tuple[ExperimentalPair[PayloadT], ...]
    blocked: tuple[BlockedExperimentalPair, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.contrast, ContrastSpec):
            raise TypeError("pair construction contrast must be ContrastSpec")
        if not isinstance(self.pairs, tuple) or not all(
            isinstance(item, ExperimentalPair) for item in self.pairs
        ):
            raise TypeError("constructed pairs must be a tuple of ExperimentalPair values")
        if not isinstance(self.blocked, tuple) or not all(
            isinstance(item, BlockedExperimentalPair) for item in self.blocked
        ):
            raise TypeError("blocked pairs must be a tuple of BlockedExperimentalPair values")
        pair_keys = [item.key for item in self.pairs]
        blocked_keys = [item.key for item in self.blocked]
        if any(item.contrast != self.contrast for item in self.pairs):
            raise ValueError("constructed pairs must use the construction contrast")
        if any(item.contrast_id != self.contrast.contrast_id for item in self.blocked):
            raise ValueError("blocked pairs must use the construction contrast")
        if len(set(pair_keys)) != len(pair_keys) or len(set(blocked_keys)) != len(blocked_keys):
            raise ValueError("pair construction cannot repeat an experimental identity")
        if set(pair_keys) & set(blocked_keys):
            raise ValueError("an experimental identity cannot be paired and blocked")

    def diagnostics(self) -> dict[str, Any]:
        reason_counts: dict[str, int] = {}
        for item in self.blocked:
            reason_counts[item.reason] = reason_counts.get(item.reason, 0) + 1
        return {
            "contrast_id": self.contrast.contrast_id,
            "eligible_pairs": len(self.pairs),
            "blocked_pairs": len(self.blocked),
            "blocked_reason_counts": dict(sorted(reason_counts.items())),
        }


def construct_pairs(
    arms: Iterable[ExperimentalArm[PayloadT]],
    *,
    contrast: ContrastSpec = SKILL_PRESENCE_CONTRAST,
) -> PairConstruction[PayloadT]:
    """Build matched pairs and reject duplicate observations for either arm."""
    indexed: dict[
        ExperimentalPairKey, dict[ExperimentalArmId, ExperimentalArm[PayloadT]]
    ] = {}
    for observation in arms:
        if not isinstance(observation, ExperimentalArm):
            raise TypeError("pair construction requires ExperimentalArm values")
        if observation.arm not in {contrast.treatment_arm, contrast.control_arm}:
            raise ValueError(
                f"experimental arm {observation.arm!r} is not part of "
                f"contrast {contrast.contrast_id!r}"
            )
        slots = indexed.setdefault(observation.key, {})
        if observation.arm in slots:
            raise ValueError(
                "duplicate experimental arm for "
                f"{observation.key.to_dict()}: {observation.arm}"
            )
        slots[observation.arm] = observation

    pairs: list[ExperimentalPair[PayloadT]] = []
    blocked: list[BlockedExperimentalPair] = []
    for key in sorted(indexed, key=lambda item: (
            item.case_id, item.model or "", item.run_number, item.population.value)):
        slots = indexed[key]
        left = slots.get(contrast.treatment_arm)
        right = slots.get(contrast.control_arm)
        if left is None:
            blocked.append(BlockedExperimentalPair(
                key, f"missing_{contrast.treatment_arm}", contrast.contrast_id))
        elif right is None:
            blocked.append(BlockedExperimentalPair(
                key, f"missing_{contrast.control_arm}", contrast.contrast_id))
        elif not left.eligible:
            blocked.append(BlockedExperimentalPair(
                key, str(left.blocked_reason), contrast.contrast_id))
        elif not right.eligible:
            blocked.append(BlockedExperimentalPair(
                key, str(right.blocked_reason), contrast.contrast_id))
        else:
            pairs.append(ExperimentalPair(key, contrast, left, right))
    return PairConstruction(contrast, tuple(pairs), tuple(blocked))


def pairs_from_rows(
    rows: Iterable[Mapping[str, Any]],
    *,
    population: ExperimentalPopulation,
    eligibility: Callable[[Mapping[str, Any]], tuple[bool, str | None]] | None = None,
    contrast: ContrastSpec = SKILL_PRESENCE_CONTRAST,
) -> PairConstruction[Mapping[str, Any]]:
    """Parse untrusted result rows into arms, then construct validated pairs."""
    arms: list[ExperimentalArm[Mapping[str, Any]]] = []
    for row in rows:
        arm = row.get("variant")
        if arm not in (contrast.treatment_arm, contrast.control_arm):
            continue
        key = ExperimentalPairKey.from_row(row, population=population)
        eligible, reason = eligibility(row) if eligibility is not None else (True, None)
        assert isinstance(arm, str)
        arms.append(ExperimentalArm(
            key, ExperimentalArmId(arm), row, eligible, reason))
    return construct_pairs(arms, contrast=contrast)
