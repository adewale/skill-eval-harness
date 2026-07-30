"""Lossless, immutable JSON values for persisted evidence contracts."""
from __future__ import annotations

import json
import math
from collections.abc import Mapping
from typing import Any, NoReturn


class FrozenJSONDict(dict[str, Any]):
    """A JSON-serializable mapping that cannot be mutated after validation."""

    def _immutable(self, *args: Any, **kwargs: Any) -> NoReturn:
        raise TypeError("frozen JSON mapping cannot be mutated")

    __setitem__ = __delitem__ = __ior__ = clear = pop = popitem = setdefault = update = _immutable


def freeze_json_value(value: Any, label: str) -> Any:
    """Validate and recursively freeze one strict-JSON value.

    Object keys are never coerced: coercion can collapse distinct evidence such
    as ``1`` and ``"1"``. Non-finite floats are rejected at construction so a
    value accepted by the typed domain is guaranteed to survive strict JSON
    serialization later.
    """
    if isinstance(value, Mapping):
        frozen: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError(f"{label} object keys must be strings")
            frozen[key] = freeze_json_value(item, f"{label}.{key}")
        return FrozenJSONDict(frozen)
    if isinstance(value, (list, tuple)):
        return tuple(
            freeze_json_value(item, f"{label}[{index}]")
            for index, item in enumerate(value)
        )
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError(f"{label} must not contain non-finite numbers")
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise TypeError(
        f"{label} contains non-JSON value {type(value).__name__}")


def freeze_json_mapping(
    value: Mapping[str, Any] | None,
    label: str,
) -> Mapping[str, Any]:
    if value is None:
        return FrozenJSONDict()
    if not isinstance(value, Mapping):
        raise TypeError(f"{label} must be a mapping")
    frozen = freeze_json_value(value, label)
    if not isinstance(frozen, Mapping):  # pragma: no cover - established above
        raise TypeError(f"{label} must be a mapping")
    return frozen


def strict_json_equal(left: Any, right: Any) -> bool:
    """Compare JSON values without Python's bool/int or int/float coercions."""
    left_frozen = freeze_json_value(left, "left JSON value")
    right_frozen = freeze_json_value(right, "right JSON value")
    left_wire = json.dumps(
        left_frozen, sort_keys=True, separators=(",", ":"), allow_nan=False)
    right_wire = json.dumps(
        right_frozen, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return left_wire == right_wire
