"""Source metadata stays opaque text; processing receives a typed description."""
from copy import deepcopy
from unittest.mock import AsyncMock, MagicMock

import pytest

from omnibci.application.events import CommandRequest
from omnibci.infrastructure.acquisition.acquisition import AcquisitionRuntime, AcquisitionCommand
from omnibci.infrastructure.acquisition.metadata import acquisition_descriptor
from omnibci.shared.events import EventType


RAW = {"sample_rate": 250, "channel_count": 2, "output_channel_count": 3,
       "channel_labels": ["C3", "C4"], "provenance": "device-reported",
       "native_event": "separate-event-packets", "device": "Test amplifier",
       "protocol_version": 803, "capabilities": {"recording_control": False}}
TEXT = "  Test amplifier\r\nChannels: C3, C4\n<vendor>原始信息</vendor>\n"


def test_processing_description_uses_only_source_fields():
    raw = {**RAW, "channel_types": ["eeg", "eog"], "channel_units": ["V", "uV"],
           "original_reference": "Cz"}
    metadata = acquisition_descriptor(raw, source_id="test_source", connection_id="connected")
    assert metadata.sample_rate == 250
    assert metadata.signal_channel_count == 2
    assert metadata.output_row_count == 3
    assert metadata.trigger_row_index == 2
    assert [channel.channel_id for channel in metadata.channels] == ["channel-0", "channel-1"]
    assert [channel.name for channel in metadata.channels] == ["C3", "C4"]
    assert [channel.signal_type for channel in metadata.channels] == ["eeg", "eog"]
    assert [channel.unit for channel in metadata.channels] == ["V", "uV"]
    assert "original_reference" not in metadata.to_dict()
    assert raw["original_reference"] == "Cz"
    assert metadata.provenance == "device-reported"


def test_missing_optional_channel_details_are_internal_placeholders_only():
    raw = {"sample_rate": 250, "output_channel_count": 3, "provenance": "user-declared"}
    original = deepcopy(raw)
    metadata = acquisition_descriptor(raw, source_id="test_source", connection_id="connected")
    assert [channel.name for channel in metadata.channels] == ["CH1", "CH2"]
    assert all(channel.name_provenance == "generated" for channel in metadata.channels)
    assert all(channel.unit is None and channel.signal_type == "unknown" for channel in metadata.channels)
    assert raw == original


def test_acquisition_preserves_source_channel_ids_independently_of_duplicate_labels():
    raw = {**RAW, "channel_labels": ["EEG", "EEG"], "channel_ids": ["left", "right"],
           "channel_positions": [{"unsupported": "raw-only"}]}
    descriptor = acquisition_descriptor(raw, source_id="test_source", connection_id="connected")
    assert [channel.channel_id for channel in descriptor.channels] == ["left", "right"]
    assert [channel.name for channel in descriptor.channels] == ["EEG", "EEG"]
    assert "channel_positions" not in descriptor.to_dict()


def test_descriptor_normalizes_type_spelling_without_editing_source_metadata():
    raw = {**RAW, "channel_types": [" EEG ", "UNKNOWN"]}
    descriptor = acquisition_descriptor(raw, source_id="test_source", connection_id="connected")
    assert [channel.signal_type for channel in descriptor.channels] == ["eeg", "unknown"]
    assert raw["channel_types"] == [" EEG ", "UNKNOWN"]


@pytest.mark.parametrize("raw, message", [
    ({}, "positive integer sample rate"),
    ({**RAW, "sample_rate": 249.5}, "positive integer"),
    ({**RAW, "sample_rate": float("inf")}, "positive integer"),
    ({**RAW, "output_channel_count": 1}, "signal row"),
    ({**RAW, "channel_count": 3}, "signal channel count"),
    ({**RAW, "channel_labels": ["C3"]}, "exactly 2"),
    ({**RAW, "channel_labels": "C3"}, "must be a sequence"),
    ({**RAW, "channel_units": ["V"]}, "signal row count"),
    ({**RAW, "channel_ids": ["one"]}, "signal row count"),
    ({**RAW, "channel_ids": ["one", "one"]}, "IDs must be unique"),
    ({**RAW, "channel_ids": ["", "two"]}, "nonempty strings"),
])
def test_invalid_processing_description_is_rejected(raw, message):
    with pytest.raises(ValueError, match=message):
        acquisition_descriptor(raw, source_id="test_source", connection_id="connected")


