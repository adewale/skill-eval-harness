"""The closed JSON Schema subset behind `structured_output` assertions and judge
verdict schemas; a manifest schema outside the subset is rejected, not partially
enforced.
"""
from __future__ import annotations

from typing import Any

SUPPORTED_JSON_SCHEMA_TYPES = {
    "object", "array", "string", "integer", "number", "boolean", "null",
}
SUPPORTED_JSON_SCHEMA_KEYS = {
    "type", "properties", "required", "additionalProperties", "items",
    "enum", "const", "minItems", "maxItems",
}


def json_values_equal(left: Any, right: Any) -> bool:
    """JSON-Schema equality without Python's bool/int aliasing.

    JSON numbers compare by mathematical value (so 1 equals 1.0), while JSON
    booleans are a distinct instance type (so true does not equal 1).
    """
    if isinstance(left, bool) or isinstance(right, bool):
        return isinstance(left, bool) and isinstance(right, bool) and left == right
    if (isinstance(left, (int, float)) and not isinstance(left, bool)
            and isinstance(right, (int, float)) and not isinstance(right, bool)):
        return left == right
    if left is None or right is None:
        return left is None and right is None
    if isinstance(left, str) or isinstance(right, str):
        return isinstance(left, str) and isinstance(right, str) and left == right
    if isinstance(left, list) or isinstance(right, list):
        return (isinstance(left, list) and isinstance(right, list)
                and len(left) == len(right)
                and all(json_values_equal(a, b) for a, b in zip(left, right)))
    if isinstance(left, dict) or isinstance(right, dict):
        return (isinstance(left, dict) and isinstance(right, dict)
                and set(left) == set(right)
                and all(json_values_equal(left[key], right[key]) for key in left))
    return False


def supported_json_schema_errors(schema: Any, path: str = "$") -> list[str]:
    """Validate the closed JSON-Schema subset implemented by json_schema_errors."""
    if not isinstance(schema, dict) or not schema:
        return [f"{path}: schema must be a non-empty object"]
    if not all(isinstance(key, str) for key in schema):
        return [f"{path}: schema object keys must be strings"]
    schema = {
        key: value for key, value in schema.items() if isinstance(key, str)
    }
    errors: list[str] = []
    unknown = sorted(set(schema) - SUPPORTED_JSON_SCHEMA_KEYS)
    if unknown:
        errors.append(f"{path}: unsupported keyword(s): {', '.join(unknown)}")
    raw_type = schema.get("type")
    allowed_types: list[str] = []
    if raw_type is not None:
        if isinstance(raw_type, str):
            allowed_types = [raw_type]
        elif (isinstance(raw_type, list) and raw_type
              and all(isinstance(item, str) for item in raw_type)
              and len(raw_type) == len(set(raw_type))):
            allowed_types = raw_type
        else:
            errors.append(f"{path}.type: must be a supported type or unique non-empty type list")
        bad_types = sorted(set(allowed_types) - SUPPORTED_JSON_SCHEMA_TYPES)
        if bad_types:
            errors.append(f"{path}.type: unsupported type(s): {', '.join(bad_types)}")

    object_keywords = {"properties", "required", "additionalProperties"} & set(schema)
    if object_keywords and "object" not in allowed_types:
        errors.append(f"{path}: {', '.join(sorted(object_keywords))} require type object")
    array_keywords = {"items", "minItems", "maxItems"} & set(schema)
    if array_keywords and "array" not in allowed_types:
        errors.append(f"{path}: {', '.join(sorted(array_keywords))} require type array")

    properties = schema.get("properties")
    if properties is not None:
        if (not isinstance(properties, dict)
                or not all(isinstance(key, str) and key for key in properties)):
            errors.append(f"{path}.properties: must map non-empty string names to schemas")
        else:
            for key, child in properties.items():
                errors.extend(supported_json_schema_errors(child, f"{path}.properties.{key}"))
    required = schema.get("required")
    if required is not None:
        if (not isinstance(required, list)
                or not all(isinstance(key, str) and key for key in required)
                or len(required) != len(set(required))):
            errors.append(f"{path}.required: must be a unique list of non-empty strings")
        elif isinstance(properties, dict):
            required_names = [key for key in required if isinstance(key, str)]
            property_names = [key for key in properties if isinstance(key, str)]
            missing = sorted(set(required_names) - set(property_names))
            if missing:
                errors.append(f"{path}.required: keys absent from properties: {', '.join(missing)}")
        else:
            errors.append(f"{path}.required: requires a properties object")
    if ("additionalProperties" in schema
            and not isinstance(schema["additionalProperties"], bool)):
        errors.append(f"{path}.additionalProperties: must be boolean")

    if "items" in schema:
        errors.extend(supported_json_schema_errors(schema["items"], f"{path}.items"))
    minimum, maximum = schema.get("minItems"), schema.get("maxItems")
    for key, value in (("minItems", minimum), ("maxItems", maximum)):
        if key in schema and (isinstance(value, bool)
                              or not isinstance(value, int) or value < 0):
            errors.append(f"{path}.{key}: must be a nonnegative integer")
    if (isinstance(minimum, int) and not isinstance(minimum, bool)
            and isinstance(maximum, int) and not isinstance(maximum, bool)
            and minimum > maximum):
        errors.append(f"{path}: minItems must be <= maxItems")

    if "enum" in schema:
        enum = schema["enum"]
        if not isinstance(enum, list) or not enum:
            errors.append(f"{path}.enum: must be a non-empty list")
        elif any(json_values_equal(value, prior) for index, value in enumerate(enum)
                 for prior in enum[:index]):
            errors.append(f"{path}.enum: values must be unique")
    return errors


