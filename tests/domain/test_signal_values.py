"""Signal metadata fails early without changing sample ownership or snapshots."""
import numpy as np
import pytest

from omnibci.domain.signal import SignalChunk


@pytest.mark.parametrize("field", ["timestamp", "sequence_id"])
@pytest.mark.parametrize("value", [True, np.bool_(False), -1, 0.0, 1.5, np.nan, np.inf, "1", np.array(1)])
def test_chunk_requires_nonnegative_integer_metadata(field, value):
    fields = {"timestamp": 0, "sequence_id": 0, "raw_data": np.zeros((2, 3))}
    fields[field] = value

    with pytest.raises(ValueError, match=field):
        SignalChunk(**fields)


@pytest.mark.parametrize("data", [
    [[1, 2], [3, 4]],
    np.zeros((2, 3), dtype=bool),
    np.zeros((2, 3), dtype=complex),
    np.zeros((2, 3), dtype=object),
    np.full((2, 3), "1"),
])
def test_signal_values_require_real_numeric_arrays(data):
    with pytest.raises(TypeError, match="array|dtype"):
        SignalChunk(0, 0, data)


@pytest.mark.parametrize("shape", [(3,), (2, 2, 2)])
def test_signal_values_reject_non_matrix_shapes(shape):
    data = np.zeros(shape)
    with pytest.raises(ValueError, match="2-D"):
        SignalChunk(0, 0, data)


def test_chunk_requires_a_signal_row_and_an_event_row():
    with pytest.raises(ValueError, match="2 rows"):
        SignalChunk(0, 0, np.zeros((1, 3)))


@pytest.mark.parametrize("dtype", [np.int16, np.uint32, np.float32, np.float64])
def test_chunk_keeps_borrowed_storage_and_views(dtype):
    backing = np.arange(12, dtype=dtype).reshape(3, 4)
    raw = backing[:, ::2]
    raw.flags.writeable = False

    chunk = SignalChunk(1_700_000_000_000_000_001, 0, raw)

    assert chunk.timestamp == 1_700_000_000_000_000_001
    assert chunk.raw_data is raw
    assert np.shares_memory(chunk.data, backing)
    assert np.shares_memory(chunk.triggers, backing)
    assert not chunk.raw_data.flags.writeable


def test_chunk_allows_nan_samples_and_empty_internal_slices():
    raw = np.array([[np.nan, 1.0], [0.0, 7.0]])
    chunk = SignalChunk(0, 0, raw)
    empty = SignalChunk(0, 0, raw[:, :0])

    assert np.isnan(chunk.data[0, 0])
    assert empty.raw_data.shape == (2, 0)


def _layout():
    from omnibci.domain import signal_info

    return signal_info.SignalInfo(sample_rate=250, channels=(
        signal_info.ChannelDescriptor(channel_id="c3", index=0, name="C3"),
    ))


def test_layout_contains_only_rate_and_channels_and_derives_event_row():
    from dataclasses import fields, replace
    from omnibci.domain import signal_info

    info = _layout()
    assert {field.name for field in fields(info)} == {"sample_rate", "channels"}
    assert info.signal_channel_count == info.trigger_row_index == 1
    assert info.output_row_count == 2
    assert signal_info.SignalInfo.from_dict(info.to_dict()) == info
    mapped = replace(info, channels=info.channels + (
        signal_info.ChannelDescriptor(channel_id="c4", index=1, name="C4"),
    ))
    assert mapped.trigger_row_index == 2 and mapped.output_row_count == 3


def test_channel_identity_is_required_at_construction():
    from omnibci.domain import signal_info

    with pytest.raises(TypeError, match="channel_id"):
        signal_info.ChannelDescriptor(index=0, name="C3")


@pytest.mark.parametrize("field", ["source_id", "connection_id", "native_event", "provenance", "trigger_row_index"])
def test_layout_does_not_accept_host_or_derived_fields(field):
    from omnibci.domain import signal_info

    with pytest.raises(ValueError, match="Unknown"):
        signal_info.SignalInfo.from_dict({**_layout().to_dict(), field: "host-value"})


def test_observation_preserves_native_text_and_partial_layout_diagnostic():
    from omnibci.domain.ports import acquisition

    observation = acquisition.SourceObservation(
        native_text="  native\r\n", signal_info=None, native_event="annotations",
        provenance="file-reported", layout_error="Unsupported channel layout",
    )
    assert observation.native_text == "  native\r\n"
    assert observation.signal_info is None


@pytest.mark.parametrize("changes", [
    {"native_text": b"binary"}, {"signal_info": {}}, {"native_event": ""},
    {"provenance": "guessed"}, {"layout_error": "unexpected diagnostic"},
    {"signal_info": None}, {"signal_info": None, "layout_error": " "},
    {"signal_info": None, "layout_error": "x" * 4097},
])
def test_observation_rejects_inconsistent_or_invalid_values(changes):
    from omnibci.domain.ports import acquisition

    values = dict(native_text="native", signal_info=_layout(), native_event="final-row",
                  provenance="device-reported", layout_error=None)
    with pytest.raises(ValueError):
        acquisition.SourceObservation(**{**values, **changes})


def test_checkpoint_copies_nested_json_state():
    from omnibci.domain.ports import acquisition

    state = {"position": {"next_sample": 10}, "tags": ["file"]}
    checkpoint = acquisition.SourceCheckpoint(resource_id="file", resource_revision="rev", state=state)
    state["position"]["next_sample"] = 20
    state["tags"].append("changed")
    assert checkpoint.state == {"position": {"next_sample": 10}, "tags": ["file"]}


@pytest.mark.parametrize("changes", [
    {"resource_id": ""}, {"resource_revision": " "}, {"state": []},
    {"state": {"nan": float("nan")}}, {"state": {"bytes": b"data"}},
    {"state": {1: "non-string key"}}, {"state": {"tuple": (1, 2)}},
    {"state": {"large": "x" * 65537}},
])
def test_checkpoint_rejects_invalid_or_unbounded_state(changes):
    from omnibci.domain.ports import acquisition

    with pytest.raises(ValueError):
        acquisition.SourceCheckpoint(**{**dict(resource_id="file", resource_revision="rev", state={}), **changes})


def _boundary(**changes):
    from omnibci.domain.ports import algorithm

    return algorithm.EpochBoundary(**{"offset": 0, "kind": "start", "event_id": 7, **changes})


def test_start_and_end_have_only_position_kind_and_optional_start_event():
    start = _boundary()
    end = _boundary(kind="end", event_id=None)
    assert start.offset == end.offset == 0
    assert start.event_id == 7 and end.event_id is None
    assert set(start.__dataclass_fields__) == {"offset", "kind", "event_id"}


@pytest.mark.parametrize("changes", [
    {"offset": True}, {"offset": -1}, {"offset": 1.5}, {"kind": "unknown"},
    {"event_id": None}, {"event_id": 0}, {"event_id": -1},
    {"event_id": True}, {"event_id": 1.5}, {"kind": "end", "event_id": 7},
])
def test_invalid_boundary_values_fail(changes):
    with pytest.raises(ValueError):
        _boundary(**changes)
