"""Plugin configuration declarations and parameters are strictly validated."""
from copy import deepcopy

import pytest

from omnibci.application.plugin_schema import (
    PluginSchemaError,
    validate_parameters,
    validate_schema,
)


def _object(properties=None, **fields):
    return {"type": "object", "properties": properties or {},
            "additionalProperties": False, **fields}


def _field(schema):
    return _object({"value": schema}, required=["value"])


def test_defaults_fill_required_fields_and_preserve_independent_copies():
    schema = _object({
        "port": {"type": "integer", "minimum": 1, "default": 4000},
        "bands": {"type": "array", "items": {"type": "number"}, "default": [8, 12]},
        "device": _object({"host": {"type": "string", "default": "localhost"}},
                          required=["host"], default={}),
    }, required=["port", "bands", "device"])
    original = deepcopy(schema)
    parameters = {}

    result = validate_parameters(schema, parameters)

    assert result == {"port": 4000, "bands": [8, 12], "device": {"host": "localhost"}}
    result["bands"].append(16)
    result["device"]["host"] = "changed"
    assert schema == original
    assert parameters == {}
    assert validate_parameters(schema, parameters)["bands"] == [8, 12]


def test_schema_and_provided_parameter_containers_are_copied():
    schema = _object({"values": {"type": "array", "items": {"type": "integer"}}})
    parameters = {"values": [1, 2]}
    copied_schema = validate_schema(schema)
    copied_schema["properties"]["values"]["items"]["minimum"] = 1
    result = validate_parameters(schema, parameters)
    result["values"].append(3)

    assert "minimum" not in schema["properties"]["values"]["items"]
    assert parameters == {"values": [1, 2]}


def test_optional_object_is_not_created_from_its_child_defaults():
    schema = _object({"device": _object({"port": {"type": "integer", "default": 4000}})})
    assert validate_parameters(schema, {}) == {}
    assert validate_parameters(schema, {"device": {}}) == {"device": {"port": 4000}}


def test_defaults_can_be_disabled():
    schema = _field({"type": "integer", "default": 3})
    with pytest.raises(PluginSchemaError, match=r"\$\.value.*required"):
        validate_parameters(schema, {}, apply_defaults=False)
    assert validate_parameters(schema, {"value": 4}, apply_defaults=False) == {"value": 4}


@pytest.mark.parametrize(("field", "value"), [
    ({"type": "integer"}, True), ({"type": "integer"}, 1.5),
    ({"type": "integer"}, "3"), ({"type": "integer"}, 3.0),
    ({"type": "number"}, False), ({"type": "number"}, float("nan")),
    ({"type": "number"}, float("inf")), ({"type": "number"}, float("-inf")),
    ({"type": "string"}, None), ({"type": "boolean"}, 1),
    ({"type": "array", "items": {"type": "integer"}}, (1, 2)),
    (_object(), []),
])
def test_wrong_types_and_non_json_values_are_rejected(field, value):
    with pytest.raises(PluginSchemaError, match=r"\$\.value"):
        validate_parameters(_field(field), {"value": value})


@pytest.mark.parametrize(("field", "valid", "invalid"), [
    ({"type": "integer", "minimum": 1, "maximum": 3}, 3, 4),
    ({"type": "number", "minimum": 0.1, "maximum": 1}, 0.5, 0),
    ({"type": "string", "minLength": 2, "maxLength": 3}, "ab", "a"),
    ({"type": "string", "maxLength": 2}, "ab", "abc"),
    ({"type": "string", "enum": ["a", "b"]}, "b", "c"),
    ({"type": "boolean", "enum": [True]}, True, False),
    ({"type": "array", "items": {"type": "integer"}, "minItems": 1}, [1], []),
    ({"type": "array", "items": {"type": "integer"}, "maxItems": 1}, [1], [1, 2]),
])
def test_supported_constraints(field, valid, invalid):
    assert validate_parameters(_field(field), {"value": valid}) == {"value": valid}
    with pytest.raises(PluginSchemaError, match=r"\$\.value"):
        validate_parameters(_field(field), {"value": invalid})


def test_nested_error_paths_identify_array_item_and_property():
    schema = _field({"type": "array", "items": _object({"port": {
        "type": "integer", "minimum": 1,
    }}, required=["port"])})
    with pytest.raises(PluginSchemaError, match=r"\$\.value\[1\]\.port"):
        validate_parameters(schema, {"value": [{"port": 1}, {"port": 0}]})


