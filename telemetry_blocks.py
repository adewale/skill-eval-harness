"""Normalized usage and cost blocks built from provider-reported fields.
"""
from __future__ import annotations

import math
import re
from typing import Any

import telemetry as telemetry_domain


def metric_number(metrics: dict[str, Any], *keys: str) -> int | float | None:
    def validated(value: Any, key: str) -> int | float | None:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        if isinstance(value, int):
            return value if value >= 0 else None
        number = value
        if not math.isfinite(number) or number < 0:
            return None
        integer_metric = (key.endswith("tokens") or key in {
            "commands", "command_count", "tool_calls", "file_reads",
            "file_writes", "errors", "retries", "repeated_command_max",
        })
        if integer_metric and not number.is_integer():
            return None
        return number

    for key in keys:
        value = validated(metrics.get(key), key)
        if value is not None:
            return value
    usage = metrics.get("usage")
    if isinstance(usage, dict):
        for key in keys:
            value = validated(usage.get(key), key)
            if value is not None:
                return value
    return None


USAGE_SOURCES = {"provider_reported", "trace_normalized", "estimated", "missing", "not_applicable"}
COST_SOURCES = {"provider_reported", "trace_normalized", "price_table_estimated", "missing", "not_applicable"}
# Every normalizer reads the leaf telemetry domain's token-usage alias table, so
# provider payloads cannot be classified differently by two paths.
USAGE_ALIASES = telemetry_domain.USAGE_ALIASES
COST_PART_ALIASES: dict[str, tuple[str, ...]] = {
    "input_cost": ("input_cost", "prompt_cost"),
    "output_cost": ("output_cost", "completion_cost"),
    "cache_read_cost": ("cache_read_cost",),
    "cache_write_cost": ("cache_write_cost", "cache_creation_cost"),
    "reasoning_cost": ("reasoning_cost",),
}


def _num(value: Any) -> float | None:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (OverflowError, ValueError):
        return None
    return number if math.isfinite(number) else None


def normalize_usage(raw: Any, *, source: str = "provider_reported") -> dict[str, Any]:
    """The per-run usage_normalized block (issue #21): alias-normalized token
    counts with explicit provenance. No usable numbers means {"source":
    "missing"} — missing telemetry is never silently zero."""
    if source not in USAGE_SOURCES:
        raise ValueError(f"unknown usage source {source!r}; expected one of {sorted(USAGE_SOURCES)}")
    out: dict[str, Any] = telemetry_domain.canonical_usage_counts(raw)
    if source == "not_applicable":
        return {"source": "not_applicable"}
    if ("total_tokens" not in out and "input_tokens" in out
            and "output_tokens" in out):
        out["total_tokens"] = out["input_tokens"] + out["output_tokens"]
    if not out:
        return {"source": "missing"}
    out["source"] = source
    return out


def normalize_cost(raw: Any, *, source: str = "provider_reported", currency: str = "USD",
                   pricing_model: str | None = None, pricing_table_version: str | None = None,
                   pricing_notes: list[str] | None = None) -> dict[str, Any]:
    """The per-run cost_normalized block (issue #21): a provider-reported number
    or cost object, or a price-table estimate, with currency and provenance.
    Missing cost is marked missing, never zero."""
    if source not in COST_SOURCES:
        raise ValueError(f"unknown cost source {source!r}; expected one of {sorted(COST_SOURCES)}")
    if source == "not_applicable":
        return {"source": "not_applicable"}
    if isinstance(raw, (int, float)) and (isinstance(raw, bool) or _num(raw) is None):
        raise ValueError("cost measurement must be a finite number")
    total = _num(raw)
    parts: dict[str, float] = {}
    resolved_currency = currency
    if isinstance(raw, dict):
        if raw.get("currency") is not None:
            raw_currency = raw.get("currency")
            if not isinstance(raw_currency, str) or not re.fullmatch(r"[A-Z]{3}", raw_currency):
                raise ValueError("cost currency must be a three-letter uppercase code")
            resolved_currency = raw_currency
        total_observations: list[tuple[str, float]] = []
        for key in ("total_cost", "total_cost_usd", "cost_usd", "total", "cost", "amount"):
            if key not in raw or raw[key] is None:
                continue
            value = _num(raw[key])
            if value is None:
                raise ValueError(f"cost.{key} must be a finite number")
            if key.endswith("_usd") and resolved_currency != "USD":
                raise ValueError(f"cost.{key} cannot be labelled {resolved_currency}")
            total_observations.append((key, value))
        if total_observations:
            values = {value for _, value in total_observations}
            if len(values) != 1:
                raise ValueError(
                    f"conflicting total cost aliases: {dict(total_observations)}")
            total = total_observations[0][1]
        for norm_key, aliases in COST_PART_ALIASES.items():
            observations: list[tuple[str, float]] = []
            for alias in aliases:
                if alias not in raw or raw[alias] is None:
                    continue
                value = _num(raw[alias])
                if value is None:
                    raise ValueError(f"cost.{alias} must be a finite number")
                observations.append((alias, value))
            if observations:
                values = {value for _, value in observations}
                if len(values) != 1:
                    raise ValueError(
                        f"conflicting aliases for {norm_key}: {dict(observations)}")
                parts[norm_key] = observations[0][1]
        if "components_complete" in raw and not isinstance(raw["components_complete"], bool):
            raise ValueError("cost.components_complete must be boolean")
        if total is None and parts and raw.get("components_complete") is True:
            total = sum(parts.values())
        elif total is not None and parts and raw.get("components_complete") is True:
            component_total = sum(parts.values())
            if not math.isclose(total, component_total, rel_tol=1e-12, abs_tol=1e-12):
                raise ValueError(
                    "complete cost components do not sum to the reported total")
    if total is None:
        return ({"source": "missing", "currency": resolved_currency,
                 "observed_parts": parts, "reason": "partial_cost_components"}
                if parts else {"source": "missing"})
    if total < 0 or any(value < 0 for value in parts.values()):
        raise ValueError("cost measurements must be nonnegative")
    out: dict[str, Any] = {"currency": resolved_currency, **parts,
                           "total_cost": total, "source": source}
    if pricing_model:
        out["pricing_model"] = pricing_model
    if pricing_table_version:
        out["pricing_table_version"] = pricing_table_version
    if pricing_notes:
        out["pricing_notes"] = list(pricing_notes)
    return out


