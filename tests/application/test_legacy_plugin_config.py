"""Legacy parameter mapping stays independent from migration and initialization."""
from copy import deepcopy
from dataclasses import replace
from unittest.mock import Mock

import pytest

from omnibci.application.legacy_plugin_config import (
    CCA_PLUGIN_TYPE, PluginConfigMigrationError, legacy_parameters,
    resolve_legacy_configuration,
)
from omnibci.application.plugin_profiles import PluginSelection
from omnibci.application.plugin_schema import PluginSchemaError
from omnibci.application.ports.plugin_configuration_catalog import PluginConfiguration


def definition(kind="decoder", alias=CCA_PLUGIN_TYPE):
    return PluginConfiguration(
        PluginSelection("test.decoder", "default", "1"), kind, alias, "Example", None,
        {"type": "object", "additionalProperties": False, "properties": {
            "window_duration": {"type": "number", "minimum": 0.1, "default": 3.0},
            "offset_time": {"type": "number", "minimum": 0, "default": 0.14},
        }}, 1, True,
    )


@pytest.mark.parametrize("alias,expected", [
    (CCA_PLUGIN_TYPE, {"window_duration": 2.5, "offset_time": 0}),
    ("THUBCI_SSVEP_classic40FBCCA", {"window_duration": 4.0, "offset_time": 0.2}),
])
def test_decoder_mapping_preserves_its_original_parameter_sources(alias, expected):
    settings = {"ssvep.duration": 2.5, "ssvep.offset_time": 0,
                "ALGORITHM.window_duration": 4.0, "ALGORITHM.offset_time": 0.2}
    assert legacy_parameters(definition(alias=alias), settings) == expected


def test_cca_uses_schema_defaults_when_ssvep_values_are_absent():
    assert legacy_parameters(definition(), {"ALGORITHM.window_duration": 9}) == {
        "window_duration": 3.0, "offset_time": 0.14,
    }


@pytest.mark.parametrize("kind", ["acquisition", "trigger"])
def test_acquisition_parameters_are_copied_from_declared_keys_only(kind):
    selected = replace(definition(kind), schema={
        "type": "object", "additionalProperties": False, "properties": {
            "channels": {"type": "array", "items": {"type": "string"}, "default": []},
        },
    })
    settings = {"ACQUISITION.channels": ["C3"], "ALGORITHM.channels": ["C4"],
                "ACQUISITION.unrelated": "preserve"}
    original_settings, original_schema = deepcopy(settings), deepcopy(selected.schema)
    mapped = legacy_parameters(selected, settings)
    assert mapped == {"channels": ["C3"]}
    mapped["channels"].append("Cz")
    assert settings == original_settings
    assert selected.schema == original_schema


@pytest.mark.parametrize("kind", ["epoch_detector", "filter", "paradigm", "effector"])
def test_other_plugin_kinds_retain_schema_defaults(kind):
    assert legacy_parameters(definition(kind), {"ALGORITHM.window_duration": 9,
                                               "STIMULATION.window_duration": 8}) == {
        "window_duration": 3.0, "offset_time": 0.14,
    }


def test_missing_schema_keeps_the_migration_error():
    with pytest.raises(PluginConfigMigrationError, match="configuration schema is missing"):
        legacy_parameters(replace(definition(), schema=None), {})


@pytest.mark.parametrize("value", [None, True, -1])
def test_invalid_present_values_are_not_replaced_with_defaults(value):
    with pytest.raises(PluginSchemaError):
        legacy_parameters(definition(), {"ssvep.duration": value})


def test_alias_resolution_returns_the_exact_catalog_definition():
    selected = definition()
    catalog = Mock(legacy=Mock(return_value=selected), get=Mock(return_value=selected))
    assert resolve_legacy_configuration(catalog, "decoder", CCA_PLUGIN_TYPE) is selected
    catalog.legacy.assert_called_once_with("decoder", CCA_PLUGIN_TYPE)
    catalog.get.assert_called_once_with("decoder", selected.selection)


@pytest.mark.parametrize("alias", [None, "", " decoder "])
def test_invalid_alias_is_rejected_before_catalog_access(alias):
    catalog = Mock()
    with pytest.raises(PluginConfigMigrationError, match="a non-empty plugin alias is required"):
        resolve_legacy_configuration(catalog, "decoder", alias)
    assert catalog.mock_calls == []


@pytest.mark.parametrize("failure", ["kind", "version", "schema", "configuration_version"])
def test_alias_resolution_rejects_incomplete_or_inconsistent_metadata(failure):
    selected = definition()
    invalid = replace(selected, **{
        "kind": {"kind": "filter"},
        "version": {"selection": replace(selected.selection, version=None)},
        "schema": {"schema": None},
        "configuration_version": {"configuration_version": None},
    }[failure])
    catalog = Mock(legacy=Mock(return_value=selected), get=Mock(return_value=invalid))
    message = "exact versioned decoder" if failure in {"kind", "version"} else "schema/version is missing"
    with pytest.raises(PluginConfigMigrationError, match=message):
        resolve_legacy_configuration(catalog, "decoder", CCA_PLUGIN_TYPE)


@pytest.mark.parametrize("error", [ValueError("invalid"), KeyError("missing"), RuntimeError("unavailable")])
def test_alias_resolution_preserves_catalog_error_context(error):
    catalog = Mock(legacy=Mock(side_effect=error))
    with pytest.raises(PluginConfigMigrationError, match="Cannot resolve decoder plugin") as captured:
        resolve_legacy_configuration(catalog, "decoder", CCA_PLUGIN_TYPE)
    assert captured.value.__cause__ is error


def test_migration_import_path_reexports_the_same_mapping_contracts():
    from omnibci.application.services import plugin_config_migration

    assert plugin_config_migration.PluginConfigMigrationError is PluginConfigMigrationError
    assert plugin_config_migration.legacy_parameters is legacy_parameters
