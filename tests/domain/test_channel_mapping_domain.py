"""Channel mapping only selects, orders, renames, and declares unknown types."""
from dataclasses import FrozenInstanceError, replace
import json
import pickle

import numpy as np
import pytest

from omnibci.domain.signal_info import SignalInfo, ChannelDescriptor
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
    return SignalInfo(**values)


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


def test_manual_rows_resolve_custom_channel_ids_and_preserve_the_event_row():
    mapping = ProcessingInput.from_dict({"schema_version": 2, "outputs": [
        {"name": "Right", "source_channel_index": 1, "signal_type": "eeg"},
        {"name": "Left", "source_channel_index": 0, "signal_type": "eeg"},
    ]})
    observed = source()
    plan = compile_processing_plan(observed, mapping)
    assert plan.source_indices == (1, 0)
    assert [channel.channel_id for channel in plan.output_channels] == ["id-C4", "id-C3"]
    assert [channel.name for channel in plan.output_channels] == ["Right", "Left"]
    raw = np.array([[1, 2, 3], [4, 5, 6], [20, 30, 40], [0, 7, 255]])
    chunk = plan.apply(SignalChunk(123, 17, raw))
    np.testing.assert_array_equal(chunk.raw_data, raw[[1, 0, 3], :])
    assert ResolvedSignalPlan.from_dict(plan.to_dict()) == plan
    assert ProcessingInput.from_dict(mapping.to_dict()) == mapping
    assert all("source_channel_id" not in row for row in mapping.to_dict()["outputs"])


@pytest.mark.parametrize("index", [3, 4, 100])
def test_manual_rows_reject_the_event_row_and_missing_rows(index):
    mapping = ProcessingInput(outputs=(ChannelBinding("Manual", source_channel_index=index),))
    with pytest.raises(ValueError, match="missing or is the trigger row"):
        compile_processing_plan(source(), mapping)


def test_manual_rows_validate_types_and_duplicates_against_live_channels():
    observed = source()
    observed = replace(observed, channels=(*observed.channels[:2], replace(observed.channels[2], signal_type="eog")))
    with pytest.raises(ValueError, match="conflicts.*id-AUX"):
        compile_processing_plan(observed, ProcessingInput(outputs=(
            ChannelBinding("Eye", signal_type="eeg", source_channel_index=2),
        )))
    with pytest.raises(ValueError, match="Source indices must be unique"):
        compile_processing_plan(observed, ProcessingInput(outputs=(
            ChannelBinding("First", "id-C3"), ChannelBinding("Repeated", source_channel_index=0),
        )))
    with pytest.raises(ValueError, match="cannot be mapped more than once"):
        ProcessingInput(outputs=(ChannelBinding("First", source_channel_index=0),
                                 ChannelBinding("Repeated", source_channel_index=0)))


@pytest.mark.parametrize("reference", [
    {}, {"source_channel_id": None}, {"source_channel_index": None},
    {"source_channel_id": "id-C3", "source_channel_index": 0},
    {"source_channel_index": -1}, {"source_channel_index": True},
    {"source_channel_index": 1.0}, {"source_channel_index": "1"},
])
def test_manual_rows_require_exactly_one_valid_source_reference(reference):
    with pytest.raises(ValueError):
        ChannelBinding.from_dict({"name": "Input", **reference})


@pytest.mark.parametrize("channel_ids, requires_data_mapping", [
    ((), False),
    (("id-C3", "id-C4", "id-AUX"), False),
    (("id-C4", "id-C3", "id-AUX"), True),
    (("id-C3", "id-C4"), True),
])
def test_data_mapping_requirement_depends_only_on_complete_physical_order(channel_ids, requires_data_mapping):
    mapping = ProcessingInput(outputs=tuple(
        ChannelBinding(f"Input {index}", channel_id, "eeg")
        for index, channel_id in enumerate(channel_ids)
    ))
    plan = compile_processing_plan(source(), mapping, "revision")
    assert plan.requires_data_mapping is requires_data_mapping
    assert plan.is_identity is (not channel_ids)
    restored = ResolvedSignalPlan.from_dict(json.loads(json.dumps(plan.to_dict())))
    assert restored.requires_data_mapping is requires_data_mapping
    assert restored.fingerprint == plan.fingerprint
    assert replace(plan, config_revision="new-revision").requires_data_mapping is requires_data_mapping
    with pytest.raises(FrozenInstanceError):
        plan.requires_data_mapping = not requires_data_mapping


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
    for mapping in (ProcessingInput(), ProcessingInput(outputs=(ChannelBinding("EEG", "id-event", "eeg"),)),
                    ProcessingInput(outputs=(ChannelBinding("EEG", signal_type="eeg", source_channel_index=0),))):
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
    assert SignalInfo.from_dict(json.loads(json.dumps(plan.source.to_dict()))) == plan.source


@pytest.mark.parametrize("field,value", [
    ("source_indices", [1]), ("coefficients", [[1, 0, 0]]), ("setup", {}),
    ("requires_data_mapping", False),
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


def test_invalid_source_dimensions_are_rejected():
    observed = source()
    for key, value in (("sample_rate", True), ("sample_rate", 0), ("trigger_row_index", 2),
                       ("output_row_count", 3), ("signal_channel_count", 4)):
        data = observed.to_dict()
        data[key] = value
        with pytest.raises(ValueError):
            SignalInfo.from_dict(data)


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
    assert SignalInfo.from_dict(observed.to_dict()) == observed
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