def run_cost_facts(merged: dict[str, Any]) -> dict[str, Any]:
    """ONE reader for a run's usage/cost facts.

    New v3 artifacts are parsed through :mod:`telemetry`; old normalized/flat
    fields are adapted at this boundary and labelled ``legacy_unverified``. The
    scalar compatibility fields are populated only for available measurements,
    so callers cannot confuse unavailable telemetry with zero.
    """
    basis = telemetry_domain.basis_from_run(merged, source=str(merged.get("provider") or merged.get("runner") or ""))
    envelope = merged.get("telemetry")

    def v3_or_usage(key: str):
        if isinstance(envelope, dict) and envelope.get("schema_version") == 3:
            measurements = envelope.get("measurements")
            if isinstance(measurements, dict) and isinstance(measurements.get(key), dict):
                try:
                    return telemetry_domain.Measurement.from_dict(measurements[key])
                except ValueError:
                    return telemetry_domain.Measurement.unavailable(f"invalid_v3_{key}", basis=basis)
        return telemetry_domain.measurement_from_usage_block(
            merged.get("usage_normalized"), key,
            legacy_value=metric_number(merged, key), basis=basis,
        )

    input_measurement = v3_or_usage("input_tokens")
    output_measurement = v3_or_usage("output_tokens")
    total_measurement = v3_or_usage("total_tokens")
    if isinstance(envelope, dict) and envelope.get("schema_version") == 3:
        measurements = envelope.get("measurements")
        if isinstance(measurements, dict) and isinstance(measurements.get("cost"), dict):
            try:
                cost_measurement = telemetry_domain.Measurement.from_dict(measurements["cost"])
            except ValueError:
                cost_measurement = telemetry_domain.Measurement.unavailable("invalid_v3_cost", basis=basis)
        else:
            cost_measurement = telemetry_domain.measurement_from_cost_block(
                merged.get("cost_normalized"), legacy_value=metric_number(merged, "cost_usd", "cost"), basis=basis)
    else:
        cost_measurement = telemetry_domain.measurement_from_cost_block(
            merged.get("cost_normalized"), legacy_value=metric_number(merged, "cost_usd", "cost"), basis=basis)

    def value_of(measurement):
        return measurement.value if measurement.availability == telemetry_domain.AVAILABLE else None

    def source_of(measurement) -> str:
        if measurement.availability == telemetry_domain.AVAILABLE:
            # Keep the historical scalar reader label stable; the typed
            # measurement still records the stricter legacy_unverified fact.
            return "legacy_fields" if measurement.provenance == "legacy_unverified" else str(measurement.provenance)
        return "not_applicable" if measurement.availability == telemetry_domain.NOT_APPLICABLE else "missing"

    cost = value_of(cost_measurement)
    cost_usd = float(cost.amount) if isinstance(cost, telemetry_domain.Money) and cost.currency == "USD" else None
    return {
        "input_tokens": value_of(input_measurement),
        "output_tokens": value_of(output_measurement),
        "total_tokens": value_of(total_measurement),
        "cost_usd": cost_usd,
        "cost_currency": cost.currency if isinstance(cost, telemetry_domain.Money) else None,
        "usage_source": source_of(total_measurement),
        "cost_source": source_of(cost_measurement),
        "input_tokens_measurement": input_measurement,
        "output_tokens_measurement": output_measurement,
        "total_tokens_measurement": total_measurement,
        "cost_measurement": cost_measurement,
    }
