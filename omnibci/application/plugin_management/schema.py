"""Validate the documented, closed JSON Schema subset for plugin parameters.

This module reads data only. It never resolves references, imports plugins or
coerces parameter types. Supported keywords and constraints are defined below.
"""
from __future__ import annotations

import math
import json
import re
from typing import Any


class PluginSchemaError(ValueError):
    """A schema declaration or parameter is invalid at the reported field path."""


_MAX_SCHEMA_DEPTH = 16
_MAX_JSON_DEPTH = 64
_MAX_PARAMETER_BYTES = 1024 * 1024
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


def validate_parameters(schema: dict | None, parameters: dict, *, apply_defaults: bool = True) -> dict:
    """Return independent parameters, with explicit defaults optionally applied.

    Optional objects without their own default are left absent. Nested defaults
    are applied only when that object is present or has an explicit default.
    Without a schema, only the JSON object contract is enforced. The resulting
    object, including defaults, is limited to 1 MiB of encoded JSON.
    """
    values = _copy_json(parameters, "$")
    if not isinstance(values, dict):
        _fail("$", "parameters must be an object")
    if schema is not None:
        validated_schema = validate_schema(schema)
        values = _validate_value(validated_schema, values, "$", apply_defaults=apply_defaults)
    if len(json.dumps(values, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) > _MAX_PARAMETER_BYTES:
        _fail("$", "parameters exceed 1 MiB")
    return values


_MAX_TRIAL_SEQUENCE_LEN = 10_000
_CONTROL_KEYS = frozenset({
    "label", "type", "field", "payload_key", "default", "min", "max", "default_sequence",
})
_SEQUENCE_TOKEN_RE = re.compile(r"\d+(?:\s*-\s*\d+)?")
_SEQUENCE_SEP_RE = re.compile(r"^(?:\s*,\s*|\s+)$")


def _validate_trial_sequence(text: str, minimum: int | float | None, maximum: int | float | None, path: str) -> None:
    count = 0
    cursor = 0
    for match in _SEQUENCE_TOKEN_RE.finditer(text):
        separator = text[cursor:match.start()]
        if cursor == 0:
            if separator.strip() != "":
                _fail(path, "invalid trial sequence syntax")
        elif not _SEQUENCE_SEP_RE.fullmatch(separator):
            _fail(path, "invalid trial sequence syntax")
        parts = [int(part.strip()) for part in match.group(0).split("-")]
        first = parts[0]
        last = parts[1] if len(parts) == 2 else first
        if last < first:
            _fail(path, "invalid trial sequence range")
        if minimum is not None and first < minimum:
            _fail(path, "trial sequence target is below min")
        if maximum is not None and last > maximum:
            _fail(path, "trial sequence target exceeds max")
        span = last - first + 1
        if span > _MAX_TRIAL_SEQUENCE_LEN - count:
            _fail(path, f"trial sequence exceeds {_MAX_TRIAL_SEQUENCE_LEN} trials")
        count += span
        cursor = match.end()
    if count == 0 or text[cursor:].strip() != "":
        _fail(path, "invalid trial sequence syntax")


def validate_controls(controls: dict) -> dict:
    """Validate manifest contribution trial controls consistently with the console."""
    copied = _copy_json(controls, "controls")
    if not isinstance(copied, dict):
        _fail("controls", "must be a table")
    seen_fields: set[str] = set()
    for name, control in copied.items():
        path = _field("controls", name)
        if not isinstance(control, dict):
            _fail(path, "control must be a table")
        unknown = sorted(set(control) - _CONTROL_KEYS)
        if unknown:
            _fail(_field(path, unknown[0]), "unsupported control field")
        field = control.get("field")
        if field not in ("n_trials", "trial_params"):
            _fail(_field(path, "field"), "must be 'n_trials' or 'trial_params'")
        if field in seen_fields:
            _fail(_field(path, "field"), f"duplicate control field {field!r}")
        seen_fields.add(field)
        if "label" in control and not isinstance(control["label"], str):
            _fail(_field(path, "label"), "must be a string")
        control_type = control.get("type")
        if control_type is not None and control_type not in ("int", "float"):
            _fail(_field(path, "type"), "must be 'int' or 'float'")
        if field == "trial_params":
            payload_key = control.get("payload_key")
            if not isinstance(payload_key, str) or not payload_key.strip():
                _fail(_field(path, "payload_key"), "trial_params control requires a non-empty payload_key")
        elif "payload_key" in control:
            _fail(_field(path, "payload_key"), "payload_key is only valid for trial_params controls")
        for bound in ("default", "min", "max"):
            if bound in control:
                value = control[bound]
                if type(value) not in (int, float):
                    _fail(_field(path, bound), "must be a finite number")
        minimum = control.get("min")
        maximum = control.get("max")
        initial = control.get("default")
        if minimum is not None and maximum is not None and minimum > maximum:
            _fail(path, "min must be <= max")
        if initial is not None:
            if type(initial) is not int:
                _fail(_field(path, "default"), "must be an integer")
            if (minimum is not None and initial < minimum) or (maximum is not None and initial > maximum):
                _fail(_field(path, "default"), "default must lie within [min, max]")
        if "default_sequence" in control:
            sequence = control["default_sequence"]
            if not isinstance(sequence, str) or field != "trial_params" or control_type != "int":
                _fail(_field(path, "default_sequence"), "requires trial_params control with type 'int'")
            _validate_trial_sequence(sequence, minimum, maximum, _field(path, "default_sequence"))
    return copied
