"""Source observations and checkpoints keep host identity out of signal layout."""
from dataclasses import fields, replace

import pytest

from omnibci.domain import signal_info
from omnibci.domain.ports import acquisition


def layout():
    return signal_info.SignalInfo(sample_rate=250, channels=(
        signal_info.ChannelDescriptor(channel_id="c3", index=0, name="C3"),
    ))


def test_layout_contains_only_rate_and_channels_and_derives_event_row():
    info = layout()
    assert {field.name for field in fields(info)} == {"sample_rate", "channels"}
    assert info.signal_channel_count == info.trigger_row_index == 1
    assert info.output_row_count == 2
    assert signal_info.SignalInfo.from_dict(info.to_dict()) == info
    mapped = replace(info, channels=info.channels + (
        signal_info.ChannelDescriptor(channel_id="c4", index=1, name="C4"),
    ))
    assert mapped.trigger_row_index == 2 and mapped.output_row_count == 3


def test_channel_identity_is_required_at_construction():
    with pytest.raises(TypeError, match="channel_id"):
        signal_info.ChannelDescriptor(index=0, name="C3")


@pytest.mark.parametrize("field", ["source_id", "connection_id", "native_event", "provenance", "trigger_row_index"])
def test_layout_does_not_accept_host_or_derived_fields(field):
    with pytest.raises(ValueError, match="Unknown"):
        signal_info.SignalInfo.from_dict({**layout().to_dict(), field: "host-value"})


def test_observation_preserves_native_text_and_partial_layout_diagnostic():
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
    values = dict(native_text="native", signal_info=layout(), native_event="final-row",
                  provenance="device-reported", layout_error=None)
    with pytest.raises(ValueError):
        acquisition.SourceObservation(**{**values, **changes})


def test_checkpoint_copies_nested_json_state():
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
    with pytest.raises(ValueError):
        acquisition.SourceCheckpoint(**{**dict(resource_id="file", resource_revision="rev", state={}), **changes})
