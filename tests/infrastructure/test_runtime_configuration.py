"""Pinned runtime resolution rejects stale or incomplete selections."""
from dataclasses import replace

import pytest

from omnibci.application.plugin_profiles import ProfileBinding
from omnibci.infrastructure.plugins.loader import PluginLoadError
from omnibci.infrastructure.plugins.plugin_catalog import PluginCatalog, PluginNotFoundError
from omnibci.infrastructure.plugins.runtime_configuration import (
    configured_bindings, load_configured_plugin,
)
from omnibci.sdk import SignalSource


def source_binding():
    descriptor = PluginCatalog().resolve("acquisition", "file_reader")
    return ProfileBinding("acquisition.source", "acquisition", None, descriptor.selection, {})


def test_missing_selected_version_never_falls_back_to_alias():
    binding = source_binding()
    binding = replace(binding, selection=replace(binding.selection, version="unavailable"))
    with pytest.raises(PluginNotFoundError):
        load_configured_plugin({"PLUGINS.bindings": [binding.to_dict()],
                                "ACQUISITION.signal_source": "file_reader"},
                               "acquisition.source", "acquisition", SignalSource,
                               legacy_key="ACQUISITION.signal_source")


def test_explicit_empty_snapshot_never_falls_back_to_legacy():
    with pytest.raises(PluginLoadError, match="No acquisition"):
        load_configured_plugin({"PLUGINS.bindings": [], "ACQUISITION.signal_source": "file_reader"},
                               "acquisition.source", "acquisition", SignalSource,
                               legacy_key="ACQUISITION.signal_source")


def test_duplicate_slots_are_rejected():
    binding = source_binding().to_dict()
    with pytest.raises(PluginLoadError, match="Duplicate"):
        configured_bindings({"PLUGINS.bindings": [binding, binding]})


def test_schema_version_requires_migration_before_loading():
    binding = replace(source_binding(), configuration_version=99)
    with pytest.raises(PluginLoadError, match="migration required"):
        load_configured_plugin({"PLUGINS.bindings": [binding.to_dict()]},
                               "acquisition.source", "acquisition", SignalSource)
