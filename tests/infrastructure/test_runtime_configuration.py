"""Pinned runtime resolution rejects stale or incomplete selections."""
from dataclasses import replace
import subprocess
import sys
from textwrap import dedent

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
                               "acquisition.source", "acquisition", SignalSource)


def test_explicit_empty_snapshot_never_falls_back_to_legacy():
    with pytest.raises(PluginLoadError, match="No acquisition"):
        load_configured_plugin({"PLUGINS.bindings": [], "ACQUISITION.signal_source": "file_reader"},
                               "acquisition.source", "acquisition", SignalSource)


def test_duplicate_slots_are_rejected():
    binding = source_binding().to_dict()
    with pytest.raises(PluginLoadError, match="Duplicate"):
        configured_bindings({"PLUGINS.bindings": [binding, binding]})


def test_legacy_alias_without_pinned_bindings_is_rejected_before_loading():
    class RejectLoader:
        @staticmethod
        def instantiate(*args, **kwargs):
            raise AssertionError("No plugin should be instantiated")

    with pytest.raises(PluginLoadError, match="pinned"):
        load_configured_plugin({"ACQUISITION.signal_source": "file_reader"},
                               "acquisition.source", "acquisition", SignalSource,
                               loader=RejectLoader)


def test_schema_version_requires_migration_before_loading():
    binding = replace(source_binding(), configuration_version=99)
    with pytest.raises(PluginLoadError, match="migration required"):
        load_configured_plugin({"PLUGINS.bindings": [binding.to_dict()]},
                               "acquisition.source", "acquisition", SignalSource)


def test_pinned_loading_does_not_import_legacy_mapping():
    result = subprocess.run([sys.executable, "-c", dedent("""
        import sys
        from omnibci.application.plugin_profiles import ProfileBinding
        from omnibci.infrastructure.plugins.plugin_catalog import PluginCatalog
        from omnibci.infrastructure.plugins.runtime_configuration import load_configured_plugin
        from omnibci.sdk import Decoder, PluginContext
        from omnibci.domain.montage import ChannelDescriptor, SourceMetadata

        catalog = PluginCatalog()
        descriptor = catalog.resolve("decoder", "THUBCI_SSVEP_classic40CCA")
        binding = ProfileBinding("processing.decoder", "decoder", None, descriptor.selection,
                                 {"window_duration": 2.5, "offset_time": 0})
        class CaptureLoader:
            @staticmethod
            def instantiate(descriptor, expected_base, **kwargs):
                return kwargs["parameters"]

        assert load_configured_plugin(
            {"PLUGINS.bindings": [binding.to_dict()]}, "processing.decoder", "decoder", Decoder,
            catalog=catalog, loader=CaptureLoader,
            context=PluginContext(input_signal=SourceMetadata("source", 250, (ChannelDescriptor(0, "Oz"),), 1)),
        ) == {"window_duration": 2.5, "offset_time": 0}
        assert "omnibci.application.legacy_plugin_config" not in sys.modules
    """)], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
