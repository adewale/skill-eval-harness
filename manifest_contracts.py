"""Typed identity values shared by manifest planning and prepared tasks.

The manifest remains an ordinary JSON/YAML mapping at the wire boundary. These
``str`` subclasses are the validated in-process values: they serialize exactly
like the legacy strings while preventing an unchecked string from masquerading
as a split, case kind, or execution arm in typed code.
"""
from __future__ import annotations

from enum import Enum

ABLATION_VARIANT_PREFIX = "ablation:"


class Split(str):
    """One of the three closed evaluation phases."""

    _VALUES = ("tune", "holdout", "holdback")

    def __new__(cls, value: str) -> Split:
        if not isinstance(value, str):
            raise TypeError("split must be a string")
        if value not in cls._VALUES:
            raise ValueError(f"split must be one of {list(cls._VALUES)!r}")
        return str.__new__(cls, value)

    @classmethod
    def parse(cls, value: object) -> Split:
        if not isinstance(value, str):
            raise ValueError("split must be a string")
        return cls(value)

    @classmethod
    def values(cls) -> tuple[str, ...]:
        return cls._VALUES


class CasePopulation(str, Enum):
    ANSWER = "answer"
    TRIGGER = "trigger"


class CaseKind(str):
    """A non-empty descriptive case kind with one structural population."""

    def __new__(cls, value: str) -> CaseKind:
        if not isinstance(value, str):
            raise TypeError("case kind must be a string")
        if not value.strip():
            raise ValueError("case kind must be a non-empty string")
        return str.__new__(cls, value)

    @classmethod
    def parse(cls, value: object) -> CaseKind:
        if not isinstance(value, str):
            raise ValueError("case kind must be a string")
        return cls(value)

    @property
    def population(self) -> CasePopulation:
        return CasePopulation.TRIGGER if self == "trigger" else CasePopulation.ANSWER


class ExecutionVariant(str):
    """A base, historical, or declared-ablation execution arm."""

    _BASE_VALUES = ("with_skill", "without_skill", "old_skill")

    def __new__(cls, value: str) -> ExecutionVariant:
        if not isinstance(value, str):
            raise TypeError("execution variant must be a string")
        if value not in cls._BASE_VALUES:
            if not value.startswith(ABLATION_VARIANT_PREFIX):
                raise ValueError("execution variant is not a supported arm")
            ablation_id = value.removeprefix(ABLATION_VARIANT_PREFIX)
            if not ablation_id:
                raise ValueError("ablation execution variant must include an id")
        return str.__new__(cls, value)

    @classmethod
    def parse(cls, value: object) -> ExecutionVariant:
        if not isinstance(value, str):
            raise ValueError("execution variant must be a string")
        return cls(value)

    @classmethod
    def base_values(cls) -> tuple[str, ...]:
        return cls._BASE_VALUES

    @classmethod
    def ablation(cls, ablation_id: object) -> ExecutionVariant:
        if not isinstance(ablation_id, str) or not ablation_id:
            raise ValueError("ablation execution variant must include a string id")
        return cls(f"{ABLATION_VARIANT_PREFIX}{ablation_id}")

    @property
    def is_ablation(self) -> bool:
        return self.startswith(ABLATION_VARIANT_PREFIX)

    @property
    def ablation_id(self) -> str | None:
        if not self.is_ablation:
            return None
        return self.removeprefix(ABLATION_VARIANT_PREFIX)


WITH_SKILL = ExecutionVariant("with_skill")
WITHOUT_SKILL = ExecutionVariant("without_skill")
OLD_SKILL = ExecutionVariant("old_skill")
DEFAULT_EXECUTION_VARIANTS = (WITH_SKILL, WITHOUT_SKILL)


def is_ablation_variant(variant: object) -> bool:
    """Compatibility predicate for untrusted/legacy values at wire boundaries."""
    return str(variant).startswith(ABLATION_VARIANT_PREFIX)


def ablation_id_of(variant: object) -> str | None:
    """Return the id encoded by ``ablation:<id>``, else ``None``."""
    value = str(variant)
    if not value.startswith(ABLATION_VARIANT_PREFIX):
        return None
    return value.removeprefix(ABLATION_VARIANT_PREFIX)
