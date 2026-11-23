"""Signal Processing follows pinned mappings and the connected acquisition descriptor."""
from copy import deepcopy
from dataclasses import replace

import pytest

from omnibci.application.processing_source import (
    acquisition_binding_key, binding_for, materialize_processing_binding, processing_binding_key,
    processing_input, requires_signal_preparation, validate_plan_binding, validate_source_binding,
)
from omnibci.application.source_metadata import source_descriptor
from omnibci.application.source_expectations import validate_source_expectations
from omnibci.domain.ports.acquisition import SourceObservation
from omnibci.domain.signal_info import SignalInfo, ChannelDescriptor
from omnibci.domain.channel_mapping import ChannelBinding, ProcessingInput, compile_processing_plan


def descriptor(source, provenance="protocol-defined"):
    return source_descriptor(SourceObservation(native_text="native", signal_info=source,
        native_event="final-row", provenance=provenance), source_id="test.source/reader@1.0.0", connection_id="connection-one")


@pytest.fixture
def source():
    return SignalInfo(sample_rate=250, channels=tuple(
        ChannelDescriptor(index, name, unit="uV", channel_id=f"channel-{index}") for index, name in enumerate(("C3", "C4", "M1"))))


@pytest.fixture
def snapshot():
    return {
        "ACQUISITION.sample_rate": 250,
        "ACQUISITION.channel_count": 4, "CONFIG.module_revisions": {"acquisition": "a" * 64, "processing": "b" * 64},
        "CONFIG.generation": "generation-one", "PLUGINS.bindings": [
            {"slot": "acquisition.source", "kind": "acquisition", "profile_id": "source-profile",
             "selection": {"package_id": "test.source", "contribution_id": "reader", "version": "1.0.0"},
             "parameters": {"sample_rate": 250, "channel_count": 4, "data_file_path": "one.cnt"},
             "profile_revision": 1, "configuration_version": 1},
            {"slot": "processing.decoder", "kind": "decoder", "profile_id": "decoder-profile",
             "selection": {"package_id": "test.decoder", "contribution_id": "decoder", "version": "1.0.0"},
             "parameters": {"window_duration": 1.0}, "profile_revision": 1, "configuration_version": 1,
             "processing_input": ProcessingInput(outputs=(ChannelBinding("C3", "channel-0"),)).to_dict()},
        ],
    }


def test_explicit_mapping_replaces_retired_global_setup(snapshot):
    snapshot["SIGNAL.setup"] = {"source_id": "unrelated-device", "outputs": ["Legacy"]}
    assert processing_input(snapshot).outputs == (ChannelBinding("C3", "channel-0"),)
    assert requires_signal_preparation(snapshot)


def test_retired_input_is_deferred_until_processing_is_used(snapshot):
    binding_for(snapshot, "processing.decoder")["processing_input"] = {"schema_version": 1, "outputs": ["C3"]}
    snapshot["SIGNAL.setup"] = {"source_id": "file_reader"}
    with pytest.raises(ValueError):
        processing_input(snapshot)
    assert requires_signal_preparation(snapshot)






def test_retired_global_setup_does_not_configure_decoder_input(snapshot):
    binding_for(snapshot, "processing.decoder").pop("processing_input")
    snapshot["SIGNAL.setup"] = {"source_id": "file_reader", "outputs": ["C3"]}
    with pytest.raises(ValueError, match="explicit processing_input"):
        processing_input(snapshot)
    snapshot["PLUGINS.bindings"].clear()
    assert processing_input(snapshot) is None
    assert not requires_signal_preparation(snapshot)


def test_processing_binding_survives_profile_revision_and_decoder_parameter_changes(snapshot):
    before = processing_binding_key(snapshot)
    snapshot["CONFIG.module_revisions"]["processing"] = "c" * 64
    snapshot["CONFIG.generation"] = "generation-two"
    binding_for(snapshot, "acquisition.source")["profile_revision"] += 1
    decoder = binding_for(snapshot, "processing.decoder")
    decoder["profile_revision"] += 1
    decoder["parameters"]["window_duration"] = 2.5
    assert processing_binding_key(snapshot) == before


@pytest.mark.parametrize("field,value", [
    ("parameters", {"sample_rate": 250, "channel_count": 4, "data_file_path": "two.cnt"}),
    ("selection", {"package_id": "test.source", "contribution_id": "reader", "version": "2.0.0"}),
])
def test_processing_binding_distinguishes_source_ownership(snapshot, field, value):
    previous = deepcopy(snapshot)
    binding_for(snapshot, "acquisition.source")[field] = value
    assert processing_binding_key(snapshot) != processing_binding_key(previous)
    assert acquisition_binding_key(binding_for(snapshot, "acquisition.source")) != acquisition_binding_key(
        binding_for(previous, "acquisition.source"))