def _worker(*, preparation_required=True):
    source = MagicMock(is_connected=True, is_streaming=False, plugin_type="plugin-implementation")
    source.get_metadata = AsyncMock(return_value=TEXT)
    source.describe_output = MagicMock(side_effect=lambda **identity: acquisition_descriptor(RAW, **identity))
    source.start_stream = AsyncMock()
    bus = MagicMock()
    worker = AcquisitionRuntime(source, MagicMock(), MagicMock(), event_bus=bus,
                                source_id="test_source", preparation_required=preparation_required,
                                generation="generation", config_revision="revision")
    return worker, source, bus


def _result(bus):
    return [call.args[0].data for call in bus.put_event.call_args_list
            if call.args[0].type == EventType.CommandResult][-1]


@pytest.mark.asyncio
async def test_metadata_command_preserves_source_text_and_uses_explicit_source_identity():
    worker, source, bus = _worker()
    await worker.execute((AcquisitionCommand.get_metadata, CommandRequest("metadata")))
    result = _result(bus)
    assert result.ok and result.command == "GetSourceMetadata"
    assert set(result.data) == {"source_id", "connection_id", "metadata", "descriptor", "descriptor_error"}
    assert result.data["source_id"] == "test_source"
    assert result.data["connection_id"] == worker.connection_id
    assert result.data["metadata"] == TEXT
    assert result.data["descriptor_error"] is None
    assert result.data["descriptor"]["sample_rate"] == 250
    assert result.data["descriptor"]["signal_channel_count"] == 2
    assert [channel["name"] for channel in result.data["descriptor"]["channels"]] == ["C3", "C4"]
    assert "original_reference" not in result.data["descriptor"]
    source.describe_output.assert_called_once_with(source_id="test_source", connection_id=worker.connection_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [
    NotImplementedError("Output description is unavailable"),
    ValueError("Source channel labels do not match the output"),
])
async def test_unusable_processing_description_does_not_hide_or_reread_metadata_text(error):
    worker, source, bus = _worker()
    source.describe_output.side_effect = error
    await worker.execute((AcquisitionCommand.get_metadata, CommandRequest("metadata")))
    result = _result(bus)
    assert result.ok and result.data["metadata"] == TEXT
    assert result.data["descriptor"] is None
    assert result.data["descriptor_error"]
    source.is_streaming = True
    await worker.execute((AcquisitionCommand.get_metadata, CommandRequest("cached")))
    assert _result(bus).ok and _result(bus).data["metadata"] == TEXT
    source.get_metadata.assert_awaited_once()
    await worker.execute((AcquisitionCommand.start_stream, CommandRequest("start", {})))
    assert _result(bus).error_code == "SIGNAL_NOT_PREPARED"


@pytest.mark.asyncio
async def test_metadata_cache_is_isolated_and_refresh_requires_stopped_source():
    worker, source, bus = _worker()
    await worker.execute((AcquisitionCommand.get_metadata, CommandRequest("metadata")))
    _result(bus).data["metadata"] = "Caller changed the response"
    _result(bus).data["descriptor"]["channels"][0]["name"] = "Changed"
    source.get_metadata.return_value = "New source information\n"
    source.is_streaming = True
    await worker.execute((AcquisitionCommand.get_metadata, CommandRequest("cached")))
    assert _result(bus).data["metadata"] == TEXT
    assert _result(bus).data["descriptor"]["channels"][0]["name"] == "C3"
    source.get_metadata.assert_awaited_once()
    await worker.execute((AcquisitionCommand.get_metadata, CommandRequest("refresh", {"refresh": True})))
    assert _result(bus).error_code == "METADATA_REQUIRES_STOP"
    source.get_metadata.assert_awaited_once()
    source.is_streaming = False
    await worker.execute((AcquisitionCommand.get_metadata, CommandRequest("refresh", {"refresh": True})))
    assert _result(bus).data["metadata"] == "New source information\n"
    assert source.get_metadata.await_count == 2


