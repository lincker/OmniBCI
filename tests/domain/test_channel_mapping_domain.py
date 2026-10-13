"""Channel mapping only selects, orders, renames, and declares unknown types."""
from dataclasses import FrozenInstanceError, replace
import json
import pickle

import numpy as np
import pytest

from omnibci.domain.acquisition_descriptor import AcquisitionDescriptor, ChannelDescriptor
from omnibci.domain.channel_mapping import (
    ChannelBinding,
    ProcessingInput,
    ResolvedSignalPlan,
    compile_processing_plan,
)
from omnibci.domain.decoder_input import DecoderChannelRequirements
from omnibci.domain.signal import SignalChunk


def source(names=("C3", "C4", "AUX"), **overrides):
    values = dict(source_id="device", sample_rate=500,
                  channels=tuple(ChannelDescriptor(i, name, unit="uV", channel_id=f"id-{name}") for i, name in enumerate(names)),
                  trigger_row_index=len(names), connection_id="connection", provenance="device-reported")
    values.update(overrides)
    return AcquisitionDescriptor(**values)


def test_mapping_preserves_values_trigger_dtype_and_source_metadata():
    observed = source()
    mapping = ProcessingInput(outputs=(ChannelBinding("Right", "id-C4", "eeg"),
                                       ChannelBinding("Left", "id-C3", "eeg")))
    plan = compile_processing_plan(observed, mapping, "revision")
    raw = np.array([[1, 2, 3], [4, 5, 6], [20, 30, 40], [0, 7, 0]], dtype=np.int16)
    before = raw.copy()
    chunk = SignalChunk(123, 17, raw)
    result = plan.apply(chunk)
    assert plan.source_indices == (1, 0)
    assert [c.name for c in plan.output_channels] == ["Right", "Left"]
    assert [c.signal_type for c in plan.output_channels] == ["eeg", "eeg"]
    assert [c.signal_type for c in observed.channels] == ["unknown"] * 3
    assert all(c.unit == "uV" for c in plan.output_channels)
    np.testing.assert_array_equal(result.raw_data, before[[1, 0, 3], :])
    np.testing.assert_array_equal(raw, before)
    assert result.raw_data.dtype == raw.dtype
    assert (result.timestamp, result.sequence_id) == (123, 17)
    assert not np.shares_memory(result.raw_data, raw)
    assert not plan.is_identity
    DecoderChannelRequirements(signal_type="eeg", min_count=2).validate_channels(plan.output_channels)


def test_mapping_tracks_channel_ids_after_source_reordering():
    mapping = ProcessingInput(outputs=(ChannelBinding("Right", "id-C4"), ChannelBinding("Left", "id-C3")))
    first = compile_processing_plan(source(), mapping)
    second = compile_processing_plan(source(("C4", "AUX", "C3")), mapping)
    assert first.source_indices == (1, 0)
    assert second.source_indices == (0, 2)
    assert first.output_channels == second.output_channels
    with pytest.raises(ValueError, match="C4"):
        compile_processing_plan(source(("C3",)), mapping)


@pytest.mark.parametrize("dtype", [np.float32, np.float64])
def test_identity_preserves_nan_infinity_and_trigger_bits(dtype):
    observed = source(("C3",))
    plan = compile_processing_plan(observed, ProcessingInput())
    raw = np.array([[np.nan, np.inf, -0.0], [0, 255, -1]], dtype=dtype)
    mapped = plan.apply(SignalChunk(1, 2, raw))
    assert plan.is_identity
    assert plan.output_channels == observed.channels
    assert mapped.raw_data.tobytes() == raw.tobytes()


def test_type_declaration_fills_unknown_without_overriding_reported_type():
    observed = source(("C3", "AUX"))
    observed = replace(observed, channels=(observed.channels[0], replace(observed.channels[1], signal_type="eog")))
    plan = compile_processing_plan(observed, ProcessingInput(outputs=(ChannelBinding("Eye", "id-AUX", "eog"),)))
    assert plan.output_channels[0].signal_type == "eog"
    assert plan.source == observed
    with pytest.raises(ValueError, match="conflicts.*AUX"):
        compile_processing_plan(observed, ProcessingInput(outputs=(ChannelBinding("Eye", "id-AUX", "eeg"),)))
    with pytest.raises(ValueError, match="incompatible channels"):
        DecoderChannelRequirements(signal_type="eeg").validate_channels(plan.output_channels)


@pytest.mark.parametrize("signal_type", ["trigger", "stim", "ground"])
def test_non_signal_rows_cannot_be_mapped_or_disguised(signal_type):
    observed = source(("event",))
    observed = replace(observed, channels=(replace(observed.channels[0], signal_type=signal_type),))
    for mapping in (ProcessingInput(), ProcessingInput(outputs=(ChannelBinding("EEG", "id-event", "eeg"),))):
        with pytest.raises(ValueError, match="cannot be mapped"):
            compile_processing_plan(observed, mapping)


