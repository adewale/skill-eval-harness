"""Runtime spend ceiling: one closed ledger per paid loop.

`suite-run` gates on a projected spend before any model call. This module is
the runtime half of that policy. A paid loop (native answer backends, the
subagent seam, Jetty, judges) plans a ``SpendLedger`` over the runs it may
start, asks it ``can_start`` before every run, charges each completed run's
cost ``Measurement`` into it, and skips the rest once it says no. The ledger
is the single owner of that state: which runs started, what each cost, which
run's cost could not be observed, which planned runs never started, and why
the loop stopped are all derived from one frozen value, never from
independently writable counters.

Fail-closed like the telemetry it consumes: an unavailable cost is never
charged as zero. It is charged the policy's assumed per-run cost, or it ends
the ledger as an ``UnpricedRun`` whose presence makes the spent total a
partial known subtotal and stops the loop. Persisted ledgers are parsed back
through ``SpendLedger.from_dict`` and must agree with their own records.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Any

from manifest_contracts import CaseId, ExecutionVariant, ModelId, RunNumber
from telemetry import AVAILABLE, COMPLETE, PARTIAL, PROVENANCE, Measurement, Money

SPEND_LEDGER_NAME = "spend-ceiling.json"
SPEND_LEDGER_SCHEMA_VERSION = 1
SPEND_CURRENCY = "USD"


class SpendPopulation(str, Enum):
    ANSWER = "answer"
    JUDGE = "judge"


class SpendStopReason(str, Enum):
    COST_CEILING = "cost_ceiling"
    COST_UNOBSERVABLE = "cost_unobservable"


class SpendObservation(str, Enum):
    """How a loop learns whether its backend can price a run at all."""

    DECLARED = "declared"   # the backend registry declares dollar-cost availability
    RUNTIME = "runtime"     # the cost depends on a caller-supplied function; observe per run


class SpendChargeBasis(str, Enum):
    OBSERVED = "observed"
    ASSUMED = "assumed"


def _usd(value: Any, label: str) -> Money:
    if isinstance(value, bool):
        raise ValueError(f"{label} must be a finite non-negative USD amount")
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{label} must be a finite non-negative USD amount") from exc
    if not amount.is_finite() or amount < 0:
        raise ValueError(f"{label} must be a finite non-negative USD amount")
    return Money(amount, SPEND_CURRENCY)


def _usd_money(value: Any, label: str) -> Money:
    if not isinstance(value, Money):
        raise TypeError(f"{label} must be Money")
    if value.currency != SPEND_CURRENCY:
        raise ValueError(f"{label} must be in {SPEND_CURRENCY}")
    return value


def _label(value: Any) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError("spend row label must be a non-empty string")
    return value


def _reason(value: Any) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError("spend reason must be a non-empty string")
    return value


def _format_money(money: Money) -> str:
    return format(money.amount, "f")


@dataclass(frozen=True)
class SpendPolicy:
    """The operator's ceiling and, optionally, a fixed charge for unpriced runs."""

    ceiling: Money
    assumed_cost_per_run: Money | None = None

    def __post_init__(self) -> None:
        _usd_money(self.ceiling, "spend ceiling")
        if self.assumed_cost_per_run is not None:
            _usd_money(self.assumed_cost_per_run, "assumed cost per run")

    @classmethod
    def from_raw(cls, ceiling: Any, assumed_cost_per_run: Any = None) -> SpendPolicy:
        return cls(
            _usd(ceiling, "spend ceiling"),
            None if assumed_cost_per_run is None else _usd(assumed_cost_per_run, "assumed cost per run"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "ceiling_usd": _format_money(self.ceiling),
            "assumed_cost_per_run_usd": (
                None if self.assumed_cost_per_run is None else _format_money(self.assumed_cost_per_run)),
        }


@dataclass(frozen=True)
class ObservedCharge:
    """A run priced by its own cost measurement."""

    label: str
    amount: Money
    provenance: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "label", _label(self.label))
        _usd_money(self.amount, "observed charge")
        if self.provenance not in PROVENANCE:
            raise ValueError(f"observed charge requires a known provenance, got {self.provenance!r}")

    @property
    def basis(self) -> SpendChargeBasis:
        return SpendChargeBasis.OBSERVED