@pytest.mark.asyncio
async def test_failed_refresh_clears_cached_metadata_without_configuration_fallback():
    worker, source, bus = _worker()
    await worker.execute((AcquisitionCommand.get_metadata, CommandRequest("metadata")))
    source.get_metadata.side_effect = RuntimeError("Device has no metadata command")
    await worker.execute((AcquisitionCommand.get_metadata, CommandRequest("refresh", {"refresh": True})))
    assert not _result(bus).ok
    assert "Device has no metadata command" in _result(bus).message
    assert worker._descriptor is None and worker._raw_metadata is None


@pytest.mark.asyncio
@pytest.mark.parametrize("raw", [None, b"device", {"device": "Amplifier"}, [], 42])
async def test_metadata_requires_a_string(raw):
    worker, source, bus = _worker()
    source.get_metadata.return_value = raw
    await worker.execute((AcquisitionCommand.get_metadata, CommandRequest("metadata")))
    assert not _result(bus).ok
    assert worker._descriptor is None and worker._raw_metadata is None
    source.describe_output.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["", "not JSON: sample_rate=9999\r\nchannel_count=1000\n", TEXT])
async def test_display_text_does_not_determine_the_output_descriptor(text):
    worker, source, bus = _worker()
    source.get_metadata.return_value = text
    await worker.execute((AcquisitionCommand.get_metadata, CommandRequest("metadata")))
    assert _result(bus).data["metadata"] == text
    assert worker._descriptor.sample_rate == 250
    assert worker._descriptor.signal_channel_count == 2
    source.is_streaming = True
    await worker.execute((AcquisitionCommand.get_metadata, CommandRequest("cached")))
    assert _result(bus).data["metadata"] == text
    source.get_metadata.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("descriptor", [
    RAW,
    acquisition_descriptor(RAW, source_id="other-source", connection_id="stale"),
])
async def test_invalid_or_stale_output_descriptor_is_not_used(descriptor):
    worker, source, bus = _worker()
    source.describe_output.side_effect = None
    source.describe_output.return_value = descriptor
    await worker.execute((AcquisitionCommand.get_metadata, CommandRequest("metadata")))
    result = _result(bus)
    assert result.ok and result.data["metadata"] == TEXT
    assert result.data["descriptor"] is None
    assert result.data["descriptor_error"]
    assert worker._descriptor is None


@pytest.mark.asyncio
async def test_prepared_start_rejects_missing_or_stale_receipt_before_starting_source():
    worker, source, bus = _worker()
    await worker.execute((AcquisitionCommand.get_metadata, CommandRequest("metadata")))
    receipt = {"generation": worker.generation, "config_revision": worker.config_revision,
               "connection_id": worker.connection_id, "source_fingerprint": worker._descriptor.fingerprint,
               "plan_fingerprint": "a" * 64}
    for payload in (None, {**receipt, "connection_id": "old"}, {**receipt, "config_revision": "old"},
                    {**receipt, "source_fingerprint": "old"}, {**receipt, "generation": "old"}):
        await worker.execute((AcquisitionCommand.start_stream, CommandRequest("start", payload)))
        assert _result(bus).error_code == "SIGNAL_NOT_PREPARED"
    source.start_stream.assert_not_awaited()
    worker._validate_signal_receipt(receipt)