@pytest.mark.parametrize("value", [
    {"schema_version": 1}, {"schema_version": True}, {"schema_version": 3},
    {"outputs": ["C3"]}, {"outputs": "C3"}, {"outputs": None},
    {"outputs": [{"name": "C3"}]}, {"outputs": [{"source_channel_id": "id-C3"}]},
    {"outputs": [{"source_channel_id": "id-C3", "name": " "}]},
    {"outputs": [{"source_channel_id": "id-C3", "name": "C3", "signal_type": "unknown"}]},
    {"outputs": [{"source_channel_id": "id-C3", "name": "C3", "signal_type": "stim"}]},
    {"outputs": [{"source_channel_id": "id-C3", "name": "C3", "signal_type": []}]},
    {"outputs": [{"source_channel_id": "id-C3", "name": "C3", "unit": "V"}]},
    {"outputs": [{"source_channel_id": "id-C3", "name": "Left"}, {"source_channel_id": "id-C3", "name": "Right"}]},
    {"outputs": [{"source_channel_id": "id-C3", "name": "EEG"}, {"source_channel_id": "id-C4", "name": "EEG"}]},
    {"reference": {"mode": "original"}}, {"excluded_channels": []},
    {"template": "standard_1020"}, {"coefficients": [[1]]},
    {"outputs": [{"name": "bipolar", "positive": "C3", "negative": "C4"}]},
])
def test_processing_input_rejects_legacy_transforms_and_invalid_bindings(value):
    with pytest.raises(ValueError):
        ProcessingInput.from_dict(value)


def test_mapping_and_plan_round_trip_and_freeze():
    mapping = ProcessingInput.from_dict({"schema_version": 2, "outputs": [
        {"name": "Left", "source_channel_id": "id-C3", "signal_type": "eeg"},
    ]})
    assert ProcessingInput.from_dict(mapping.to_dict()) == mapping
    with pytest.raises(FrozenInstanceError):
        mapping.outputs = ()
    plan = compile_processing_plan(source(), mapping, "revision")
    assert ResolvedSignalPlan.from_dict(json.loads(json.dumps(plan.to_dict()))) == plan
    assert pickle.loads(pickle.dumps(plan)) == plan
    assert AcquisitionDescriptor.from_dict(json.loads(json.dumps(plan.source.to_dict()))) == plan.source


@pytest.mark.parametrize("field,value", [
    ("source_indices", [1]), ("coefficients", [[1, 0, 0]]), ("setup", {}),
    ("output_channels", [{"index": 0, "name": "Wrong", "signal_type": "eeg"}]),
])
def test_plan_deserialization_rejects_tampering_without_fingerprint(field, value):
    plan = compile_processing_plan(source(), ProcessingInput(outputs=(ChannelBinding("Left", "id-C3", "eeg"),)))
    payload = plan.to_dict(include_fingerprint=False)
    payload[field] = value
    with pytest.raises(ValueError):
        ResolvedSignalPlan.from_dict(payload)


def test_fingerprints_bind_semantics_without_connection_or_runtime_revision():
    observed = source()
    mapping = ProcessingInput(outputs=(ChannelBinding("Left", "id-C3", "eeg"),))
    plan = compile_processing_plan(observed, mapping, "revision")
    reconnected = compile_processing_plan(replace(observed, connection_id="new"), mapping, "new-revision")
    assert plan.fingerprint == reconnected.fingerprint
    assert plan.source.fingerprint == reconnected.source.fingerprint
    assert plan.fingerprint != compile_processing_plan(observed, ProcessingInput()).fingerprint
    changed = plan.to_dict()
    changed["fingerprint"] = "wrong"
    with pytest.raises(ValueError, match="fingerprint"):
        ResolvedSignalPlan.from_dict(changed)


def test_invalid_source_dimensions_and_changed_chunk_rows_are_rejected():
    observed = source()
    for key, value in (("sample_rate", True), ("sample_rate", 0), ("trigger_row_index", 2),
                       ("output_row_count", 3), ("signal_channel_count", 4)):
        data = observed.to_dict()
        data[key] = value
        with pytest.raises(ValueError):
            AcquisitionDescriptor.from_dict(data)
    with pytest.raises(ValueError, match="Signal rows changed"):
        compile_processing_plan(observed, ProcessingInput()).apply(SignalChunk(1, 2, np.zeros((3, 4))))


def test_generated_names_stay_marked_until_renamed():
    observed = source(("CH1",))
    observed = replace(observed, channels=(replace(observed.channels[0], name_provenance="generated"),))
    same = compile_processing_plan(observed, ProcessingInput(outputs=(ChannelBinding("CH1", "id-CH1"),)))
    renamed = compile_processing_plan(observed, ProcessingInput(outputs=(ChannelBinding("Left", "id-CH1"),)))
    assert same.output_channels[0].name_provenance == "generated"
    assert renamed.output_channels[0].name_provenance is None


def test_channel_ids_identify_duplicate_or_changed_display_names():
    observed = source(channels=(ChannelDescriptor(0, "EEG", channel_id="left"),
                                ChannelDescriptor(1, "EEG", channel_id="right")), trigger_row_index=2)
    assert compile_processing_plan(observed, ProcessingInput()).is_identity
    mapping = ProcessingInput(outputs=(ChannelBinding("Right", "right"),))
    plan = compile_processing_plan(observed, mapping)
    renamed = replace(observed, channels=(observed.channels[0], replace(observed.channels[1], name="Updated")))
    assert compile_processing_plan(renamed, mapping).source_indices == plan.source_indices == (1,)
    assert AcquisitionDescriptor.from_dict(observed.to_dict()) == observed
    with pytest.raises(ValueError, match="IDs must be unique"):
        replace(observed, channels=(observed.channels[0], replace(observed.channels[1], channel_id="left")))


def test_adapter_row_ids_are_deterministic_and_immutable():
    channel = ChannelDescriptor(3, "C3")
    assert channel.channel_id == "channel-3"
    with pytest.raises(FrozenInstanceError):
        channel.channel_id = "changed"


def test_canonical_channel_types_allow_matching_declarations():
    observed = source(channels=(ChannelDescriptor(0, "C3", signal_type=" EEG "),), trigger_row_index=1)
    plan = compile_processing_plan(observed, ProcessingInput(outputs=(ChannelBinding("Left", "channel-0", "eeg"),)))
    assert observed.channels[0].signal_type == plan.output_channels[0].signal_type == "eeg"
