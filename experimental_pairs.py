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

from completion_contracts import effort_identity
from manifest_contracts import (
    OLD_SKILL,
    WITH_SKILL,
    WITHOUT_SKILL,
    CaseId,
    ExecutionVariant,
    ModelId,
    RunNumber,
)

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


class HeldFixedFactor(str, Enum):
    """A condition both arms of a pair must share for their difference to be the skill's.

    A contrast varies one ``ExperimentalFactor``; everything named here must be
    equal between the two arms, or the pair is blocked. Each factor is read
    from a result row or run metadata by one reader.
    """

    EFFORT = "effort"


_HELD_FIXED_READERS: dict[HeldFixedFactor, Callable[[Mapping[str, Any]], str | None]] = {
    HeldFixedFactor.EFFORT: effort_identity,
}


@dataclass(frozen=True)
class ContrastSpec:
    """One declared binary comparison and both of its treatment coordinates.

    ``held_fixed`` names the conditions that must match between the arms of a
    pair, so an effort difference cannot pass as a skill effect.
    """

    contrast_id: str
    treatment_arm: ExperimentalArmId
    control_arm: ExperimentalArmId
    treatment: TreatmentCoordinate
    control: TreatmentCoordinate
    held_fixed: tuple[HeldFixedFactor, ...] = (HeldFixedFactor.EFFORT,)

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
        held = tuple(HeldFixedFactor(item) for item in self.held_fixed)
        if len(set(held)) != len(held):
            raise ValueError("a held-fixed factor can be named only once")
        object.__setattr__(self, "held_fixed", held)

    def comparability(self, left: Mapping[str, Any], right: Mapping[str, Any]) -> str | None:
        """The block reason when two arms did not share a held-fixed condition.

        Rows recorded before a factor existed carry none, and two such rows
        still pair. One recorded arm against one unrecorded arm cannot be shown
        to match, so that pair is blocked rather than trusted.
        """
        for factor in self.held_fixed:
            reader = _HELD_FIXED_READERS[factor]
            left_value, right_value = reader(left), reader(right)
            if left_value == right_value:
                continue
            if left_value is None or right_value is None:
                return f"{factor.value}_unrecorded_on_one_arm"
            return f"{factor.value}_mismatch"
        return None


