"""Decoder input requirements are static, versioned, plugin-owned metadata."""
import pytest
import tomlkit

from omnibci.application.plugin_profiles import PluginSelection
from omnibci.infrastructure.plugins.configuration_catalog import CatalogConfigurationAdapter
from omnibci.infrastructure.plugins.plugin_catalog import PluginCatalog, PluginManifestError, PluginRoot


def package(tmp_path, *, kind="decoder", **fields):
    document = {"manifest_version": 2, "package": {
        "id": "example", "publisher": "test", "version": "1", "api_version": "2",
    }, "contributions": [{"id": "default", "kind": kind, "entrypoint": "decoder:Decoder", **fields}]}
    (tmp_path / "manifest.toml").write_text(tomlkit.dumps(document), encoding="utf-8")
    return PluginCatalog({kind: lambda: [PluginRoot(tmp_path, "user", 0)]})


def test_catalog_reads_requirements_without_importing_plugin_code(tmp_path):
    (tmp_path / "decoder.py").write_text("raise RuntimeError('Do not execute')\n", encoding="utf-8")
    catalog = package(tmp_path, signal_input_contract=2, channel_requirements={
        "signal_type": "eeg", "min_count": 2, "required_names": ["Oz"], "default_names": ["O2", "Oz"],
    })
    selected = PluginSelection("test.example", "default", "1")
    descriptor = catalog.resolve_selection("decoder", selected)
    adapted = CatalogConfigurationAdapter(catalog).get("decoder", selected)
    assert descriptor.signal_input_contract == 2
    assert adapted.channel_requirements == descriptor.channel_requirements
    assert adapted.channel_requirements.default_names == ("O2", "Oz")


@pytest.mark.parametrize("fields", [
    {"signal_input_contract": 1, "channel_requirements": {}},
    {"channel_requirements": {}},
    {"signal_input_contract": 2},
    {"signal_input_contract": 3, "channel_requirements": {}},
    {"signal_input_contract": True, "channel_requirements": {}},
    {"signal_input_contract": "2", "channel_requirements": {}},
    {"signal_input_contract": 2, "channel_requirements": []},
    {"signal_input_contract": 2, "channel_requirements": {"unknown": 1}},
    {"signal_input_contract": 2, "channel_requirements": {"min_count": True}},
    {"signal_input_contract": 2, "channel_requirements": {"min_count": 2, "default_names": ["Oz"]}},
    {"signal_input_contract": 2, "channel_requirements": {"required_names": ["Oz", "Oz"]}},
])
def test_invalid_requirement_or_contract_combinations_reject_package(tmp_path, fields):
    catalog = package(tmp_path, **fields)
    with pytest.raises(PluginManifestError, match="channel_requirements|signal_input_contract"):
        catalog.resolve_legacy("decoder", "default")


@pytest.mark.parametrize("kind", ["acquisition", "filter", "epoch_detector", "effector"])
@pytest.mark.parametrize("contract", [1, 2])
def test_requirements_are_decoder_only(tmp_path, kind, contract):
    catalog = package(tmp_path, kind=kind, signal_input_contract=contract, channel_requirements={})
    with pytest.raises(PluginManifestError, match="channel_requirements|signal_input_contract"):
        catalog.resolve_legacy(kind, "default")


@pytest.mark.parametrize("kind", ["decoder", "filter", "epoch_detector"])
def test_prepared_contract_one_remains_compatible(tmp_path, kind):
    descriptor = package(tmp_path, kind=kind, signal_input_contract=1).resolve_legacy(kind, "default")
    assert descriptor.signal_input_contract == 1
    assert descriptor.channel_requirements is None


def test_legacy_decoder_without_requirements_remains_compatible(tmp_path):
    descriptor = package(tmp_path).resolve_legacy("decoder", "default")
    assert descriptor.signal_input_contract is None
    assert descriptor.channel_requirements is None
