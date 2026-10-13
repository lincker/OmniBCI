"""Source metadata stays raw; processing receives an optional typed description."""
from copy import deepcopy
from unittest.mock import AsyncMock, MagicMock

import pytest

from omnibci.application.events import CommandRequest
from omnibci.infrastructure.acquisition.acquisition import Acquisition, AcquisitionCommand
from omnibci.infrastructure.acquisition.metadata import acquisition_descriptor
from omnibci.shared.events import EventType


SETTINGS = {"ACQUISITION.signal_source": "test_source", "ACQUISITION.sample_rate": 1000,
            "ACQUISITION.channel_number": 10}
RAW = {"sample_rate": 250, "channel_count": 2, "output_channel_count": 3,
       "channel_labels": ["C3", "C4"], "provenance": "device-reported",
       "native_event": "separate-event-packets", "device": "Test amplifier",
       "protocol_version": 803, "capabilities": {"recording_control": False}}


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


def _worker():
    from omnibci.application.plugin_profiles import PluginSelection, ProfileBinding
    from omnibci.domain.channel_mapping import ProcessingInput
    source = MagicMock(is_connected=True, is_streaming=False)
    source.get_metadata = AsyncMock(return_value=deepcopy(RAW))
    source.start_stream = AsyncMock()
    bus = MagicMock()
    settings = {**SETTINGS, "PLUGINS.bindings": [ProfileBinding(
        "processing.decoder", "decoder", None, PluginSelection("test.decoder", "default", "1"),
        {}, processing_input=ProcessingInput(),
    ).to_dict()]}
    worker = Acquisition(source, MagicMock(), MagicMock(), event_bus=bus,
                         generation="generation", config_revision="revision", metadata_settings=settings)
    return worker, source, bus


def _result(bus):
    return [call.args[0].data for call in bus.put_event.call_args_list
            if call.args[0].type == EventType.CommandResult][-1]


@pytest.mark.asyncio
async def test_metadata_command_preserves_every_source_field_without_manual_overlays():
    worker, source, bus = _worker()
    worker._metadata_settings = {**SETTINGS, "ACQUISITION.manual_montage": {"invalid": True},
                                 "ACQUISITION.channel_names": ["Wrong", "Names"],
                                 "SIGNAL.setup": {"manual_metadata": {"invalid": True}}}
    await worker.execute((AcquisitionCommand.get_metadata, CommandRequest("metadata")))
    result = _result(bus)
    assert result.ok and result.command == "GetSourceMetadata"
    assert set(result.data) == {"source_id", "connection_id", "metadata", "descriptor", "descriptor_error"}
    assert result.data["source_id"] == "test_source"
    assert result.data["connection_id"] == worker.connection_id
    assert result.data["metadata"] == RAW
    assert result.data["descriptor_error"] is None
    assert result.data["descriptor"]["sample_rate"] == 250
    assert result.data["descriptor"]["signal_channel_count"] == 2
    assert [channel["name"] for channel in result.data["descriptor"]["channels"]] == ["C3", "C4"]
    assert "original_reference" not in result.data["descriptor"]


@pytest.mark.asyncio
@pytest.mark.parametrize("raw", [
    {"device": "Metadata-only device"}, {**RAW, "channel_labels": ["C3"]},
    {**RAW, "channel_types": {"C3": "eeg", "C4": "eeg"}},
])
async def test_unusable_processing_description_does_not_hide_or_reread_raw_metadata(raw):
    worker, source, bus = _worker()
    source.get_metadata.return_value = raw
    await worker.execute((AcquisitionCommand.get_metadata, CommandRequest("metadata")))
    result = _result(bus)
    assert result.ok and result.data["metadata"] == raw
    assert result.data["descriptor"] is None
    assert result.data["descriptor_error"]
    source.is_streaming = True
    await worker.execute((AcquisitionCommand.get_metadata, CommandRequest("cached")))
    assert _result(bus).ok and _result(bus).data["metadata"] == raw
    source.get_metadata.assert_awaited_once()
    await worker.execute((AcquisitionCommand.start_stream, CommandRequest("start", {})))
    assert _result(bus).error_code == "SIGNAL_NOT_PREPARED"