def json_schema_errors(instance: Any, schema: dict[str, Any], path: str = "$") -> list[str]:
    """Deterministic subset of JSON-Schema for structured_output (roadmap 1.1):
    type, properties, required, additionalProperties:false, items, enum, const,
    minItems/maxItems. Enough to pin a tool-output contract without a new
    dependency."""
    errors: list[str] = []
    if "const" in schema and not json_values_equal(instance, schema["const"]):
        errors.append(f"{path}: expected const {schema['const']!r}, got {instance!r}")
    if ("enum" in schema
            and not any(json_values_equal(instance, candidate)
                        for candidate in schema["enum"])):
        errors.append(f"{path}: {instance!r} not in enum {schema['enum']!r}")
    expected_type = schema.get("type")
    if expected_type:
        checks = {
            "object": lambda v: isinstance(v, dict),
            "array": lambda v: isinstance(v, list),
            "string": lambda v: isinstance(v, str),
            "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
            "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
            "boolean": lambda v: isinstance(v, bool),
            "null": lambda v: v is None,
        }
        allowed = expected_type if isinstance(expected_type, list) else [expected_type]
        if not any(checks.get(t, lambda v: False)(instance) for t in allowed):
            errors.append(f"{path}: expected type {expected_type}, got {type(instance).__name__}")
            return errors   # type mismatch makes deeper checks noise
    if isinstance(instance, dict):
        props = schema.get("properties") or {}
        for key in schema.get("required", []):
            if key not in instance:
                errors.append(f"{path}: missing required key {key!r}")
        if schema.get("additionalProperties") is False:
            extras = sorted(str(k) for k in instance if k not in props)
            for key in extras:
                errors.append(f"{path}: unexpected key {key!r}")
        for key, sub in props.items():
            if key in instance and isinstance(sub, dict):
                errors.extend(json_schema_errors(instance[key], sub, f"{path}.{key}"))
    if isinstance(instance, list):
        min_items = schema.get("minItems")
        if isinstance(min_items, int) and len(instance) < min_items:
            errors.append(f"{path}: {len(instance)} items < minItems {min_items}")
        max_items = schema.get("maxItems")
        if isinstance(max_items, int) and len(instance) > max_items:
            errors.append(f"{path}: {len(instance)} items > maxItems {max_items}")
        items = schema.get("items")
        if isinstance(items, dict):
            for i, element in enumerate(instance):
                errors.extend(json_schema_errors(element, items, f"{path}[{i}]"))
    return errors
