"""Signal Processing follows pinned decoder input and the selected acquisition source."""
from copy import deepcopy
from dataclasses import replace

import pytest

from omnibci.application.processing_source import (
    acquisition_binding_key,
    binding_for,
    processing_binding_key,
    processing_input,
    requires_signal_preparation,
    validate_plan_binding,
    validate_source_binding,
)
from omnibci.domain.montage import (
    ChannelDescriptor, ProcessingInput, ReferenceSpec, SignalSetup, SourceMetadata,
    compile_processing_plan, compile_signal_plan,
)


@pytest.fixture
def source():
    return SourceMetadata(
        "file_reader", 250,
        tuple(ChannelDescriptor(index, name, unit="uV") for index, name in enumerate(("C3", "C4", "M1"))),
        3, connection_id="connection-one", original_reference="Cz",
    )


@pytest.fixture
def snapshot():
    return {
        "ACQUISITION.signal_source": "file_reader", "ACQUISITION.sample_rate": 250,
        "ACQUISITION.channel_number": 4, "CONFIG.revision": "a" * 64,
        "CONFIG.generation": "generation-one",
        "PLUGINS.bindings": [
            {"slot": "acquisition.source", "kind": "acquisition", "profile_id": "00000000-0000-0000-0000-000000000001",
             "selection": {"package_id": "test.source", "contribution_id": "reader", "version": "1.0.0"},
             "parameters": {"sample_rate": 250, "channel_number": 4, "data_file_path": "one.cnt"},
             "profile_revision": 1, "configuration_version": 1},
            {"slot": "processing.decoder", "kind": "decoder", "profile_id": "00000000-0000-0000-0000-000000000002",
             "selection": {"package_id": "test.decoder", "contribution_id": "decoder", "version": "1.0.0"},
             "parameters": {"window_duration": 1.0}, "profile_revision": 1, "configuration_version": 1,
             "processing_input": ProcessingInput(outputs=("C3",), reference=ReferenceSpec("single", ("M1",))).to_dict()},
        ],
    }


@pytest.mark.parametrize("value", [{}, {"outputs": ["C4", "C3"]}])
def test_modern_input_takes_precedence_over_global_legacy_source(snapshot, value):
    binding_for(snapshot, "processing.decoder")["processing_input"] = value
    snapshot["SIGNAL.setup"] = {"source_id": "unrelated-device", "outputs": ["Legacy"]}
    assert processing_input(snapshot) == ProcessingInput.from_dict(value)
    assert requires_signal_preparation(snapshot)


def test_invalid_modern_input_cannot_fall_back_to_legacy_setup(snapshot):
    binding_for(snapshot, "processing.decoder")["processing_input"] = {"outputs": ["C3", "C3"]}
    snapshot["SIGNAL.setup"] = SignalSetup("file_reader").to_dict()
    with pytest.raises(ValueError, match="unique"):
        processing_input(snapshot)


@pytest.mark.parametrize("absent", [False, True])
def test_unconfigured_decoder_retains_legacy_input_and_no_input_stays_unprepared(snapshot, absent):
    decoder = binding_for(snapshot, "processing.decoder")
    if absent:
        decoder.pop("processing_input")
    else:
        decoder["processing_input"] = None
    assert processing_input(snapshot) is None
    assert not requires_signal_preparation(snapshot)
    legacy = SignalSetup("file_reader", outputs=("C4",))
    snapshot["SIGNAL.setup"] = legacy.to_dict()
    assert processing_input(snapshot) == legacy
    assert requires_signal_preparation(snapshot)


def test_processing_binding_survives_observation_revisions_and_decoder_parameter_changes(snapshot):
    before = processing_binding_key(snapshot)
    snapshot["CONFIG.revision"] = "b" * 64
    snapshot["CONFIG.generation"] = "generation-two"
    binding_for(snapshot, "acquisition.source")["profile_revision"] += 1
    decoder = binding_for(snapshot, "processing.decoder")
    decoder["profile_revision"] += 1
    decoder["parameters"]["window_duration"] = 2.5
    assert processing_binding_key(snapshot) == before


@pytest.mark.parametrize("field,value", [
    ("profile_id", "00000000-0000-0000-0000-000000000003"),
    ("parameters", {"sample_rate": 250, "channel_number": 4, "data_file_path": "two.cnt"}),
    ("selection", {"package_id": "test.source", "contribution_id": "reader", "version": "2.0.0"}),
])
def test_processing_binding_distinguishes_source_profiles_and_their_acquisition_inputs(snapshot, field, value):
    previous = deepcopy(snapshot)
    binding_for(snapshot, "acquisition.source")[field] = value
    assert processing_binding_key(snapshot) != processing_binding_key(previous)
    assert acquisition_binding_key(binding_for(snapshot, "acquisition.source")) != acquisition_binding_key(
        binding_for(previous, "acquisition.source"),
    )