@pytest.mark.parametrize("profile_id", [None, "another-saved-profile"])
def test_processing_binding_ignores_saved_profile_provenance(snapshot, profile_id):
    previous = processing_binding_key(snapshot)
    binding_for(snapshot, "acquisition.source")["profile_id"] = profile_id
    assert processing_binding_key(snapshot) == previous


def test_processing_binding_tracks_mapping_and_type_declarations(snapshot):
    previous = processing_binding_key(snapshot)
    binding_for(snapshot, "processing.decoder")["processing_input"] = ProcessingInput(
        outputs=(ChannelBinding("C3", "channel-0", "eeg"),)).to_dict()
    assert processing_binding_key(snapshot) != previous


def test_binding_key_copies_mutable_parameters(snapshot):
    before = processing_binding_key(snapshot)
    binding_for(snapshot, "acquisition.source")["parameters"]["data_file_path"] = "two.cnt"
    assert before["source"]["parameters"]["data_file_path"] == "one.cnt"


@pytest.mark.parametrize("change,message", [
    ({"source_id": "another-device"}, "different acquisition source"),
    ({"sample_rate": 500}, "sample rate"),
    ({"channels": (ChannelDescriptor(0, "C3", channel_id="channel-0"),)}, "channel count"),
])
def test_source_binding_rejects_descriptor_from_a_different_contract(snapshot, source, change, message):
    metadata = descriptor(source)
    validate_source_binding(snapshot, metadata)
    if "source_id" in change:
        metadata.update(change)
    else:
        metadata = descriptor(replace(source, **change))
    with pytest.raises(ValueError, match=message):
        validate_source_binding(snapshot, metadata)


@pytest.mark.parametrize("provenance", ["device-reported", "file-reported"])
def test_reported_dimensions_remain_visible_but_mismatch_blocks_preparation(snapshot, source, provenance):
    source = replace(source, sample_rate=1000, channels=source.channels[:2])
    metadata = descriptor(source, provenance)
    validate_source_binding(snapshot, metadata)
    with pytest.raises(ValueError, match="differs from the experiment profile"):
        validate_source_expectations(snapshot, metadata)
    with pytest.raises(ValueError, match="different acquisition source"):
        validate_source_binding(snapshot, {**metadata, "source_id": "another-device"})


def test_plan_binding_checks_mapping_while_host_checks_source_identity(snapshot, source):
    value = processing_input(snapshot)
    validate_plan_binding(snapshot, compile_processing_plan(source, value))
    with pytest.raises(ValueError, match="selected processing input"):
        validate_plan_binding(snapshot, compile_processing_plan(source, ProcessingInput()))
    with pytest.raises(ValueError, match="different acquisition source"):
        validate_source_binding(snapshot, {**descriptor(source), "source_id": "different"})


def test_unconfigured_snapshots_do_not_prepare():
    assert binding_for({}, "acquisition.source") is None
    assert acquisition_binding_key(None) is None
    assert processing_input({}) is None
    assert not requires_signal_preparation({})


def test_binding_projection_does_not_create_processing_configuration():
    from omnibci.application.plugin_management.identity import PluginSelection
    from omnibci.application.configuration.models import ProfileBinding
    binding = ProfileBinding("processing.decoder", "decoder", None,
                             PluginSelection("test.decoder", "default", "1"), {})
    result = materialize_processing_binding(binding)
    assert "processing_input" not in result
    assert binding.processing_input is None




def test_selected_decoder_without_input_still_requires_source_preparation(snapshot):
    binding_for(snapshot, "processing.decoder").pop("processing_input")
    with pytest.raises(ValueError, match="explicit processing_input"):
        processing_input(snapshot)
    assert requires_signal_preparation(snapshot)


@pytest.mark.parametrize("provenance", ["device-reported", "file-reported"])
def test_plan_uses_observed_source_values_for_unspecified_expectations(snapshot, source, provenance):
    metadata = descriptor(source, provenance)
    validate_source_expectations(snapshot, metadata)
    plan = compile_processing_plan(source, processing_input(snapshot))
    validate_plan_binding(snapshot, plan)
    binding_for(snapshot, "acquisition.source")["parameters"].pop("sample_rate")
    validate_source_expectations(snapshot, metadata)
    validate_plan_binding(snapshot, plan)


def test_source_binding_distinguishes_two_versions_of_same_acquisition_package(snapshot, source):
    v1_metadata = descriptor(source)
    validate_source_binding(snapshot, v1_metadata)
    binding_for(snapshot, "acquisition.source")["selection"]["version"] = "2.0.0"
    with pytest.raises(ValueError, match="different acquisition source"):
        validate_source_binding(snapshot, v1_metadata)