@pytest.mark.asyncio
@pytest.mark.parametrize("preparation_required", [False, True])
async def test_explicit_preparation_requirement_controls_start(preparation_required):
    worker, source, bus = _worker(preparation_required=preparation_required)
    async def start():
        source.is_streaming = True
    source.start_stream.side_effect = start
    source.read_chunk = AsyncMock(return_value=None)
    source.stop_stream = AsyncMock()
    await worker.execute((AcquisitionCommand.start_stream, CommandRequest("start")))
    if preparation_required:
        assert _result(bus).error_code == "SIGNAL_NOT_PREPARED"
        source.start_stream.assert_not_awaited()
    else:
        assert _result(bus).ok
        source.start_stream.assert_awaited_once()
        await worker.fetch_task


@pytest.mark.asyncio
async def test_prepared_start_accepts_receipt_after_revision_change_and_reconnection():
    worker, source, bus = _worker()
    await worker.execute((AcquisitionCommand.get_metadata, CommandRequest("metadata")))
    old_receipt = {"generation": worker.generation, "config_revision": worker.config_revision,
                   "connection_id": worker.connection_id, "source_fingerprint": worker._descriptor.fingerprint,
                   "plan_fingerprint": "a" * 64}
    await worker.execute((AcquisitionCommand.update_revision, CommandRequest("revision", {
        "generation": worker.generation, "expected_revision": worker.config_revision, "target_revision": "b" * 64,
    })))
    assert _result(bus).ok
    await worker.execute((AcquisitionCommand.start_stream, CommandRequest("stale-revision", old_receipt)))
    assert _result(bus).error_code == "SIGNAL_NOT_PREPARED"
    source.start_stream.assert_not_awaited()

    async def disconnect():
        source.is_connected = False
    async def connect():
        source.is_connected = True
    source.disconnect = disconnect
    source.connect = connect
    await worker._on_disconnect()
    await worker._on_connect()
    await worker.execute((AcquisitionCommand.get_metadata, CommandRequest("reconnected-metadata")))
    stale_connection = {**old_receipt, "config_revision": worker.config_revision}
    await worker.execute((AcquisitionCommand.start_stream, CommandRequest("stale-connection", stale_connection)))
    assert _result(bus).error_code == "SIGNAL_NOT_PREPARED"
    source.start_stream.assert_not_awaited()

    async def start():
        source.is_streaming = True
    source.start_stream.side_effect = start
    source.read_chunk = AsyncMock(return_value=None)
    source.stop_stream = AsyncMock()
    receipt = {**stale_connection, "connection_id": worker.connection_id,
               "source_fingerprint": worker._descriptor.fingerprint}
    await worker.execute((AcquisitionCommand.start_stream, CommandRequest("prepared-start", receipt)))
    assert _result(bus).ok
    source.start_stream.assert_awaited_once()
    await worker.fetch_task


@pytest.mark.asyncio
async def test_disconnect_clears_metadata_and_reconnect_invalidates_old_receipt():
    worker, source, bus = _worker()
    await worker.execute((AcquisitionCommand.get_metadata, CommandRequest("metadata")))
    connection_id = worker.connection_id
    async def disconnect():
        source.is_connected = False
    async def connect():
        source.is_connected = True
    source.disconnect = disconnect
    source.connect = connect
    await worker._on_disconnect()
    assert worker.connection_id is None and worker._descriptor is None and worker._raw_metadata is None
    await worker._on_connect()
    assert worker.connection_id != connection_id


@pytest.mark.asyncio
async def test_scan45_metadata_reports_unsupported_query_separately_from_configured_output():
    from omnibci.infrastructure.plugins.acquisition.neuroscan.scan45.scan45 import Scan45DataReader
    source = Scan45DataReader(parameters={"sample_rate": 250, "channel_number": 3})
    source.request_basic_info = AsyncMock(side_effect=AssertionError("must not read unimplemented header"))
    metadata = await source.get_metadata()
    assert isinstance(metadata, str)
    assert "not implemented" in metadata.lower()
    descriptor = source.describe_output(source_id="scan45", connection_id="connected")
    assert descriptor.output_row_count == 3
    assert descriptor.signal_channel_count == 2
    assert descriptor.sample_rate == 250
    assert descriptor.provenance == "user-declared"
    source.request_basic_info.assert_not_awaited()
