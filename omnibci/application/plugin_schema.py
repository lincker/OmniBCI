"""Validate the documented, closed JSON Schema subset for plugin parameters.

This module reads data only. It never resolves references, imports plugins or
coerces parameter types. See docs/plugin-configuration-schema.md for the subset.
"""
from __future__ import annotations

import math
from typing import Any


class PluginSchemaError(ValueError):
    """A schema declaration or parameter is invalid at the reported field path."""


_MAX_SCHEMA_DEPTH = 16
_MAX_JSON_DEPTH = 64
_COMMON = {"type", "title", "description", "default", "enum", "x-scope", "x-apply", "x-widget"}
_KEYWORDS = {
    "object": {"properties", "required", "additionalProperties"},
    "array": {"items", "minItems", "maxItems"},
    "integer": {"minimum", "maximum"},
    "number": {"minimum", "maximum"},
    "string": {"minLength", "maxLength"},
    "boolean": set(),
}


def _fail(path: str, message: str) -> None:
    raise PluginSchemaError(f"{path}: {message}")


def _field(path: str, name: str) -> str:
    return f"{path}.{name}" if name.isidentifier() else f"{path}[{name!r}]"


def _copy_json(value: Any, path: str, depth: int = 0) -> Any:
    if depth > _MAX_JSON_DEPTH:
        _fail(path, f"JSON nesting exceeds depth {_MAX_JSON_DEPTH}")
    if value is None or type(value) in (str, bool, int):
        return value
    if type(value) is float:
        if not math.isfinite(value):
            _fail(path, "numbers must be finite")
        return value
    if isinstance(value, list):
        return [_copy_json(item, f"{path}[{index}]", depth + 1)
                for index, item in enumerate(value)]
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            if not isinstance(key, str):
                _fail(path, "object keys must be strings")
            result[key] = _copy_json(item, _field(path, key), depth + 1)
        return result
    _fail(path, "value must contain JSON-compatible types only")


def _validate_schema_node(schema: Any, path: str, depth: int = 0) -> None:
    if depth > _MAX_SCHEMA_DEPTH:
        _fail(path, f"schema nesting exceeds depth {_MAX_SCHEMA_DEPTH}")
    if not isinstance(schema, dict):
        _fail(path, "schema must be an object")
    kind = schema.get("type")
    if not isinstance(kind, str) or kind not in _KEYWORDS:
        _fail(_field(path, "type"), "requires a supported single type")
    unknown = sorted(set(schema) - _COMMON - _KEYWORDS[kind])
    if unknown:
        _fail(_field(path, unknown[0]), "unsupported schema keyword")
    for key in ("title", "description"):
        if key in schema and not isinstance(schema[key], str):
            _fail(_field(path, key), "must be a string")
    for key, allowed in (("x-scope", {"portable", "machine", "secret"}),
                         ("x-apply", {"restart", "module", "trial"})):
        if key in schema and (not isinstance(schema[key], str) or schema[key] not in allowed):
            _fail(_field(path, key), f"must be one of {', '.join(sorted(allowed))}")
    if "x-widget" in schema and (kind != "string" or schema["x-widget"] != "file-path"):
        _fail(_field(path, "x-widget"), "file-path is supported only for string fields")

    if kind == "object":
        if schema.get("additionalProperties") is not False:
            _fail(_field(path, "additionalProperties"), "must explicitly be false")
        properties = schema.get("properties", {})
        if not isinstance(properties, dict):
            _fail(_field(path, "properties"), "must be an object")
        required = schema.get("required", [])
        if (not isinstance(required, list) or any(not isinstance(key, str) for key in required)
                or len(required) != len(set(required))):
            _fail(_field(path, "required"), "must be a list of unique property names")
        if any(key not in properties for key in required):
            _fail(_field(path, "required"), "contains an undeclared property")
        for key, child in properties.items():
            _validate_schema_node(child, _field(f"{path}.properties", key), depth + 1)
    elif kind == "array":
        if "items" not in schema:
            _fail(_field(path, "items"), "an item schema is required")
        _validate_schema_node(schema["items"], _field(path, "items"), depth + 1)

    bounds = {"integer": ("minimum", "maximum"), "number": ("minimum", "maximum"),
              "string": ("minLength", "maxLength"), "array": ("minItems", "maxItems")}
    if kind in bounds:
        lower, upper = bounds[kind]
        for key in (lower, upper):
            if key not in schema:
                continue
            value = schema[key]
            if kind in ("integer", "number"):
                if type(value) not in (int, float):
                    _fail(_field(path, key), "must be a finite number")
            elif type(value) is not int or value < 0:
                _fail(_field(path, key), "must be a non-negative integer")
        if lower in schema and upper in schema and schema[lower] > schema[upper]:
            _fail(path, f"{lower} must not exceed {upper}")

    if "enum" in schema:
        values = schema["enum"]
        if not isinstance(values, list) or not values:
            _fail(_field(path, "enum"), "must be a non-empty list")
        for index, value in enumerate(values):
            enum_path = f"{path}.enum[{index}]"
            _validate_value(schema, value, enum_path, apply_defaults=False, check_enum=False)
            if any(value == previous for previous in values[:index]):
                _fail(enum_path, "enum values must be unique")
    if "default" in schema:
        _validate_value(schema, schema["default"], _field(path, "default"), apply_defaults=True)