@pytest.mark.asyncio
async def test_metadata_cache_is_isolated_and_refresh_requires_stopped_source():
    worker, source, bus = _worker()
    await worker.execute((AcquisitionCommand.get_metadata, CommandRequest("metadata")))
    _result(bus).data["metadata"]["capabilities"]["recording_control"] = True
    source.get_metadata.return_value["channel_labels"][0] = "Changed"
    source.is_streaming = True
    await worker.execute((AcquisitionCommand.get_metadata, CommandRequest("cached")))
    assert _result(bus).data["metadata"] == RAW
    source.get_metadata.assert_awaited_once()
    await worker.execute((AcquisitionCommand.get_metadata, CommandRequest("refresh", {"refresh": True})))
    assert _result(bus).error_code == "METADATA_REQUIRES_STOP"
    source.get_metadata.assert_awaited_once()
    source.is_streaming = False
    await worker.execute((AcquisitionCommand.get_metadata, CommandRequest("refresh", {"refresh": True})))
    assert _result(bus).data["metadata"]["channel_labels"][0] == "Changed"
    assert source.get_metadata.await_count == 2


@pytest.mark.asyncio
async def test_failed_refresh_clears_cached_metadata_without_configuration_fallback():
    worker, source, bus = _worker()
    await worker.execute((AcquisitionCommand.get_metadata, CommandRequest("metadata")))
    source.get_metadata.side_effect = RuntimeError("Device has no metadata command")
    worker._metadata_settings["ACQUISITION.manual_montage"] = {"sample_rate": 250}
    await worker.execute((AcquisitionCommand.get_metadata, CommandRequest("refresh", {"refresh": True})))
    assert not _result(bus).ok
    assert "Device has no metadata command" in _result(bus).message
    assert worker._descriptor is None and worker._raw_metadata is None


@pytest.mark.asyncio
@pytest.mark.parametrize("raw", [None, "device", {"not_json": object()}, {"invalid": float("nan")}])
async def test_metadata_requires_a_json_object(raw):
    worker, source, bus = _worker()
    source.get_metadata.return_value = raw
    await worker.execute((AcquisitionCommand.get_metadata, CommandRequest("metadata")))
    assert not _result(bus).ok
    assert worker._descriptor is None and worker._raw_metadata is None


@pytest.mark.asyncio
async def test_prepared_start_rejects_missing_or_stale_receipt_before_starting_source():
    worker, source, bus = _worker()
    await worker.execute((AcquisitionCommand.get_metadata, CommandRequest("metadata")))
    receipt = {"generation": worker.generation, "config_revision": worker.config_revision,
               "connection_id": worker.connection_id, "source_fingerprint": worker._descriptor.fingerprint,
               "plan_fingerprint": "a" * 64}
    for payload in (None, {**receipt, "connection_id": "old"}, {**receipt, "config_revision": "old"},
                    {**receipt, "source_fingerprint": "old"}):
        await worker.execute((AcquisitionCommand.start_stream, CommandRequest("start", payload)))
        assert _result(bus).error_code == "SIGNAL_NOT_PREPARED"
    source.start_stream.assert_not_awaited()
    worker._validate_signal_receipt(receipt)


@pytest.mark.asyncio
async def test_historical_decoder_projection_blocks_stream_before_preparation():
    from omnibci.application.plugin_profiles import PluginSelection, ProfileBinding
    from omnibci.application.processing_source import materialize_processing_binding
    worker, source, bus = _worker()
    binding = ProfileBinding("processing.decoder", "decoder", None,
                             PluginSelection("test.decoder", "default", "1"), {})
    worker._metadata_settings = deepcopy(SETTINGS)
    worker._metadata_settings["PLUGINS.bindings"] = [materialize_processing_binding(
        binding, requires_channels=True)]
    await worker.execute((AcquisitionCommand.start_stream, CommandRequest("unprepared-start")))
    assert _result(bus).error_code == "SIGNAL_NOT_PREPARED"
    source.start_stream.assert_not_awaited()
    assert binding.processing_input is None


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
async def test_scan45_metadata_explicitly_reports_configured_stream_details_without_device_support():
    from omnibci.infrastructure.plugins.acquisition.neuroscan.scan45.scan45 import Scan45DataReader
    source = Scan45DataReader(parameters={"sample_rate": 250, "channel_number": 3})
    source.request_basic_info = AsyncMock(side_effect=AssertionError("must not read unimplemented header"))
    metadata = await source.get_metadata()
    assert metadata["metadata_supported"] is False
    assert metadata["metadata_origin"] == "configuration"
    assert metadata["output_channel_count"] == 3
    assert metadata["sample_rate"] == 250
    assert metadata["provenance"] == "user-declared"
    source.request_basic_info.assert_not_awaited()
