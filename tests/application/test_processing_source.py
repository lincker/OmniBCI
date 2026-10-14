"""Processing follows pinned mappings and the connected acquisition descriptor."""
from copy import deepcopy
from dataclasses import replace

import pytest

from omnibci.application.processing_source import (
    acquisition_binding_key, binding_for, materialize_processing_binding, processing_binding_key,
    processing_input, requires_signal_preparation, validate_plan_binding, validate_source_binding,
)
from omnibci.domain.signal_info import SignalInfo, ChannelDescriptor
from omnibci.domain.channel_mapping import ChannelBinding, ProcessingInput, compile_processing_plan


@pytest.fixture
def source():
    return SignalInfo("file_reader", 250, tuple(
        ChannelDescriptor(index, name, unit="uV") for index, name in enumerate(("C3", "C4", "M1"))),
        3, connection_id="connection-one")


@pytest.fixture
def snapshot():
    return {
        "ACQUISITION.signal_source": "file_reader", "ACQUISITION.sample_rate": 250,
        "ACQUISITION.channel_number": 4, "CONFIG.revision": "a" * 64,
        "CONFIG.generation": "generation-one", "PLUGINS.bindings": [
            {"slot": "acquisition.source", "kind": "acquisition", "profile_id": "source-profile",
             "selection": {"package_id": "test.source", "contribution_id": "reader", "version": "1.0.0"},
             "parameters": {"sample_rate": 250, "channel_number": 4, "data_file_path": "one.cnt"},
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


@pytest.mark.parametrize("value", [None, {"schema_version": 1, "outputs": ["C3"]}])
def test_retired_input_cannot_execute_or_fall_back(snapshot, value):
    binding_for(snapshot, "processing.decoder")["processing_input"] = value
    snapshot["SIGNAL.setup"] = {"source_id": "file_reader"}
    with pytest.raises(ValueError):
        processing_input(snapshot)
    with pytest.raises(ValueError):
        requires_signal_preparation(snapshot)


def test_processing_binding_survives_profile_revision_and_decoder_parameter_changes(snapshot):
    before = processing_binding_key(snapshot)
    snapshot["CONFIG.revision"] = "b" * 64
    snapshot["CONFIG.generation"] = "generation-two"
    binding_for(snapshot, "acquisition.source")["profile_revision"] += 1
    decoder = binding_for(snapshot, "processing.decoder")
    decoder["profile_revision"] += 1
    decoder["parameters"]["window_duration"] = 2.5
    assert processing_binding_key(snapshot) == before


@pytest.mark.parametrize("field,value", [
    ("profile_id", "other-source"),
    ("parameters", {"sample_rate": 250, "channel_number": 4, "data_file_path": "two.cnt"}),
    ("selection", {"package_id": "test.source", "contribution_id": "reader", "version": "2.0.0"}),
])
def test_processing_binding_distinguishes_source_ownership(snapshot, field, value):
    previous = deepcopy(snapshot)
    binding_for(snapshot, "acquisition.source")[field] = value
    assert processing_binding_key(snapshot) != processing_binding_key(previous)
    assert acquisition_binding_key(binding_for(snapshot, "acquisition.source")) != acquisition_binding_key(
        binding_for(previous, "acquisition.source"))


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
    ({"channels": (ChannelDescriptor(0, "C3"),), "trigger_row_index": 1}, "channel count"),
])
def test_source_binding_rejects_descriptor_from_a_different_contract(snapshot, source, change, message):
    validate_source_binding(snapshot, source)
    with pytest.raises(ValueError, match=message):
        validate_source_binding(snapshot, replace(source, **change))


@pytest.mark.parametrize("provenance", ["device-reported", "file-reported"])
def test_reported_dimensions_override_configured_defaults(snapshot, source, provenance):
    source = replace(source, sample_rate=1000, channels=source.channels[:2], trigger_row_index=2,
                     provenance=provenance)
    validate_plan_binding(snapshot, compile_processing_plan(source, processing_input(snapshot)))
    with pytest.raises(ValueError, match="different acquisition source"):
        validate_source_binding(snapshot, replace(source, source_id="another-device"))


def test_plan_binding_checks_mapping_and_source_identity(snapshot, source):
    value = processing_input(snapshot)
    validate_plan_binding(snapshot, compile_processing_plan(source, value))
    with pytest.raises(ValueError, match="selected processing input"):
        validate_plan_binding(snapshot, compile_processing_plan(source, ProcessingInput()))
    with pytest.raises(ValueError, match="different acquisition source"):
        validate_plan_binding(snapshot, compile_processing_plan(replace(source, source_id="different"), value))


def test_unconfigured_snapshots_do_not_prepare():
    assert binding_for({}, "acquisition.source") is None
    assert acquisition_binding_key(None) is None
    assert processing_input({}) is None
    assert not requires_signal_preparation({})


@pytest.mark.parametrize("requires_channels", [False, True])
def test_missing_input_is_identity_only_for_decoders_that_require_channels(requires_channels):
    from omnibci.application.plugin_profiles import PluginSelection, ProfileBinding
    binding = ProfileBinding("processing.decoder", "decoder", None,
                             PluginSelection("test.decoder", "default", "1"), {})
    result = materialize_processing_binding(binding, requires_channels=requires_channels)
    assert result.get("processing_input") == (ProcessingInput().to_dict() if requires_channels else None)
    assert binding.processing_input is None
