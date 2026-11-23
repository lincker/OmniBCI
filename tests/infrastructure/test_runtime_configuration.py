"""Pinned runtime resolution rejects stale or incomplete selections."""
from tests.plugin_configuration_helpers import builtin_selection, prepared_snapshot
from dataclasses import replace
from unittest.mock import Mock

import pytest

from omnibci.application.configuration.models import ProfileBinding
from omnibci.infrastructure.plugin_management.loader import PluginLoadError
from omnibci.infrastructure.plugin_management.catalog import PluginCatalog
from omnibci.infrastructure.plugin_management.preparation import configured_bindings
from omnibci.infrastructure.plugin_management.runtime import load_configured_plugin
from omnibci.sdk import SignalSource


@pytest.mark.parametrize("module,kind", [("stimulation", "paradigm"), ("actuation", "effector")])
@pytest.mark.parametrize("sample_rate", [0, "invalid", None])
def test_output_inputs_do_not_depend_on_acquisition(module, kind, sample_rate):
    from omnibci.sdk import Effector, Paradigm
    alias = "logger" if kind == "effector" else "STIMULATION_THUBCI_SSVEP_classic40"
    binding = ProfileBinding(f"{module}.{kind}", kind, None, builtin_selection(kind, alias), {})
    loader = Mock()
    load_configured_plugin(prepared_snapshot({"PLUGINS.bindings": [binding.to_dict()],
                            "ACQUISITION.sample_rate": sample_rate,
                            "CONFIG.module_revisions": {module: "output-revision"}}),
                           binding.slot, kind, Effector if kind == "effector" else Paradigm,
                           loader=loader)
    assert loader.instantiate.call_args.kwargs == {"parameters": {}}


def source_binding():
    descriptor = PluginCatalog().resolve_selection("acquisition", builtin_selection("acquisition", "file_reader"))
    return ProfileBinding("acquisition.source", "acquisition", None, descriptor.selection, {})


@pytest.mark.parametrize("names", [["obsolete", "names"], None, 42])
def test_source_inputs_ignore_retired_flat_channel_names(names):
    binding = replace(source_binding(), parameters={"data_file_path": "selected.cnt"})
    loader = Mock()
    load_configured_plugin(prepared_snapshot({"PLUGINS.bindings": [binding.to_dict()],
                            "ACQUISITION.channel_names": names,
                            "ACQUISITION.channel_labels": ["C3", "Trigger"],
                            "CONFIG.module_revisions": {"acquisition": "current-revision"}}),
                           binding.slot, binding.kind, SignalSource, loader=loader)
    assert loader.instantiate.call_args.kwargs == {"parameters": {"data_file_path": "selected.cnt"}}


def test_missing_selected_version_never_falls_back_to_alias():
    binding = source_binding()
    binding = replace(binding, selection=replace(binding.selection, version="999.0.0"))
    with pytest.raises(PluginLoadError, match='Unknown'):
        load_configured_plugin(prepared_snapshot({"PLUGINS.bindings": [binding.to_dict()],
                                "ACQUISITION.signal_source": "file_reader"}),
                               "acquisition.source", "acquisition", SignalSource)


def test_explicit_empty_snapshot_never_falls_back_to_legacy():
    with pytest.raises(PluginLoadError, match="No acquisition"):
        load_configured_plugin({"PLUGINS.bindings": [], "ACQUISITION.signal_source": "file_reader"},
                               "acquisition.source", "acquisition", SignalSource)


def test_duplicate_slots_are_rejected():
    binding = source_binding().to_dict()
    with pytest.raises(PluginLoadError, match="Duplicate"):
        configured_bindings({"PLUGINS.bindings": [binding, binding]})


def test_source_loading_does_not_parse_dormant_decoder_input():
    binding = replace(source_binding(), parameters={"data_file_path": "selected.cnt"})
    malformed_decoder = {
        "slot": "processing.decoder", "kind": "decoder",
        "selection": {"package_id": "missing.decoder", "contribution_id": "default"},
        "parameters": {}, "configuration_version": 1, "profile_revision": 0,
        "processing_input": {"schema_version": 2, "outputs": "invalid"},
    }
    source = load_configured_plugin(prepared_snapshot({"PLUGINS.bindings": [binding.to_dict(), malformed_decoder]}, ('acquisition',)),
                                    "acquisition.source", "acquisition", SignalSource)
    assert source.file_path.endswith("selected.cnt")


def test_loading_one_slot_still_rejects_duplicate_slot_identities():
    binding = replace(source_binding(), parameters={"data_file_path": "selected.cnt"}).to_dict()
    with pytest.raises(PluginLoadError, match="Duplicate"):
        load_configured_plugin({"PLUGINS.bindings": [binding, binding]},
                               "acquisition.source", "acquisition", SignalSource)


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
        load_configured_plugin(prepared_snapshot({"PLUGINS.bindings": [binding.to_dict()]}),
                               "acquisition.source", "acquisition", SignalSource)
