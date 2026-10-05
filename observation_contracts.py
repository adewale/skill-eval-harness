"""How the harness says whether it observed something.

"We don't know this value" was spelled several ways: ``unknown`` in trace
evidence, ``unobserved`` in completion evidence, ``missing`` as a telemetry
source, ``unavailable`` in reports, and ``incomplete`` beside ``partial``.
Each spelling was right in its own module, and together they made the same
state look like five states.

``Availability`` is the canonical vocabulary. Values already persisted in run
artifacts keep their bytes (a schema change would be needed to rename them),
so ``Availability.parse`` reads every legacy spelling and code compares
canonical members instead of re-spelling strings. New fields write the
canonical values.

``TelemetrySource`` is the one list of where a usage or cost number came from.
It replaces four hand-maintained sets that had drifted apart (the trigger path
accepted a cost source the answer path rejected).
"""
from __future__ import annotations

from enum import Enum


class Availability(str, Enum):
    COMPLETE = "complete"
    PARTIAL = "partial"
    UNAVAILABLE = "unavailable"
    NOT_APPLICABLE = "not_applicable"

    @classmethod
    def parse(cls, value: object) -> Availability:
        """Read a canonical value or any legacy spelling of one."""
        if isinstance(value, cls):
            return value
        if not isinstance(value, str):
            raise ValueError(f"availability must be a string, got {type(value).__name__}")
        try:
            return cls(value)
        except ValueError:
            pass
        try:
            return LEGACY_AVAILABILITY[value]
        except KeyError as exc:
            raise ValueError(f"unknown availability {value!r}") from exc

    @property
    def observed(self) -> bool:
        """True when the value was fully observed and can be used as evidence."""
        return self is Availability.COMPLETE


# Spellings persisted by earlier modules, read as the canonical state.
LEGACY_AVAILABILITY: dict[str, Availability] = {
    "incomplete": Availability.PARTIAL,
    "unknown": Availability.UNAVAILABLE,
    "unobserved": Availability.UNAVAILABLE,
    "missing": Availability.UNAVAILABLE,
    "not-applicable": Availability.NOT_APPLICABLE,
}


class TelemetrySource(str, Enum):
    """Where a usage or cost number came from, or why there is none."""

    PROVIDER_REPORTED = "provider_reported"
    TRACE_NORMALIZED = "trace_normalized"
    PROCESS_MEASURED = "process_measured"
    PRICE_TABLE_ESTIMATED = "price_table_estimated"
    ESTIMATED = "estimated"
    LEGACY_UNVERIFIED = "legacy_unverified"
    MISSING = "missing"
    NOT_APPLICABLE = "not_applicable"


# Provenance a measured number may carry in the v3 telemetry block.
MEASUREMENT_PROVENANCE = frozenset(source.value for source in (
    TelemetrySource.PROVIDER_REPORTED,
    TelemetrySource.TRACE_NORMALIZED,
    TelemetrySource.PROCESS_MEASURED,
    TelemetrySource.PRICE_TABLE_ESTIMATED,
    TelemetrySource.ESTIMATED,
    TelemetrySource.LEGACY_UNVERIFIED,
))
# Sources a per-run usage block may declare. Token counts are never priced, so
# a price-table estimate is not a usage source.
USAGE_SOURCES = frozenset(source.value for source in (
    TelemetrySource.PROVIDER_REPORTED,
    TelemetrySource.TRACE_NORMALIZED,
    TelemetrySource.ESTIMATED,
    TelemetrySource.MISSING,
    TelemetrySource.NOT_APPLICABLE,
))
# Sources a per-run cost block may declare. A cost estimate is always a
# price-table estimate, which names its table; a bare "estimated" cost is not
# accepted anywhere.
COST_SOURCES = frozenset(source.value for source in (
    TelemetrySource.PROVIDER_REPORTED,
    TelemetrySource.TRACE_NORMALIZED,
    TelemetrySource.PRICE_TABLE_ESTIMATED,
    TelemetrySource.MISSING,
    TelemetrySource.NOT_APPLICABLE,
))
# Sources that carry no number.
ABSENT_SOURCES = frozenset({TelemetrySource.MISSING.value, TelemetrySource.NOT_APPLICABLE.value})
