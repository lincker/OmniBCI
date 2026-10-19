"""Configuration schemas are static package data and are checked before execution."""
import json

import pytest
import tomlkit

from omnibci.application.plugin_schema import validate_parameters
from omnibci.infrastructure.plugins.loader import PluginLoader, PluginLoadError
from omnibci.infrastructure.plugins.plugin_catalog import (
    PluginCatalog, PluginManifestError, PluginRoot, vendor_plugins_dir,
)
from omnibci.sdk import Effector, PluginContext


SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {"gain": {"type": "number", "minimum": 0, "default": 1}},
    "required": ["gain"],
}


def package(tmp_path, **configuration):
    manifest = {"manifest_version": 2, "package": {
        "id": "example", "publisher": "test", "version": "1", "api_version": "2",
    }, "contributions": [{
        "id": "output", "kind": "effector", "entrypoint": "output:Output",
        "configuration": configuration or {"schema": "default.schema.json", "schema_version": 1},
    }]}
    (tmp_path / "manifest.toml").write_text(tomlkit.dumps(manifest), encoding="utf-8")
    (tmp_path / "default.schema.json").write_text(json.dumps(SCHEMA), encoding="utf-8")
    return PluginCatalog({"effector": lambda: [PluginRoot(tmp_path, "user", 0)]})


def test_all_builtins_own_valid_defaults_for_their_required_parameters():
    catalog = PluginCatalog({"effector": lambda: [PluginRoot(vendor_plugins_dir(), "builtin", 100)]})
    count = 0
    for kind in ("acquisition", "decoder", "filter", "epoch_detector", "effector", "paradigm", "trigger"):
        for plugin in catalog.list_contributions(kind):
            assert plugin.configuration_version == 1
            values = validate_parameters(plugin.configuration_schema, {})
            assert set(plugin.configuration_schema.get("required", [])) <= set(values)
            assert set(values) <= set(plugin.configuration_schema["properties"])
            count += 1
    assert count == 11


@pytest.mark.parametrize("alias", ["file_reader", "neuroscan_curry8_reader"])
def test_observed_sources_preserve_optional_expectations_without_an_invented_baseline(alias):
    catalog = PluginCatalog({"effector": lambda: [PluginRoot(vendor_plugins_dir(), "builtin", 100)]})
    schema = catalog.resolve("acquisition", alias).configuration_schema
    defaults = validate_parameters(schema, {})
    assert not {"sample_rate", "channel_number"} & defaults.keys()
    saved = validate_parameters(schema, {"sample_rate": 500, "channel_number": 33})
    assert saved["sample_rate"] == 500
    assert saved["channel_number"] == 33


def test_scan45_retains_required_stream_dimensions_and_defaults():
    catalog = PluginCatalog({"effector": lambda: [PluginRoot(vendor_plugins_dir(), "builtin", 100)]})
    schema = catalog.resolve("acquisition", "neuroscan_scan45_reader").configuration_schema
    assert {"sample_rate", "channel_number"} <= set(schema["required"])
    defaults = validate_parameters(schema, {})
    assert defaults["sample_rate"] == 1000
    assert defaults["channel_number"] == 10


@pytest.mark.parametrize("configuration", [
    {"schema": "../outside.json", "schema_version": 1},
    {"schema": "/outside.json", "schema_version": 1},
    {"schema": "C:\\outside.json", "schema_version": 1},
    {"schema": "missing.json", "schema_version": 1},
    {"schema": "default.schema.json", "schema_version": True},
    {"schema": "default.schema.json"},
])
def test_invalid_schema_declaration_blocks_package(tmp_path, configuration):
    catalog = package(tmp_path, **configuration)
    with pytest.raises(PluginManifestError, match="configuration"):
        catalog.resolve("effector", "output")


@pytest.mark.parametrize("content", [
    '{"type":"object","type":"object"}',
    '{"type":"object","additionalProperties":false,"$ref":"https://example.com"}',
    '{"type":"number"}', '{"type":',
])
def test_malformed_schema_blocks_package(tmp_path, content):
    catalog = package(tmp_path)
    (tmp_path / "default.schema.json").write_text(content, encoding="utf-8")
    with pytest.raises(PluginManifestError, match="configuration"):
        catalog.resolve("effector", "output")


def test_schema_symlink_is_rejected(tmp_path):
    catalog = package(tmp_path)
    schema = tmp_path / "default.schema.json"
    schema.rename(tmp_path / "real.json")
    schema.symlink_to(tmp_path / "real.json")
    with pytest.raises(PluginManifestError, match="symlinks"):
        catalog.resolve("effector", "output")


def test_invalid_parameters_fail_before_plugin_code_runs(tmp_path, monkeypatch):
    selected = package(tmp_path).resolve("effector", "output")
    monkeypatch.setattr(PluginLoader, "load_entrypoint", lambda *a: pytest.fail("imported"))
    with pytest.raises(PluginLoadError, match="gain"):
        PluginLoader.instantiate(selected, Effector, parameters={"gain": True}, context=PluginContext())
