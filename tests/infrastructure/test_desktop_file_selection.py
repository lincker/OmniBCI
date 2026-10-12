"""An installed app can be configured before a recording file is selected."""
import json
from importlib import resources

import pytest

from omnibci.application.plugin_schema import PluginSchemaError, validate_parameters, validate_schema
from omnibci.infrastructure.plugins.acquisition.file.reader import FileReader


@pytest.mark.parametrize("parameters", [{}, {"data_file_path": ""}, {"data_file_path": "  "}])
def test_reader_requires_an_explicit_path(parameters):
    with pytest.raises(ValueError, match="Select a recording file"):
        FileReader(parameters=parameters)


def test_file_profile_can_be_saved_before_selecting_a_recording():
    schema = json.loads(resources.files("omnibci.infrastructure.plugins.acquisition.file")
                        .joinpath("default.schema.json").read_text())
    assert validate_parameters(schema, {})["data_file_path"] == ""
    assert schema["properties"]["data_file_path"]["x-widget"] == "file-path"


@pytest.mark.parametrize("kind,widget", [("integer", "file-path"), ("string", "shell"), ("string", True)])
def test_file_widget_is_a_closed_string_annotation(kind, widget):
    schema = {"type": "object", "additionalProperties": False,
              "properties": {"path": {"type": kind, "x-widget": widget}}}
    with pytest.raises(PluginSchemaError, match="x-widget"):
        validate_schema(schema)


def test_file_widget_does_not_change_string_parameter_validation():
    schema = {"type": "object", "additionalProperties": False,
              "properties": {"path": {"type": "string", "x-widget": "file-path"}}}
    assert validate_parameters(schema, {"path": "a/b.cnt"}) == {"path": "a/b.cnt"}
    with pytest.raises(PluginSchemaError):
        validate_parameters(schema, {"path": 42})