def test_unknown_fields_are_rejected_at_each_level():
    schema = _object({"device": _object({"port": {"type": "integer"}})})
    for parameters, path in (({"other": 1}, r"\$\.other"),
                             ({"device": {"other": 1}}, r"\$\.device\.other")):
        with pytest.raises(PluginSchemaError, match=path):
            validate_parameters(schema, parameters)


@pytest.mark.parametrize("field", [
    {}, {"type": "null"}, {"type": ["integer", "string"]},
    {"type": "integer", "minimum": True}, {"type": "number", "maximum": float("nan")},
    {"type": "number", "minimum": 2, "maximum": 1},
    {"type": "string", "minLength": -1}, {"type": "string", "maxLength": 1.5},
    {"type": "string", "minLength": 2, "maxLength": 1},
    {"type": "boolean", "minimum": 0}, {"type": "array"},
    {"type": "array", "items": []},
    {"type": "array", "items": {"type": "integer"}, "minItems": True},
    {"type": "array", "items": {"type": "integer"}, "minItems": 2, "maxItems": 1},
    {"type": "string", "enum": []}, {"type": "string", "enum": "x"},
    {"type": "integer", "enum": [1, True]}, {"type": "number", "enum": [1, 1.0]},
    {"type": "integer", "enum": [0], "minimum": 1},
    {"type": "integer", "default": True}, {"type": "integer", "default": -1, "minimum": 0},
    {"type": "string", "enum": ["a"], "default": "b"},
    {"type": "array", "items": {"type": "integer"}, "default": ["1"]},
    {"type": "string", "title": 3}, {"type": "string", "description": []},
    {"type": "string", "x-scope": "cloud"}, {"type": "string", "x-apply": "hot"},
    {"type": "string", "format": "uri"}, {"type": "string", "$ref": "https://example.org/schema"},
    {"type": "string", "oneOf": []}, {"type": "string", "typo": 1},
    {"type": "object", "additionalProperties": True},
    {"type": "object", "properties": {}},
    _object(required="value"), _object(required=["unknown"]),
    _object({"value": {"type": "string"}}, required=["value", "value"]),
    _object({"value": {"type": "integer"}}, required=["value"], default={}),
])
def test_malformed_declarations_and_defaults_fail_before_parameter_use(field):
    with pytest.raises(PluginSchemaError, match="schema"):
        validate_schema(_field(field))


@pytest.mark.parametrize("scope", ["portable", "machine", "secret"])
@pytest.mark.parametrize("apply", ["restart", "module", "trial"])
def test_metadata_is_preserved_without_changing_parameter_values(scope, apply):
    schema = _field({"type": "string", "x-scope": scope, "x-apply": apply})
    assert validate_schema(schema) == schema
    assert validate_parameters(schema, {"value": "data"}) == {"value": "data"}


@pytest.mark.parametrize("schema", [True, [], None, {"type": "string"}, {1: "value"}])
def test_schema_root_must_be_a_json_object_schema(schema):
    with pytest.raises(PluginSchemaError):
        validate_schema(schema)


def test_invalid_default_cannot_hide_behind_explicit_valid_parameters():
    with pytest.raises(PluginSchemaError, match="default"):
        validate_parameters(_field({"type": "integer", "default": "bad"}), {"value": 1})


def test_json_integer_validation_does_not_convert_large_values_to_float():
    value = 10 ** 400
    assert validate_parameters(_field({"type": "number"}), {"value": value}) == {"value": value}


@pytest.mark.parametrize("parameters", [None, [], {1: "value"}, {"value": object()}])
def test_parameters_must_be_a_json_object(parameters):
    with pytest.raises(PluginSchemaError, match=r"\$"):
        validate_parameters(_field({"type": "string"}), parameters)


def test_deep_and_cyclic_declarations_are_rejected_without_recursion_errors():
    deep = {"type": "integer"}
    for _ in range(40):
        deep = {"type": "array", "items": deep}
    cyclic = _object()
    cyclic["properties"]["self"] = cyclic
    for schema in (_field(deep), cyclic):
        with pytest.raises(PluginSchemaError, match="depth"):
            validate_schema(schema)


def test_cyclic_parameter_values_fail_without_recursion_errors():
    parameters = {}
    parameters["self"] = parameters
    with pytest.raises(PluginSchemaError, match="depth"):
        validate_parameters(_object(), parameters)
