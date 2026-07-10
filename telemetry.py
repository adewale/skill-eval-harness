"""Typed telemetry availability, aggregation, and comparison domain.

This is a leaf module: runner adapters and reports may import it, but it imports no
harness code.  It is deliberately strict at JSON boundaries so an unavailable
measurement cannot leak into arithmetic as zero.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
import math
import re
from typing import Any, Generic, Iterable, Mapping, TypeVar

T = TypeVar("T")

AVAILABLE = "available"
UNAVAILABLE = "unavailable"
NOT_APPLICABLE = "not_applicable"
COMPLETE = "complete"
PARTIAL = "partial"
COMPARABLE = "comparable"
BLOCKED = "blocked"

PROVENANCE = {
    "provider_reported",
    "trace_normalized",
    "price_table_estimated",
    "estimated",
    "legacy_unverified",
}
CURRENCY_RE = re.compile(r"^[A-Z]{3}$")


def _decimal(value: Any) -> Decimal:
    """Parse a finite non-negative money scalar without accepting bool/NaN."""
    if isinstance(value, bool) or not isinstance(value, (int, float, str, Decimal)):
        raise ValueError(f"money must be a numeric scalar, got {type(value).__name__}")
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("money must be finite")
    try:
        out = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("money must be a decimal number") from exc
    if not out.is_finite() or out < 0:
        raise ValueError("money must be finite and non-negative")
    return out


def _valid_measurement_value(value: Any) -> bool:
    if isinstance(value, Money):
        return True
    if isinstance(value, bool):
        return True
    if isinstance(value, int):
        return value >= 0
    if isinstance(value, float):
        return math.isfinite(value) and value >= 0
    if isinstance(value, Decimal):
        return value.is_finite() and value >= 0
    return False


def _valid_comparison_value(value: Any) -> bool:
    """A delta/ratio may be signed, but it can never be NaN or infinity."""
    if isinstance(value, (Money, SignedMoney)):
        return True
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return True
    if isinstance(value, float):
        return math.isfinite(value)
    if isinstance(value, Decimal):
        return value.is_finite()
    return False


def _wire_value(value: Any) -> Any:
    if isinstance(value, (Money, SignedMoney)):
        return value.to_dict()
    if isinstance(value, Decimal):
        return format(value, "f")
    return value


@dataclass(frozen=True)
class Money:
    """A non-negative exact amount in an ISO currency."""

    amount: Decimal
    currency: str

    def __post_init__(self) -> None:
        amount = _decimal(self.amount)
        currency = str(self.currency)
        if not CURRENCY_RE.fullmatch(currency):
            raise ValueError("currency must be a three-letter uppercase ISO code")
        object.__setattr__(self, "amount", amount)
        object.__setattr__(self, "currency", currency)

    @classmethod
    def from_raw(cls, amount: Any, currency: Any = "USD") -> "Money":
        return cls(_decimal(amount), str(currency))

    def to_dict(self) -> dict[str, str]:
        return {"amount": format(self.amount, "f"), "currency": self.currency}


@dataclass(frozen=True)
class SignedMoney:
    """A finite monetary difference. Measurements use ``Money``; deltas may save."""

    amount: Decimal
    currency: str

    def __post_init__(self) -> None:
        if isinstance(self.amount, bool):
            raise ValueError("money delta must be numeric")
        try:
            amount = Decimal(str(self.amount))
        except (InvalidOperation, ValueError) as exc:
            raise ValueError("money delta must be numeric") from exc
        currency = str(self.currency)
        if not amount.is_finite() or not CURRENCY_RE.fullmatch(currency):
            raise ValueError("money delta must be finite and use an ISO currency")
        object.__setattr__(self, "amount", amount)
        object.__setattr__(self, "currency", currency)

    def to_dict(self) -> dict[str, str]:
        return {"amount": format(self.amount, "f"), "currency": self.currency}


@dataclass(frozen=True)
class Measurement(Generic[T]):
    """An observed value, an explicitly unavailable value, or an N/A value."""

    availability: str
    value: T | None = None
    provenance: str | None = None
    basis: Mapping[str, Any] = field(default_factory=dict)
    reason: str | None = None

    def __post_init__(self) -> None:
        if self.availability not in {AVAILABLE, UNAVAILABLE, NOT_APPLICABLE}:
            raise ValueError(f"unknown availability {self.availability!r}")
        if self.availability == AVAILABLE:
            if self.value is None:
                raise ValueError("available measurement requires a value")
            if not _valid_measurement_value(self.value):
                raise ValueError("available measurement requires a finite non-negative value")
            if self.provenance not in PROVENANCE:
                raise ValueError(f"available measurement requires known provenance, got {self.provenance!r}")
            if self.reason is not None:
                raise ValueError("available measurement cannot carry an unavailable reason")
        else:
            if self.value is not None or self.provenance is not None:
                raise ValueError("unavailable/not-applicable measurement cannot carry value or provenance")
            if not isinstance(self.reason, str) or not self.reason:
                raise ValueError("unavailable/not-applicable measurement requires a reason")
        if not isinstance(self.basis, Mapping):
            raise ValueError("measurement basis must be a mapping")
        object.__setattr__(self, "basis", dict(self.basis))

    @classmethod
    def available(cls, value: T, *, provenance: str, basis: Mapping[str, Any] | None = None) -> "Measurement[T]":
        return cls(AVAILABLE, value=value, provenance=provenance, basis=basis or {})

    @classmethod
    def unavailable(cls, reason: str, *, basis: Mapping[str, Any] | None = None) -> "Measurement[T]":
        return cls(UNAVAILABLE, basis=basis or {}, reason=reason)

    @classmethod
    def not_applicable(cls, reason: str, *, basis: Mapping[str, Any] | None = None) -> "Measurement[T]":
        return cls(NOT_APPLICABLE, basis=basis or {}, reason=reason)

    @classmethod
    def from_dict(cls, raw: Any) -> "Measurement[Any]":
        if not isinstance(raw, Mapping):
            raise ValueError("measurement must be an object")
        availability = raw.get("availability")
        basis = raw.get("basis") if isinstance(raw.get("basis"), Mapping) else {}
        if availability == AVAILABLE:
            value = raw.get("value")
            if isinstance(value, Mapping) and set(value) >= {"amount", "currency"}:
                value = Money.from_raw(value["amount"], value["currency"])
            return cls.available(value, provenance=str(raw.get("provenance") or ""), basis=basis)
        if availability == UNAVAILABLE:
            return cls.unavailable(str(raw.get("reason") or ""), basis=basis)
        if availability == NOT_APPLICABLE:
            return cls.not_applicable(str(raw.get("reason") or ""), basis=basis)
        raise ValueError(f"unknown measurement availability {availability!r}")

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"availability": self.availability}
        if self.availability == AVAILABLE:
            out["value"] = _wire_value(self.value)
            out["provenance"] = self.provenance
        else:
            out["reason"] = self.reason
        if self.basis:
            out["basis"] = dict(self.basis)
        return out


@dataclass(frozen=True)
class Aggregate(Generic[T]):
    """A complete, partial, unavailable, or N/A aggregate without fake zeros."""

    availability: str
    value: T | None = None
    known_subtotal: T | None = None
    observed_count: int = 0
    unavailable_count: int = 0
    not_applicable_count: int = 0
    reason_counts: Mapping[str, int] = field(default_factory=dict)
    reason: str | None = None

    def __post_init__(self) -> None:
        if self.availability not in {COMPLETE, PARTIAL, UNAVAILABLE, NOT_APPLICABLE}:
            raise ValueError(f"unknown aggregate availability {self.availability!r}")
        if min(self.observed_count, self.unavailable_count, self.not_applicable_count) < 0:
            raise ValueError("aggregate counts cannot be negative")
        if self.availability == COMPLETE:
            if self.value is None or self.known_subtotal is not None:
                raise ValueError("complete aggregate requires value only")
            if not _valid_measurement_value(self.value):
                raise ValueError("complete aggregate requires a finite non-negative value")
            if self.unavailable_count or self.not_applicable_count:
                raise ValueError("complete aggregate cannot have unobserved inputs")
        elif self.availability == PARTIAL:
            if self.known_subtotal is None or self.value is not None:
                raise ValueError("partial aggregate requires known_subtotal only")
            if not _valid_measurement_value(self.known_subtotal):
                raise ValueError("partial aggregate requires a finite non-negative subtotal")
            if not (self.unavailable_count or self.not_applicable_count):
                raise ValueError("partial aggregate requires unavailable or N/A inputs")
        elif self.availability == UNAVAILABLE:
            if self.value is not None or self.known_subtotal is not None or self.observed_count:
                raise ValueError("unavailable aggregate cannot carry observed numeric data")
        else:
            if self.value is not None or self.known_subtotal is not None:
                raise ValueError("N/A aggregate cannot carry numeric data")
            if not self.reason:
                raise ValueError("N/A aggregate requires a reason")
        object.__setattr__(self, "reason_counts", dict(self.reason_counts))

    def scalar_if_complete(self) -> T | None:
        return self.value if self.availability == COMPLETE else None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "availability": self.availability,
            "observed_count": self.observed_count,
            "unavailable_count": self.unavailable_count,
            "not_applicable_count": self.not_applicable_count,
            "reason_counts": dict(self.reason_counts),
        }
        if self.availability == COMPLETE:
            out["value"] = _wire_value(self.value)
        elif self.availability == PARTIAL:
            out["known_subtotal"] = _wire_value(self.known_subtotal)
        elif self.availability == NOT_APPLICABLE:
            out["reason"] = self.reason
        return out


@dataclass(frozen=True)
class Comparison(Generic[T]):
    """A pairwise value that either has a valid basis or a blocking reason."""

    availability: str
    value: T | None = None
    basis: Mapping[str, Any] = field(default_factory=dict)
    reason: str | None = None

    def __post_init__(self) -> None:
        if self.availability not in {COMPARABLE, BLOCKED}:
            raise ValueError(f"unknown comparison availability {self.availability!r}")
        if self.availability == COMPARABLE:
            if self.value is None or self.reason is not None:
                raise ValueError("comparable result requires value and no reason")
            if not _valid_comparison_value(self.value):
                raise ValueError("comparable result requires a finite numeric or money value")
        elif self.value is not None or not self.reason:
            raise ValueError("blocked result requires reason and no value")
        object.__setattr__(self, "basis", dict(self.basis))

    @classmethod
    def comparable(cls, value: T, *, basis: Mapping[str, Any] | None = None) -> "Comparison[T]":
        return cls(COMPARABLE, value=value, basis=basis or {})

    @classmethod
    def blocked(cls, reason: str, *, basis: Mapping[str, Any] | None = None) -> "Comparison[T]":
        return cls(BLOCKED, basis=basis or {}, reason=reason)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"availability": self.availability}
        if self.availability == COMPARABLE:
            out["value"] = _wire_value(self.value)
        else:
            out["reason"] = self.reason
        if self.basis:
            out["basis"] = dict(self.basis)
        return out


def aggregate_numeric(measurements: Iterable[Measurement[Any]], *,
                      required_basis: tuple[str, ...] = ("population", "billing_scope", "provenance")) -> Aggregate[Any]:
    """Aggregate compatible numeric observations without treating absent as zero.

    Callers may intentionally vary case/repetition/model within a suite, but
    population, billing scope, and provenance form a minimum compatibility
    bucket. Mixed values produce an unavailable aggregate rather than a false
    complete total; report callers can expose their separate buckets.
    """
    materialized = list(measurements)
    signatures = {
        tuple(m.provenance if field_name == "provenance" else m.basis.get(field_name)
              for field_name in required_basis)
        for m in materialized if m.availability == AVAILABLE
    }
    if len(signatures) > 1:
        return Aggregate(UNAVAILABLE, reason_counts={"basis_mismatch": len(signatures)})
    observed: list[Any] = []
    measurements = materialized
    unavailable = 0
    not_applicable = 0
    reasons: Counter[str] = Counter()
    for measurement in measurements:
        if measurement.availability == AVAILABLE:
            if isinstance(measurement.value, (Money, bool)) or not _valid_measurement_value(measurement.value):
                raise ValueError("aggregate_numeric accepts only numeric measurements")
            observed.append(measurement.value)
        elif measurement.availability == UNAVAILABLE:
            unavailable += 1
            reasons[str(measurement.reason)] += 1
        else:
            not_applicable += 1
            reasons[f"not_applicable:{measurement.reason}"] += 1
    if observed:
        total = sum(observed)
        if unavailable or not_applicable:
            return Aggregate(PARTIAL, known_subtotal=total, observed_count=len(observed),
                             unavailable_count=unavailable, not_applicable_count=not_applicable,
                             reason_counts=reasons)
        return Aggregate(COMPLETE, value=total, observed_count=len(observed), reason_counts=reasons)
    if not_applicable and not unavailable:
        reason = next(iter(reasons), "not_applicable")
        return Aggregate(NOT_APPLICABLE, unavailable_count=0, not_applicable_count=not_applicable,
                         reason_counts=reasons, reason=reason)
    return Aggregate(UNAVAILABLE, unavailable_count=unavailable, not_applicable_count=not_applicable,
                     reason_counts=reasons)


def aggregate_money_by_currency(measurements: Iterable[Measurement[Money]], *,
                                required_basis: tuple[str, ...] = ("population", "billing_scope", "provenance")) -> dict[str, Aggregate[Decimal]]:
    """Return one availability-aware exact aggregate per observed currency."""
    materialized = list(measurements)
    currencies = sorted({m.value.currency for m in materialized if m.availability == AVAILABLE and isinstance(m.value, Money)})
    if not currencies:
        base = aggregate_numeric([])
        unavailable = sum(1 for m in materialized if m.availability == UNAVAILABLE)
        na = sum(1 for m in materialized if m.availability == NOT_APPLICABLE)
        reasons = Counter(
            str(m.reason) if m.availability == UNAVAILABLE else f"not_applicable:{m.reason}"
            for m in materialized if m.availability != AVAILABLE
        )
        if na and not unavailable:
            base = Aggregate(NOT_APPLICABLE, unavailable_count=0, not_applicable_count=na,
                             reason_counts=reasons, reason=next(iter(reasons), "not_applicable"))
        else:
            base = Aggregate(UNAVAILABLE, unavailable_count=unavailable, not_applicable_count=na,
                             reason_counts=reasons)
        return {"unknown": base}
    out: dict[str, Aggregate[Decimal]] = {}
    for currency in currencies:
        rows: list[Measurement[Any]] = []
        for measurement in materialized:
            if measurement.availability == AVAILABLE:
                assert isinstance(measurement.value, Money)
                if measurement.value.currency == currency:
                    rows.append(Measurement.available(measurement.value.amount, provenance=measurement.provenance or "", basis=measurement.basis))
                # A different observed currency belongs to its own complete
                # bucket; it is not missing USD/EUR telemetry.
                continue
            elif measurement.availability == UNAVAILABLE:
                rows.append(Measurement.unavailable(str(measurement.reason), basis=measurement.basis))
            else:
                rows.append(Measurement.not_applicable(str(measurement.reason), basis=measurement.basis))
        out[currency] = aggregate_numeric(rows, required_basis=required_basis)
    return out


def measurement_from_cost_block(block: Any, *, legacy_value: Any = None,
                                 basis: Mapping[str, Any] | None = None) -> Measurement[Money]:
    """Read normalized v2 cost or a legacy scalar into the strict domain model."""
    basis = basis or {}
    if isinstance(block, Mapping):
        source = block.get("source")
        if source == "missing":
            return Measurement.unavailable("missing", basis=basis)
        if source == "not_applicable":
            return Measurement.not_applicable("not_applicable", basis=basis)
        if source is not None:
            raw = block.get("total_cost")
            if raw is None:
                return Measurement.unavailable("invalid_cost_block", basis=basis)
            try:
                money = Money.from_raw(raw, block.get("currency", "USD"))
            except ValueError:
                return Measurement.unavailable("invalid_cost_block", basis=basis)
            provenance = str(source)
            if provenance not in PROVENANCE:
                return Measurement.unavailable("unknown_cost_provenance", basis=basis)
            return Measurement.available(money, provenance=provenance, basis=basis)
    if legacy_value is not None:
        try:
            return Measurement.available(Money.from_raw(legacy_value, "USD"), provenance="legacy_unverified", basis=basis)
        except ValueError:
            return Measurement.unavailable("invalid_legacy_cost", basis=basis)
    return Measurement.unavailable("missing", basis=basis)


def measurement_from_usage_block(block: Any, key: str, *, legacy_value: Any = None,
                                  basis: Mapping[str, Any] | None = None) -> Measurement[int]:
    """Read one normalized usage key or legacy scalar into a strict measurement."""
    basis = basis or {}
    if isinstance(block, Mapping):
        source = block.get("source")
        if source == "missing":
            return Measurement.unavailable("missing", basis=basis)
        if source == "not_applicable":
            return Measurement.not_applicable("not_applicable", basis=basis)
        if source is not None:
            raw = block.get(key)
            if (isinstance(raw, bool) or not isinstance(raw, (int, float)) or not math.isfinite(float(raw))
                    or raw < 0 or (isinstance(raw, float) and not raw.is_integer())):
                return Measurement.unavailable(f"missing_{key}", basis=basis)
            provenance = str(source)
            if provenance not in PROVENANCE:
                return Measurement.unavailable("unknown_usage_provenance", basis=basis)
            return Measurement.available(int(raw), provenance=provenance, basis=basis)
    if legacy_value is not None:
        if (isinstance(legacy_value, bool) or not isinstance(legacy_value, (int, float))
                or not math.isfinite(float(legacy_value)) or legacy_value < 0
                or (isinstance(legacy_value, float) and not legacy_value.is_integer())):
            return Measurement.unavailable(f"invalid_legacy_{key}", basis=basis)
        return Measurement.available(int(legacy_value), provenance="legacy_unverified", basis=basis)
    return Measurement.unavailable("missing", basis=basis)


def measurement_from_nonnegative(value: Any, *, provenance: str = "runner_measured",
                                  unavailable_reason: str = "missing", basis: Mapping[str, Any] | None = None) -> Measurement[int]:
    """Build a local counter/duration measurement with no truthiness fallback."""
    # Harness-measured values have their own explicit provenance on the v3 wire;
    # map them to trace_normalized internally until all callers expose a richer enum.
    normalized_provenance = "trace_normalized" if provenance == "runner_measured" else provenance
    if (isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value))
            or value < 0 or (isinstance(value, float) and not value.is_integer())):
        return Measurement.unavailable(unavailable_reason, basis=basis)
    return Measurement.available(int(value), provenance=normalized_provenance, basis=basis)


def basis_from_run(raw: Mapping[str, Any] | None, *, source: str | None = None,
                   population: str = "answer") -> dict[str, Any]:
    raw = raw or {}
    return {
        "population": raw.get("population", population),
        "provider": raw.get("provider") or raw.get("runner") or source,
        "runner": raw.get("runner") or source,
        "model": raw.get("model"),
        "billing_scope": raw.get("billing_scope", "run"),
        "pricing_model": raw.get("pricing_model"),
        "pricing_table_version": raw.get("pricing_table_version"),
        "case_id": raw.get("case_id"),
        "run_number": raw.get("run_number"),
        "manifest_revision": raw.get("manifest_revision"),
        "skill_tree_hash": raw.get("skill_tree_hash"),
    }


def telemetry_envelope(raw: Mapping[str, Any] | None, *, source: str | None = None,
                       population: str = "answer", legacy_unverified: bool = False) -> dict[str, Any]:
    """Build the additive v3 envelope while preserving legacy blocks beside it."""
    raw = raw or {}
    existing = raw.get("telemetry")
    if isinstance(existing, Mapping) and existing.get("schema_version") == 3:
        return dict(existing)
    basis = basis_from_run(raw, source=source, population=population)
    usage = raw.get("usage_normalized")
    cost = raw.get("cost_normalized")
    measurements: dict[str, Any] = {
        "input_tokens": measurement_from_usage_block(usage, "input_tokens", legacy_value=raw.get("input_tokens"), basis=basis).to_dict(),
        "output_tokens": measurement_from_usage_block(usage, "output_tokens", legacy_value=raw.get("output_tokens"), basis=basis).to_dict(),
        "total_tokens": measurement_from_usage_block(usage, "total_tokens", legacy_value=raw.get("total_tokens"), basis=basis).to_dict(),
        "cost": measurement_from_cost_block(cost, legacy_value=raw.get("cost_usd", raw.get("cost")), basis=basis).to_dict(),
        "elapsed_ms": measurement_from_nonnegative(raw.get("elapsed_ms", raw.get("duration_ms")),
                                                     unavailable_reason="missing_elapsed_ms", basis=basis).to_dict(),
    }
    # Trace-derived counts/bools are trustworthy only when a complete trace was
    # observed. Their old flat fields remain for assertion compatibility, while
    # v3 prevents an empty/malformed trace from looking like zero activity.
    trace_complete = raw.get("observation_complete", raw.get("trace_observation_complete"))
    for key in ("tool_calls", "commands", "file_reads", "file_writes", "errors", "retries", "repeated_command_max"):
        if trace_complete is True:
            measurements[key] = measurement_from_nonnegative(raw.get(key), unavailable_reason=f"missing_{key}", basis=basis).to_dict()
        else:
            measurements[key] = Measurement.unavailable("trace_observation_incomplete", basis=basis).to_dict()
    if trace_complete is True and isinstance(raw.get("skill_invoked"), bool):
        measurements["skill_invoked"] = Measurement.available(raw["skill_invoked"], provenance="trace_normalized", basis=basis).to_dict()
    else:
        measurements["skill_invoked"] = Measurement.unavailable("trace_observation_incomplete", basis=basis).to_dict()
    if legacy_unverified:
        # Normalized v1/v2 fields can be useful historical observations but do
        # not establish the provider/billing basis needed for causal ratios.
        for key, wire in list(measurements.items()):
            try:
                measurement = Measurement.from_dict(wire)
            except ValueError:
                continue
            if measurement.availability == AVAILABLE:
                measurements[key] = Measurement.available(
                    measurement.value, provenance="legacy_unverified", basis=measurement.basis).to_dict()
    return {
        "schema_version": 3,
        "population": basis["population"],
        "basis": {key: value for key, value in basis.items() if value is not None},
        "measurements": measurements,
    }


def measurement_from_envelope_or_cost(raw: Mapping[str, Any] | None, *, source: str | None = None,
                                      population: str = "answer") -> Measurement[Money]:
    raw = raw or {}
    envelope = raw.get("telemetry")
    if isinstance(envelope, Mapping) and envelope.get("schema_version") == 3:
        measurements = envelope.get("measurements")
        if isinstance(measurements, Mapping) and isinstance(measurements.get("cost"), Mapping):
            try:
                return Measurement.from_dict(measurements["cost"])
            except ValueError:
                return Measurement.unavailable("invalid_v3_cost")
    basis = basis_from_run(raw, source=source, population=population)
    return measurement_from_cost_block(raw.get("cost_normalized"), legacy_value=raw.get("cost_usd", raw.get("cost")), basis=basis)


def measurement_from_envelope_or_nonnegative(raw: Mapping[str, Any], key: str, *, source: str | None = None,
                                             population: str = "answer") -> Measurement[int]:
    """Read a v3 local metric (duration/count) or adapt a legacy scalar."""
    envelope = raw.get("telemetry")
    if isinstance(envelope, Mapping) and envelope.get("schema_version") == 3:
        measurements = envelope.get("measurements")
        if isinstance(measurements, Mapping) and isinstance(measurements.get(key), Mapping):
            try:
                return Measurement.from_dict(measurements[key])
            except ValueError:
                return Measurement.unavailable(f"invalid_v3_{key}")
    legacy_value = raw.get(key)
    if legacy_value is None and key == "elapsed_ms":
        legacy_value = raw.get("duration_ms")
    return measurement_from_nonnegative(legacy_value, unavailable_reason=f"missing_{key}",
                                        basis=basis_from_run(raw, source=source, population=population))


def measurement_from_envelope_or_usage(raw: Mapping[str, Any], key: str, *, source: str | None = None,
                                       population: str = "answer") -> Measurement[int]:
    envelope = raw.get("telemetry")
    if isinstance(envelope, Mapping) and envelope.get("schema_version") == 3:
        measurements = envelope.get("measurements")
        if isinstance(measurements, Mapping) and isinstance(measurements.get(key), Mapping):
            try:
                return Measurement.from_dict(measurements[key])
            except ValueError:
                return Measurement.unavailable(f"invalid_v3_{key}")
    basis = basis_from_run(raw, source=source, population=population)
    return measurement_from_usage_block(raw.get("usage_normalized"), key, legacy_value=raw.get(key), basis=basis)


def with_basis(measurement: Measurement[T], **updates: Any) -> Measurement[T]:
    """Attach run/pair identity at the harness boundary without loosening state."""
    basis = {**measurement.basis, **{key: value for key, value in updates.items() if value is not None}}
    if measurement.availability == AVAILABLE:
        return Measurement.available(measurement.value, provenance=str(measurement.provenance), basis=basis)
    if measurement.availability == UNAVAILABLE:
        return Measurement.unavailable(str(measurement.reason), basis=basis)
    return Measurement.not_applicable(str(measurement.reason), basis=basis)


def _blocked_for_measurement(measurement: Measurement[Any], side: str) -> Comparison[Any] | None:
    if measurement.availability == UNAVAILABLE:
        return Comparison.blocked(f"missing_{side}")
    if measurement.availability == NOT_APPLICABLE:
        return Comparison.blocked("not_applicable")
    return None


def _basis_compatibility(left: Measurement[Any], right: Measurement[Any]) -> str | None:
    if left.provenance == "legacy_unverified" or right.provenance == "legacy_unverified":
        return "legacy_unverified"
    if left.provenance != right.provenance:
        return "provenance_mismatch"
    required = ["population", "provider", "model", "billing_scope", "case_id", "run_number"]
    if left.provenance == "price_table_estimated":
        required.extend(["pricing_model", "pricing_table_version"])
    for field_name in required:
        left_value = left.basis.get(field_name)
        right_value = right.basis.get(field_name)
        if left_value is None or right_value is None:
            return "basis_missing"
        if left_value != right_value:
            return "population_mismatch" if field_name == "population" else "pair_key_mismatch" if field_name in {"case_id", "run_number"} else "basis_mismatch"
    # Revision identifiers are optional during migration, but if either arm
    # supplies one it must agree; a stale tree/report cannot claim paired lift.
    for field_name in ("manifest_revision", "skill_tree_hash"):
        left_value = left.basis.get(field_name)
        right_value = right.basis.get(field_name)
        if left_value is not None or right_value is not None:
            if left_value is None or right_value is None:
                return "basis_missing"
            if left_value != right_value:
                return "basis_mismatch"
    return None


def compare_cost_pair(left: Measurement[Money], right: Measurement[Money], *,
                      left_scorable: bool = True, right_scorable: bool = True) -> Comparison[SignedMoney]:
    """Compute with-minus-without only when both cost observations are comparable."""
    if not left_scorable or not right_scorable:
        return Comparison.blocked("unscorable_arm")
    if blocked := _blocked_for_measurement(left, "left"):
        return blocked
    if blocked := _blocked_for_measurement(right, "right"):
        return blocked
    assert isinstance(left.value, Money) and isinstance(right.value, Money)
    if left.value.currency != right.value.currency:
        return Comparison.blocked("currency_mismatch")
    if reason := _basis_compatibility(left, right):
        return Comparison.blocked(reason)
    return Comparison.comparable(
        SignedMoney(left.value.amount - right.value.amount, left.value.currency),
        basis={"currency": left.value.currency, "population": left.basis.get("population"),
               "provider": left.basis.get("provider"), "model": left.basis.get("model"),
               "billing_scope": left.basis.get("billing_scope")},
    )


def compare_numeric_pair(left: Measurement[int], right: Measurement[int], *,
                         left_scorable: bool = True, right_scorable: bool = True) -> Comparison[int]:
    if not left_scorable or not right_scorable:
        return Comparison.blocked("unscorable_arm")
    if blocked := _blocked_for_measurement(left, "left"):
        return blocked
    if blocked := _blocked_for_measurement(right, "right"):
        return blocked
    if reason := _basis_compatibility(left, right):
        return Comparison.blocked(reason)
    assert isinstance(left.value, int) and isinstance(right.value, int)
    return Comparison.comparable(left.value - right.value, basis=dict(left.basis))


def compare_objective_rates(with_rate: Any, without_rate: Any, *,
                            left_scorable: bool, right_scorable: bool) -> Comparison[float]:
    """The typed numerator for efficiency claims; unscorable arms cannot leak in."""
    if not left_scorable or not right_scorable:
        return Comparison.blocked("unscorable_arm")
    for value in (with_rate, without_rate):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)) or not 0 <= float(value) <= 1:
            return Comparison.blocked("missing_objective_lift")
    return Comparison.comparable(float(with_rate) - float(without_rate))


def lift_per_dollar(objective_delta: Comparison[float], cost_delta: Comparison[SignedMoney]) -> Comparison[float]:
    """Construct the efficiency ratio only from typed comparable evidence."""
    if objective_delta.availability == BLOCKED:
        return Comparison.blocked(str(objective_delta.reason))
    if cost_delta.availability == BLOCKED:
        return Comparison.blocked(str(cost_delta.reason))
    assert isinstance(objective_delta.value, float)
    assert isinstance(cost_delta.value, SignedMoney)
    if cost_delta.value.amount <= 0:
        return Comparison.blocked("non_positive_denominator", basis=cost_delta.basis)
    return Comparison.comparable(float(Decimal(str(objective_delta.value)) / cost_delta.value.amount), basis=cost_delta.basis)


def lift_per_1k_tokens(objective_delta: Comparison[float], token_delta: Comparison[int]) -> Comparison[float]:
    if objective_delta.availability == BLOCKED:
        return Comparison.blocked(str(objective_delta.reason))
    if token_delta.availability == BLOCKED:
        return Comparison.blocked(str(token_delta.reason))
    assert isinstance(objective_delta.value, float)
    assert isinstance(token_delta.value, int)
    if token_delta.value <= 0:
        return Comparison.blocked("non_positive_denominator", basis=token_delta.basis)
    return Comparison.comparable(objective_delta.value / (token_delta.value / 1000), basis=token_delta.basis)


def display_aggregate(aggregate: Mapping[str, Any] | Aggregate[Any], *, prefix: str = "") -> str:
    """One human renderer for complete, partial, unavailable, and N/A quantities."""
    data = aggregate.to_dict() if isinstance(aggregate, Aggregate) else dict(aggregate)
    availability = data.get("availability")
    if availability == COMPLETE:
        return f"{prefix}{data.get('value')}"
    if availability == PARTIAL:
        return f"partial: {prefix}{data.get('known_subtotal')} known subtotal ({data.get('unavailable_count', 0)} unavailable)"
    if availability == NOT_APPLICABLE:
        return "N/A"
    reasons = data.get("reason_counts") or {}
    reason = next(iter(reasons), "unavailable")
    return f"— unavailable ({reason})"