SKILL_PRESENCE_CONTRAST = ContrastSpec(
    contrast_id="skill_presence",
    treatment_arm=ExperimentalArmId(WITH_SKILL),
    control_arm=ExperimentalArmId(WITHOUT_SKILL),
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


# The current skill against the revision it replaces, in the same run.
EDIT_CONTRAST = ContrastSpec(
    contrast_id="skill_edit",
    treatment_arm=ExperimentalArmId(WITH_SKILL),
    control_arm=ExperimentalArmId(OLD_SKILL),
    treatment=TreatmentCoordinate((
        FactorCoordinate(ExperimentalFactor.ACTIVATION, "forced"),
        FactorCoordinate(ExperimentalFactor.SKILL_SET, "all"),
        FactorCoordinate(ExperimentalFactor.CONTENT_REVISION, "current"),
    )),
    control=TreatmentCoordinate((
        FactorCoordinate(ExperimentalFactor.ACTIVATION, "forced"),
        FactorCoordinate(ExperimentalFactor.SKILL_SET, "all"),
        FactorCoordinate(ExperimentalFactor.CONTENT_REVISION, "previous"),
    )),
)


def ablation_contrast(variant: object) -> ContrastSpec:
    """The full skill against the same skill with one declared component removed."""
    arm = ExecutionVariant.parse(variant)
    ablation_id = arm.ablation_id
    if ablation_id is None:
        raise ValueError(f"{arm!r} is not an ablation arm")
    return ContrastSpec(
        contrast_id=f"ablation:{ablation_id}",
        treatment_arm=ExperimentalArmId(WITH_SKILL),
        control_arm=ExperimentalArmId(arm),
        treatment=TreatmentCoordinate((
            FactorCoordinate(ExperimentalFactor.ACTIVATION, "forced"),
            FactorCoordinate(ExperimentalFactor.SKILL_SET, "all"),
            FactorCoordinate(ExperimentalFactor.CONTENT_REVISION, "current"),
        )),
        control=TreatmentCoordinate((
            FactorCoordinate(ExperimentalFactor.ACTIVATION, "forced"),
            FactorCoordinate(ExperimentalFactor.SKILL_SET, f"without:{ablation_id}"),
            FactorCoordinate(ExperimentalFactor.CONTENT_REVISION, "current"),
        )),
    )


def contrast_for(treatment: object, control: object) -> ContrastSpec:
    """The declared contrast between two execution arms, or an error.

    Every comparison names a contrast, so an arm is never relabelled into
    another arm's slot to reuse a pair constructor.
    """
    treatment_arm = ExecutionVariant.parse(treatment)
    control_arm = ExecutionVariant.parse(control)
    if treatment_arm == WITH_SKILL:
        if control_arm == WITHOUT_SKILL:
            return SKILL_PRESENCE_CONTRAST
        if control_arm == OLD_SKILL:
            return EDIT_CONTRAST
        if control_arm.is_ablation:
            return ablation_contrast(control_arm)
    raise ValueError(f"no declared contrast compares {treatment_arm!r} with {control_arm!r}")


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
    # Identities whose two arms both declare nothing the metric measures (a
    # case with no objective assertion, for an objective rate): out of scope,
    # so neither paired nor blocked.
    not_applicable: tuple[ExperimentalPairKey, ...] = ()

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
        if not isinstance(self.not_applicable, tuple) or not all(
            isinstance(item, ExperimentalPairKey) for item in self.not_applicable
        ):
            raise TypeError("not-applicable identities must be a tuple of ExperimentalPairKey values")
        pair_keys = [item.key for item in self.pairs]
        blocked_keys = [item.key for item in self.blocked]
        if any(item.contrast != self.contrast for item in self.pairs):
            raise ValueError("constructed pairs must use the construction contrast")
        if any(item.contrast_id != self.contrast.contrast_id for item in self.blocked):
            raise ValueError("blocked pairs must use the construction contrast")
        if any(len(set(keys)) != len(keys)
               for keys in (pair_keys, blocked_keys, list(self.not_applicable))):
            raise ValueError("pair construction cannot repeat an experimental identity")
        if (set(pair_keys) & set(blocked_keys)
                or set(self.not_applicable) & (set(pair_keys) | set(blocked_keys))):
            raise ValueError("an experimental identity cannot be paired and blocked")

    def diagnostics(self) -> dict[str, Any]:
        reason_counts: dict[str, int] = {}
        for item in self.blocked:
            reason_counts[item.reason] = reason_counts.get(item.reason, 0) + 1
        out: dict[str, Any] = {
            "contrast_id": self.contrast.contrast_id,
            "eligible_pairs": len(self.pairs),
            "blocked_pairs": len(self.blocked),
            "blocked_reason_counts": dict(sorted(reason_counts.items())),
        }
        if self.not_applicable:
            out["not_applicable_pairs"] = len(self.not_applicable)
        return out


def construct_pairs(
    arms: Iterable[ExperimentalArm[PayloadT]],
    *,
    contrast: ContrastSpec = SKILL_PRESENCE_CONTRAST,
    comparable: Callable[[PayloadT, PayloadT], str | None] | None = None,
    not_applicable: Callable[[PayloadT], bool] | None = None,
) -> PairConstruction[PayloadT]:
    """Build matched pairs and reject duplicate observations for either arm.

    ``comparable`` returns a block reason when two eligible arms share an
    identity but ran under conditions that make their difference meaningless
    (for example, different effort levels). ``not_applicable`` is true of an
    arm that declares nothing the metric measures; an identity whose two arms
    are both present and both not applicable is recorded as out of scope
    instead of blocked. One such arm beside a measured one still blocks."""
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
    out_of_scope: list[ExperimentalPairKey] = []
    for key in sorted(indexed, key=lambda item: (
            item.case_id, item.model or "", item.run_number, item.population.value)):
        slots = indexed[key]
        left = slots.get(contrast.treatment_arm)
        right = slots.get(contrast.control_arm)
        if (left is not None and right is not None and not_applicable is not None
                and not_applicable(left.payload) and not_applicable(right.payload)):
            out_of_scope.append(key)
        elif left is None:
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
        elif comparable is not None and (
                reason := comparable(left.payload, right.payload)) is not None:
            blocked.append(BlockedExperimentalPair(key, reason, contrast.contrast_id))
        else:
            pairs.append(ExperimentalPair(key, contrast, left, right))
    return PairConstruction(contrast, tuple(pairs), tuple(blocked), tuple(out_of_scope))


def pairs_from_rows(
    rows: Iterable[Mapping[str, Any]],
    *,
    population: ExperimentalPopulation,
    eligibility: Callable[[Mapping[str, Any]], tuple[bool, str | None]] | None = None,
    contrast: ContrastSpec = SKILL_PRESENCE_CONTRAST,
    not_applicable: Callable[[Mapping[str, Any]], bool] | None = None,
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
    return construct_pairs(arms, contrast=contrast, comparable=contrast.comparability,
                           not_applicable=not_applicable)