@dataclass(frozen=True)
class AssumedCharge:
    """A run whose cost was unavailable, charged the policy's assumed amount."""

    label: str
    amount: Money
    reason: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "label", _label(self.label))
        _usd_money(self.amount, "assumed charge")
        object.__setattr__(self, "reason", _reason(self.reason))

    @property
    def basis(self) -> SpendChargeBasis:
        return SpendChargeBasis.ASSUMED


SpendCharge = ObservedCharge | AssumedCharge


@dataclass(frozen=True)
class UnpricedRun:
    """A run that started and finished but whose cost could not be charged.

    It is terminal: the ledger's spent total becomes a partial known subtotal
    and no further run may start."""

    label: str
    reason: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "label", _label(self.label))
        object.__setattr__(self, "reason", _reason(self.reason))


@dataclass(frozen=True)
class PlannedSpendRow:
    """The stable identity of one planned paid run the ledger never started."""

    label: str
    case_id: CaseId
    variant: ExecutionVariant
    run_number: RunNumber
    model: ModelId | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "label", _label(self.label))
        object.__setattr__(self, "case_id", CaseId.parse(self.case_id))
        object.__setattr__(self, "variant", ExecutionVariant.parse(self.variant))
        object.__setattr__(self, "run_number", RunNumber.parse(self.run_number))
        if self.model is not None:
            object.__setattr__(self, "model", ModelId.parse(self.model))

    @classmethod
    def parse(cls, label: Any, case_id: Any, variant: Any, run_number: Any, model: Any = None) -> PlannedSpendRow:
        """The smart constructor for wire values; every field is validated."""
        return cls(_label(label), CaseId.parse(case_id), ExecutionVariant.parse(variant),
                   RunNumber.parse(run_number), None if model is None else ModelId.parse(model))

    def to_dict(self) -> dict[str, Any]:
        return {"label": self.label, "case_id": str(self.case_id), "variant": str(self.variant),
                "run_number": int(self.run_number), "model": None if self.model is None else str(self.model)}

    @classmethod
    def from_dict(cls, data: Any) -> PlannedSpendRow:
        if not isinstance(data, Mapping):
            raise ValueError("planned spend row must be a mapping")
        unknown = set(data) - {"label", "case_id", "variant", "run_number", "model"}
        if unknown:
            raise ValueError(f"planned spend row has unknown field(s): {sorted(map(str, unknown))}")
        return cls.parse(data.get("label"), data.get("case_id"), data.get("variant"),
                         data.get("run_number"), data.get("model"))


def charge_to_dict(charge: SpendCharge) -> dict[str, Any]:
    if isinstance(charge, ObservedCharge):
        return {"label": charge.label, "basis": charge.basis.value,
                "amount_usd": _format_money(charge.amount), "provenance": charge.provenance}
    return {"label": charge.label, "basis": charge.basis.value,
            "amount_usd": _format_money(charge.amount), "reason": charge.reason}


def charge_from_dict(data: Any) -> SpendCharge:
    if not isinstance(data, Mapping):
        raise ValueError("spend charge must be a mapping")
    basis = data.get("basis")
    if basis == SpendChargeBasis.OBSERVED.value:
        unknown = set(data) - {"label", "basis", "amount_usd", "provenance"}
        if unknown:
            raise ValueError(f"observed charge has unknown field(s): {sorted(map(str, unknown))}")
        return ObservedCharge(data.get("label"), _usd(data.get("amount_usd"), "observed charge"), data.get("provenance"))
    if basis == SpendChargeBasis.ASSUMED.value:
        unknown = set(data) - {"label", "basis", "amount_usd", "reason"}
        if unknown:
            raise ValueError(f"assumed charge has unknown field(s): {sorted(map(str, unknown))}")
        return AssumedCharge(data.get("label"), _usd(data.get("amount_usd"), "assumed charge"), data.get("reason"))
    raise ValueError(f"spend charge basis must be observed or assumed, got {basis!r}")