def test_processing_binding_tracks_decoder_channel_selection(snapshot):
    previous = processing_binding_key(snapshot)
    binding_for(snapshot, "processing.decoder")["processing_input"] = ProcessingInput(outputs=("C4",)).to_dict()
    assert processing_binding_key(snapshot) != previous


def test_processing_binding_tracks_manual_acquisition_montage(snapshot, source):
    previous = processing_binding_key(snapshot)
    binding_for(snapshot, "acquisition.source")["manual_montage"] = source.to_dict()
    assert processing_binding_key(snapshot) != previous


def test_binding_key_does_not_share_mutable_parameters_with_snapshot(snapshot):
    before = processing_binding_key(snapshot)
    binding_for(snapshot, "acquisition.source")["parameters"]["data_file_path"] = "two.cnt"
    assert before["source"]["parameters"]["data_file_path"] == "one.cnt"
    assert processing_binding_key(snapshot) != before


@pytest.mark.parametrize("change,message", [
    ({"source_id": "another-device"}, "different acquisition source"),
    ({"sample_rate": 500}, "sample rate"),
    ({"channels": (ChannelDescriptor(0, "C3"),), "trigger_row_index": 1}, "channel count"),
])
def test_source_binding_rejects_metadata_from_a_different_acquisition_contract(snapshot, source, change, message):
    validate_source_binding(snapshot, source)
    with pytest.raises(ValueError, match=message):
        validate_source_binding(snapshot, replace(source, **change))


def test_plan_binding_checks_decoder_input_and_source_identity(snapshot, source):
    channel_input = processing_input(snapshot)
    plan = compile_processing_plan(source, channel_input)
    validate_plan_binding(snapshot, plan)
    stale_input = compile_processing_plan(source, ProcessingInput(outputs=("C4",)))
    with pytest.raises(ValueError, match="selected processing input"):
        validate_plan_binding(snapshot, stale_input)
    stale_source = compile_processing_plan(replace(source, source_id="another-device"), channel_input)
    with pytest.raises(ValueError, match="different acquisition source"):
        validate_plan_binding(snapshot, stale_source)
    legacy = compile_signal_plan(source, channel_input.as_signal_setup(source.source_id))
    assert legacy.coefficients == plan.coefficients
    with pytest.raises(ValueError, match="selected processing input"):
        validate_plan_binding(snapshot, legacy)


def test_legacy_plan_binding_preserves_its_representation(snapshot, source):
    channel_input = processing_input(snapshot)
    binding_for(snapshot, "processing.decoder").pop("processing_input")
    legacy = channel_input.as_signal_setup(source.source_id)
    snapshot["SIGNAL.setup"] = legacy.to_dict()
    validate_plan_binding(snapshot, compile_signal_plan(source, legacy))
    with pytest.raises(ValueError, match="selected processing input"):
        validate_plan_binding(snapshot, compile_processing_plan(source, channel_input))


def test_absent_bindings_are_supported_for_unconfigured_legacy_snapshots():
    assert binding_for({}, "acquisition.source") is None
    assert acquisition_binding_key(None) is None
    assert processing_input({}) is None
    assert not requires_signal_preparation({})
    legacy = SignalSetup("file_reader")
    assert processing_input({"SIGNAL.setup": legacy.to_dict()}) == legacy
    assert requires_signal_preparation({"SIGNAL.setup": legacy.to_dict()})


@pytest.mark.parametrize("requirements,legacy,expected", [
    (True, None, ProcessingInput()), (False, None, None),
    (True, {"source_id": "file_reader"}, None),
])
def test_historical_decoder_input_is_derived_only_when_required(requirements, legacy, expected):
    from omnibci.application.plugin_profiles import PluginSelection, ProfileBinding
    from omnibci.application.processing_source import materialize_processing_binding
    binding = ProfileBinding("processing.decoder", "decoder", None,
                             PluginSelection("test.decoder", "default", "1"), {})
    result = materialize_processing_binding(binding, requires_channels=requirements, legacy_setup=legacy)
    assert result.get("processing_input") == (expected.to_dict() if expected is not None else None)
    assert binding.processing_input is None
    assert "processing_input" not in binding.to_dict()