def _validate_value(schema: dict, value: Any, path: str, *,
                    apply_defaults: bool, check_enum: bool = True) -> Any:
    kind = schema["type"]
    valid_type = {
        "object": isinstance(value, dict), "array": isinstance(value, list),
        "integer": type(value) is int, "number": type(value) in (int, float),
        "string": isinstance(value, str), "boolean": type(value) is bool,
    }[kind]
    if not valid_type:
        _fail(path, f"must be {kind}")

    if kind == "object":
        properties = schema.get("properties", {})
        unknown = sorted(set(value) - set(properties))
        if unknown:
            _fail(_field(path, unknown[0]), "unknown parameter")
        result = {}
        for key, child in properties.items():
            field_path = _field(path, key)
            if key in value:
                item = value[key]
            elif apply_defaults and "default" in child:
                item = child["default"]
            elif key in schema.get("required", []):
                _fail(field_path, "required parameter is missing")
            else:
                continue
            result[key] = _validate_value(child, item, field_path, apply_defaults=apply_defaults)
    elif kind == "array":
        if "minItems" in schema and len(value) < schema["minItems"]:
            _fail(path, f"must contain at least {schema['minItems']} items")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            _fail(path, f"must contain at most {schema['maxItems']} items")
        result = [_validate_value(schema["items"], item, f"{path}[{index}]",
                                  apply_defaults=apply_defaults)
                  for index, item in enumerate(value)]
    else:
        result = value
        if kind in ("integer", "number"):
            if "minimum" in schema and value < schema["minimum"]:
                _fail(path, f"must be at least {schema['minimum']}")
            if "maximum" in schema and value > schema["maximum"]:
                _fail(path, f"must be at most {schema['maximum']}")
        elif kind == "string":
            if "minLength" in schema and len(value) < schema["minLength"]:
                _fail(path, f"must contain at least {schema['minLength']} characters")
            if "maxLength" in schema and len(value) > schema["maxLength"]:
                _fail(path, f"must contain at most {schema['maxLength']} characters")
    if check_enum and "enum" in schema and result not in schema["enum"]:
        _fail(path, "must match one of the declared enum values")
    return result


def validate_schema(schema: dict) -> dict:
    """Return a validated, independent schema copy or raise PluginSchemaError."""
    result = _copy_json(schema, "schema")
    _validate_schema_node(result, "schema")
    if result["type"] != "object":
        _fail("schema.type", "configuration root must be an object")
    return result


def validate_parameters(schema: dict, parameters: dict, *, apply_defaults: bool = True) -> dict:
    """Return independent parameters, with explicit defaults optionally applied.

    Optional objects without their own default are left absent. Nested defaults
    are applied only when that object is present or has an explicit default.
    """
    validated_schema = validate_schema(schema)
    values = _copy_json(parameters, "$")
    return _validate_value(validated_schema, values, "$", apply_defaults=apply_defaults)
