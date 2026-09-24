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


def test_standalone_legacy_mapping_does_not_import_migration_or_initialization():
    result = subprocess.run([sys.executable, "-c", dedent("""
        import importlib.abc
        import sys

        class RejectConfigurationServices(importlib.abc.MetaPathFinder):
            def find_spec(self, fullname, path=None, target=None):
                if fullname == "omnibci.application.services" or fullname.startswith(
                    "omnibci.application.services."
                ):
                    raise AssertionError("Runtime mapping imported a configuration service: " + fullname)

        sys.meta_path.insert(0, RejectConfigurationServices())
        from omnibci.infrastructure.plugins.plugin_catalog import (
            PluginCatalog, PluginRoot, vendor_plugins_dir,
        )
        from omnibci.infrastructure.plugins.runtime_configuration import load_configured_plugin
        from omnibci.sdk import Decoder

        assert "omnibci.application.legacy_plugin_config" not in sys.modules
        catalog = PluginCatalog({
            "decoder": lambda: [PluginRoot(vendor_plugins_dir(), "builtin", 100)],
        })
        class CaptureLoader:
            @staticmethod
            def instantiate(descriptor, expected_base, **kwargs):
                assert expected_base is Decoder
                return kwargs["parameters"]

        snapshot = {"ssvep.duration": 2.5, "ssvep.offset_time": 0,
                    "ALGORITHM.window_duration": 4.0, "ALGORITHM.offset_time": 0.2}
        for alias, expected in (
            ("THUBCI_SSVEP_classic40CCA", {"window_duration": 2.5, "offset_time": 0}),
            ("THUBCI_SSVEP_classic40FBCCA", {"window_duration": 4.0, "offset_time": 0.2}),
        ):
            assert load_configured_plugin(
                snapshot, "processing.decoder", "decoder", Decoder, legacy_alias=alias,
                catalog=catalog, loader=CaptureLoader,
            ) == expected
        assert "omnibci.application.legacy_plugin_config" in sys.modules
    """)], capture_output=True, text=True, timeout=30)

    assert result.returncode == 0, result.stderr