_LEDGER_FIELDS = frozenset({
    "schema_version", "population", "planned", "ceiling_usd", "assumed_cost_per_run_usd",
    "spent_usd", "spent_availability", "exhausted", "runs_started", "runs_skipped",
    "stop_reason", "charges", "unpriced", "skipped",
})


@dataclass(frozen=True)
class SpendLedger:
    """The closed state of one paid loop under a spend policy.

    Every fact a consumer needs — started, spent, exhausted, stopped and why —
    is derived from the records; nothing is a separately writable flag."""

    policy: SpendPolicy
    population: SpendPopulation
    planned: int
    charges: tuple[SpendCharge, ...] = ()
    unpriced: UnpricedRun | None = None
    skipped: tuple[PlannedSpendRow, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.policy, SpendPolicy):
            raise TypeError("spend ledger requires a SpendPolicy")
        try:
            object.__setattr__(self, "population", SpendPopulation(self.population))
        except ValueError as exc:
            raise ValueError(f"unknown spend population {self.population!r}") from exc
        if isinstance(self.planned, bool) or not isinstance(self.planned, int) or self.planned < 0:
            raise ValueError("planned run count must be a non-negative integer")
        if not isinstance(self.charges, tuple) or not all(
                isinstance(charge, (ObservedCharge, AssumedCharge)) for charge in self.charges):
            raise TypeError("spend charges must be a tuple of ObservedCharge/AssumedCharge")
        if self.unpriced is not None and not isinstance(self.unpriced, UnpricedRun):
            raise TypeError("unpriced run must be an UnpricedRun or None")
        if not isinstance(self.skipped, tuple) or not all(
                isinstance(row, PlannedSpendRow) for row in self.skipped):
            raise TypeError("skipped runs must be a tuple of PlannedSpendRow")
        labels = [charge.label for charge in self.charges] + [row.label for row in self.skipped]
        if self.unpriced is not None:
            labels.append(self.unpriced.label)
        if len(set(labels)) != len(labels):
            raise ValueError("spend ledger labels must be unique")
        if self.started + len(self.skipped) > self.planned:
            raise ValueError("spend ledger records more runs than were planned")
        if self.skipped and self.unpriced is None and not self.exhausted:
            raise ValueError("skipped runs require an exhausted ceiling or an unpriced run")

    @property
    def started(self) -> int:
        return len(self.charges) + (0 if self.unpriced is None else 1)

    @property
    def spent(self) -> Money:
        total = sum((charge.amount.amount for charge in self.charges), Decimal(0))
        return Money(total, SPEND_CURRENCY)

    @property
    def spent_availability(self) -> str:
        return COMPLETE if self.unpriced is None else PARTIAL

    @property
    def exhausted(self) -> bool:
        return self.spent.amount >= self.policy.ceiling.amount

    @property
    def stop_reason(self) -> SpendStopReason | None:
        if self.unpriced is not None:
            return SpendStopReason.COST_UNOBSERVABLE
        if self.skipped:
            return SpendStopReason.COST_CEILING
        return None

    @property
    def can_start(self) -> bool:
        return self.unpriced is None and not self.exhausted

    def charge(self, label: str, cost: Measurement[Money]) -> SpendLedger:
        """Record one completed run. Only a run the ledger allowed to start may
        be charged; an unpriceable cost ends the ledger rather than costing $0."""
        if not self.can_start:
            raise ValueError("cannot charge a run the ledger did not allow to start")
        if not isinstance(cost, Measurement):
            raise TypeError("spend charge requires a cost Measurement")
        label = _label(label)
        reason: str
        if cost.availability == AVAILABLE:
            money = cost.value
            if not isinstance(money, Money):
                raise TypeError("spend charge requires a Money measurement")
            if money.currency == SPEND_CURRENCY:
                return replace(self, charges=(*self.charges, ObservedCharge(label, money, str(cost.provenance))))
            reason = f"non_usd_cost:{money.currency}"
        else:
            reason = cost.reason or cost.availability
        if self.policy.assumed_cost_per_run is not None:
            return replace(self, charges=(*self.charges, AssumedCharge(label, self.policy.assumed_cost_per_run, reason)))
        return replace(self, unpriced=UnpricedRun(label, reason))

    def skip(self, row: PlannedSpendRow) -> SpendLedger:
        """Record a planned run the ledger refused to start."""
        if self.can_start:
            raise ValueError("cannot skip a run the ledger allows to start")
        if not isinstance(row, PlannedSpendRow):
            raise TypeError("skipped run must be a PlannedSpendRow")
        return replace(self, skipped=(*self.skipped, row))

    def to_dict(self) -> dict[str, Any]:
        stop = self.stop_reason
        return {
            "schema_version": SPEND_LEDGER_SCHEMA_VERSION,
            "population": self.population.value,
            "planned": self.planned,
            **self.policy.to_dict(),
            "spent_usd": _format_money(self.spent),
            "spent_availability": self.spent_availability,
            "exhausted": self.exhausted,
            "runs_started": self.started,
            "runs_skipped": len(self.skipped),
            "stop_reason": None if stop is None else stop.value,
            "charges": [charge_to_dict(charge) for charge in self.charges],
            "unpriced": None if self.unpriced is None else {"label": self.unpriced.label, "reason": self.unpriced.reason},
            "skipped": [row.to_dict() for row in self.skipped],
        }

    @classmethod
    def from_dict(cls, data: Any) -> SpendLedger:
        """Parse a persisted ledger and refuse one whose derived fields
        contradict its own records."""
        if not isinstance(data, Mapping):
            raise ValueError("spend ledger must be a mapping")
        unknown = set(data) - _LEDGER_FIELDS
        missing = _LEDGER_FIELDS - set(data)
        if unknown or missing:
            raise ValueError(f"spend ledger fields mismatch: unknown={sorted(map(str, unknown))} missing={sorted(missing)}")
        schema_version = data["schema_version"]
        if isinstance(schema_version, bool) or schema_version != SPEND_LEDGER_SCHEMA_VERSION:
            raise ValueError(f"unsupported spend ledger schema_version {schema_version!r}")
        policy = SpendPolicy.from_raw(data["ceiling_usd"], data["assumed_cost_per_run_usd"])
        charges_raw = data["charges"]
        skipped_raw = data["skipped"]
        if not isinstance(charges_raw, list) or not isinstance(skipped_raw, list):
            raise ValueError("spend ledger charges and skipped must be lists")
        unpriced_raw = data["unpriced"]
        unpriced = None
        if unpriced_raw is not None:
            if not isinstance(unpriced_raw, Mapping) or set(unpriced_raw) != {"label", "reason"}:
                raise ValueError("spend ledger unpriced run must carry exactly label and reason")
            unpriced = UnpricedRun(unpriced_raw["label"], unpriced_raw["reason"])
        ledger = cls(
            policy, data["population"], data["planned"],
            tuple(charge_from_dict(item) for item in charges_raw),
            unpriced,
            tuple(PlannedSpendRow.from_dict(item) for item in skipped_raw),
        )
        derived = ledger.to_dict()
        for key in ("spent_usd", "spent_availability", "exhausted", "runs_started", "runs_skipped", "stop_reason"):
            if derived[key] != data[key] or type(derived[key]) is not type(data[key]):
                raise ValueError(f"spend ledger {key} contradicts its records")
        return ledger
