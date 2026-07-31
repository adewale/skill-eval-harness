"""Immutable invocation results for qualitative judge backends.

Provider adapters and the shell-command escape hatch construct this type once at
the process boundary.  Verdict parsing may therefore consume one closed shape
instead of repairing independently assembled dictionaries.
"""
from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

import telemetry
from json_contracts import freeze_json_mapping

JudgeUsageSource = Literal["provider_reported", "trace_normalized"]
JUDGE_USAGE_SOURCES = frozenset({"provider_reported", "trace_normalized"})


def _finite_nonnegative(value: Any, label: str) -> int | float:
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(float(value)) or value < 0):
        raise ValueError(f"{label} must be finite and non-negative")
    return value


def _validate_usage(value: Any, label: str, key: str | None = None) -> None:
    if isinstance(value, Mapping):
        for child_key, item in value.items():
            if not isinstance(child_key, str):
                raise TypeError(f"{label} object keys must be strings")
            _validate_usage(item, f"{label}.{child_key}", child_key)
        return
    _finite_nonnegative(value, label)
    if key is not None and key.casefold().endswith("tokens") and type(value) is not int:
        raise ValueError(f"{label} must be a non-negative integer")


@dataclass(frozen=True)
class JudgeInvocation:
    """One completed attempt to obtain a verdict from a judge backend.

    A nonzero ``returncode`` is a valid invocation record, but never a successful
    observation.  Provider output may be empty on failure; identity and telemetry
    remain independently validated and immutable for diagnostics and accounting.
    """

    stdout: str
    stderr: str
    returncode: int
    usage: Mapping[str, Any] | None = None
    cost_usd: float | None = None
    usage_source: JudgeUsageSource = "provider_reported"
    model_label: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.stdout, str) or not isinstance(self.stderr, str):
            raise TypeError("judge stdout and stderr must be strings")
        if type(self.returncode) is not int:
            raise TypeError("judge returncode must be an integer")
        if self.usage_source not in JUDGE_USAGE_SOURCES:
            raise ValueError(
                f"unknown judge usage source {self.usage_source!r}")
        if self.model_label is not None and (
                not isinstance(self.model_label, str) or not self.model_label.strip()):
            raise ValueError("judge model label must be None or a non-empty string")
        if self.cost_usd is not None:
            _finite_nonnegative(self.cost_usd, "judge cost_usd")
            object.__setattr__(self, "cost_usd", float(self.cost_usd))
        if self.usage is not None:
            if not isinstance(self.usage, Mapping):
                raise TypeError("judge usage must be a mapping or None")
            _validate_usage(self.usage, "judge usage")
            frozen_usage = freeze_json_mapping(self.usage, "judge usage")
            telemetry.canonical_usage_counts(frozen_usage)
            object.__setattr__(self, "usage", frozen_usage)

    @property
    def succeeded(self) -> bool:
        return self.returncode == 0
