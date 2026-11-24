"""Source metadata stays opaque text; processing receives a typed description."""
from pathlib import Path
from copy import deepcopy
from dataclasses import replace
from omnibci.domain.ports.acquisition import SourceObservation
from omnibci.application.source_metadata import source_fingerprint
from threading import Event as ThreadEvent
from unittest.mock import AsyncMock, MagicMock

import pytest

from omnibci.application.events import CommandRequest, ProcessingInputStart
from omnibci.infrastructure.acquisition.acquisition import AcquisitionRuntime, AcquisitionCommand
from omnibci.infrastructure.acquisition.metadata import signal_info
from omnibci.shared.events import EventType


FIELDS = {"sample_rate": 250, "signal_channel_count": 2,
          "channel_labels": ["C3", "C4"]}
TEXT = "  Test amplifier\r\nChannels: C3, C4\n<vendor>原始信息</vendor>\n"


@pytest.mark.parametrize("sample_rate", [250, 250.0])
@pytest.mark.parametrize("sequence", [list, tuple])
def test_processing_description_preserves_explicit_channel_details(sample_rate, sequence):
    fields = {**FIELDS, "sample_rate": sample_rate,
              "channel_labels": sequence(["C3", "C4"]), "channel_types": sequence(["eeg", "eog"]),
              "channel_units": sequence(["V", "uV"]), "channel_ids": sequence(["left", "right"])}
    original = deepcopy(fields)
    metadata = signal_info(**fields)
    assert metadata.sample_rate == 250
    assert metadata.signal_channel_count == 2
    assert metadata.output_row_count == 3
    assert metadata.trigger_row_index == 2
    assert [channel.channel_id for channel in metadata.channels] == ["left", "right"]
    assert [channel.name for channel in metadata.channels] == ["C3", "C4"]
    assert [channel.signal_type for channel in metadata.channels] == ["eeg", "eog"]
    assert [channel.unit for channel in metadata.channels] == ["V", "uV"]
    assert fields == original


@pytest.mark.parametrize("optional", [{}, {"channel_labels": None, "channel_types": None, "channel_units": None,
                                        "channel_ids": None},
                                     {"channel_labels": [], "channel_types": [], "channel_units": []},
                                     {"channel_labels": (), "channel_types": (), "channel_units": ()}])
def test_missing_optional_channel_details_are_internal_placeholders_only(optional):
    fields = {"sample_rate": 250, "signal_channel_count": 2, **optional}
    original = deepcopy(fields)
    metadata = signal_info(**fields)
    assert [channel.name for channel in metadata.channels] == ["CH1", "CH2"]
    assert [channel.channel_id for channel in metadata.channels] == ["channel-0", "channel-1"]
    assert all(channel.name_provenance == "generated" for channel in metadata.channels)
    assert all(channel.unit is None and channel.signal_type == "unknown" for channel in metadata.channels)
    assert fields == original


def test_blank_channel_labels_use_generated_names_without_editing_input():
    labels = [None, "  ", ""]
    descriptor = signal_info(
        sample_rate=250,
        signal_channel_count=3, channel_labels=labels,
    )
    assert [channel.name for channel in descriptor.channels] == ["CH1", "CH2", "CH3"]
    assert all(channel.name_provenance == "generated" for channel in descriptor.channels)
    assert labels == [None, "  ", ""]


def test_acquisition_preserves_source_channel_ids_independently_of_duplicate_labels():
    fields = {**FIELDS, "channel_labels": ["EEG", "EEG"], "channel_ids": ["left", "right"]}
    original = deepcopy(fields)
    descriptor = signal_info(**fields)
    assert [channel.channel_id for channel in descriptor.channels] == ["left", "right"]
    assert [channel.name for channel in descriptor.channels] == ["EEG", "EEG"]
    assert fields == original


def test_descriptor_normalizes_type_spelling_without_editing_source_metadata():
    fields = {**FIELDS, "channel_types": [" EEG ", "UNKNOWN"]}
    descriptor = signal_info(**fields)
    assert [channel.signal_type for channel in descriptor.channels] == ["eeg", "unknown"]
    assert fields["channel_types"] == [" EEG ", "UNKNOWN"]


@pytest.mark.parametrize("missing", ["sample_rate", "signal_channel_count"])
def test_processing_description_requires_explicit_required_fields(missing):
    fields = dict(FIELDS)
    fields.pop(missing)
    with pytest.raises(TypeError, match=missing):
        signal_info(**fields)


@pytest.mark.parametrize("fields, message", [
    ({**FIELDS, "sample_rate": 249.5}, "positive integer"),
    ({**FIELDS, "sample_rate": float("inf")}, "positive integer"),
    ({**FIELDS, "signal_channel_count": 0}, "signal channel count"),
    ({**FIELDS, "channel_labels": ["C3"]}, "exactly 2"),
    ({**FIELDS, "channel_labels": "C3"}, "must be a sequence"),
    ({**FIELDS, "channel_units": ["V"]}, "signal row count"),
    ({**FIELDS, "channel_ids": []}, "signal row count"),
    ({**FIELDS, "channel_ids": ["one"]}, "signal row count"),
    ({**FIELDS, "channel_ids": ["one", "one"]}, "IDs must be unique"),
    ({**FIELDS, "channel_ids": ["", "two"]}, "nonempty strings"),
])
def test_invalid_processing_description_is_rejected(fields, message):
    with pytest.raises(ValueError, match=message):
        signal_info(**fields)


def _worker():
    source = MagicMock(trigger_mode="events", resume_mode="restart-only")
    source.observe = AsyncMock(return_value=SourceObservation(
        native_text=TEXT, signal_info=signal_info(**FIELDS),
        native_event="separate-event-packets", provenance="device-reported"))
    source.start_stream = AsyncMock()
    bus = MagicMock()
    worker = AcquisitionRuntime(source, MagicMock(), MagicMock(), event_bus=bus,
                                source_id="test_source", processing_active=ThreadEvent(),
                                generation="generation", acquisition_revision="revision", engine_revision="engine")
    worker._connected = True
    worker.connection_id = "connection-one"
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
    source.observe.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_repeated_connect_preserves_connection_metadata():
    worker, source, bus = _worker()
    await worker.execute((AcquisitionCommand.get_metadata, CommandRequest("metadata")))
    metadata = _result(bus).data
    await worker.execute((AcquisitionCommand.connect, CommandRequest("connect")))
    assert _result(bus).ok
    await worker.execute((AcquisitionCommand.get_metadata, CommandRequest("cached")))
    assert _result(bus).data == metadata
    assert worker.connection_id == metadata["connection_id"]
    source.connect.assert_not_called()
    source.observe.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [
    NotImplementedError("Output description is unavailable"),
    ValueError("Source channel labels do not match the output"),
])
async def test_unusable_processing_description_does_not_hide_or_reread_metadata_text(error):
    worker, source, bus = _worker()
    source.observe.return_value = replace(source.observe.return_value, signal_info=None, layout_error=str(error))
    await worker.execute((AcquisitionCommand.get_metadata, CommandRequest("metadata")))
    result = _result(bus)
    assert result.ok and result.data["metadata"] == TEXT
    assert result.data["descriptor"] is None
    assert result.data["descriptor_error"]
    worker._streaming = True
    await worker.execute((AcquisitionCommand.get_metadata, CommandRequest("cached")))
    assert _result(bus).ok and _result(bus).data["metadata"] == TEXT
    source.observe.assert_awaited_once()
    await worker.execute((AcquisitionCommand.start_stream, CommandRequest("start")))
    assert _result(bus).ok


@pytest.mark.asyncio
async def test_metadata_cache_is_isolated_and_refresh_requires_stopped_source():
    worker, source, bus = _worker()
    await worker.execute((AcquisitionCommand.get_metadata, CommandRequest("metadata")))
    _result(bus).data["metadata"] = "Caller changed the response"
    _result(bus).data["descriptor"]["channels"][0]["name"] = "Changed"
    source.observe.return_value = replace(source.observe.return_value, native_text="New source information\n")
    worker._streaming = True
    await worker.execute((AcquisitionCommand.get_metadata, CommandRequest("cached")))
    assert _result(bus).data["metadata"] == TEXT
    assert _result(bus).data["descriptor"]["channels"][0]["name"] == "C3"
    source.observe.assert_awaited_once()
    await worker.execute((AcquisitionCommand.get_metadata, CommandRequest("refresh", {"refresh": True})))
    assert _result(bus).error_code == "METADATA_REQUIRES_STOP"
    source.observe.assert_awaited_once()
    worker._streaming = False
    await worker.execute((AcquisitionCommand.get_metadata, CommandRequest("refresh", {"refresh": True})))
    assert _result(bus).data["metadata"] == "New source information\n"
    assert source.observe.await_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("descriptor_available", [True, False])
async def test_failed_refresh_clears_cached_metadata_without_configuration_fallback(descriptor_available):
    worker, source, bus = _worker()
    if not descriptor_available:
        source.observe.return_value = replace(source.observe.return_value, signal_info=None, layout_error="Unavailable")
    await worker.execute((AcquisitionCommand.get_metadata, CommandRequest("metadata")))
    source.observe.side_effect = RuntimeError("Device has no metadata command")
    await worker.execute((AcquisitionCommand.get_metadata, CommandRequest("refresh", {"refresh": True})))
    assert not _result(bus).ok
    assert "Device has no metadata command" in _result(bus).message
    assert worker._observation is None


@pytest.mark.asyncio
@pytest.mark.parametrize("raw", [None, b"device", {"device": "Amplifier"}, [], 42])
async def test_metadata_requires_a_string(raw):
    worker, source, bus = _worker()
    source.observe.return_value = raw
    await worker.execute((AcquisitionCommand.get_metadata, CommandRequest("metadata")))
    assert not _result(bus).ok
    assert worker._observation is None


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["", "not JSON: sample_rate=9999\r\nchannel_count=1000\n", TEXT])
async def test_display_text_does_not_determine_the_output_descriptor(text):
    worker, source, bus = _worker()
    source.observe.return_value = replace(source.observe.return_value, native_text=text)
    await worker.execute((AcquisitionCommand.get_metadata, CommandRequest("metadata")))
    assert _result(bus).data["metadata"] == text
    assert worker._observation.signal_info.sample_rate == 250
    assert worker._observation.signal_info.signal_channel_count == 2
    worker._streaming = True
    await worker.execute((AcquisitionCommand.get_metadata, CommandRequest("cached")))
    assert _result(bus).data["metadata"] == text
    source.observe.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("descriptor", [
    FIELDS,
    signal_info(**FIELDS),
])
async def test_invalid_observation_is_not_used(descriptor):
    worker, source, bus = _worker()
    source.observe.return_value = descriptor
    await worker.execute((AcquisitionCommand.get_metadata, CommandRequest("metadata")))
    assert not _result(bus).ok
    assert worker._observation is None


@pytest.mark.asyncio
async def test_input_attachment_rejects_missing_or_stale_receipt_without_starting_source():
    worker, source, bus = _worker()
    await worker.execute((AcquisitionCommand.get_metadata, CommandRequest("metadata")))
    receipt = {"generation": worker.generation, "acquisition_revision": worker.acquisition_revision, "processing_revision": "processing", "engine_revision": worker.engine_revision,
               "connection_id": worker.connection_id, "source_fingerprint": source_fingerprint(worker._metadata_response()["descriptor"]),
               "plan_fingerprint": "a" * 64, "input_id": "input"}
    for payload in (None, {**receipt, "connection_id": "old"}, {**receipt, "acquisition_revision": "old"},
                    {**receipt, "source_fingerprint": "f" * 64}, {**receipt, "generation": "old"}):
        await worker.execute((AcquisitionCommand.attach_processing, CommandRequest("start", payload)))
        assert _result(bus).error_code == ("INVALID_COMMAND_PAYLOAD" if payload is None else "PROCESSING_INPUT_REJECTED")
    source.start_stream.assert_not_awaited()
    worker._validate_signal_receipt(receipt)


@pytest.mark.asyncio
async def test_source_starts_without_a_prepared_processing_plan():
    worker, source, bus = _worker()
    async def start():
        worker._streaming = True
    source.start_stream.side_effect = start
    source.read_chunk = AsyncMock(return_value=None)
    source.stop_stream = AsyncMock()
    await worker.execute((AcquisitionCommand.start_stream, CommandRequest("start")))
    assert _result(bus).ok
    source.start_stream.assert_awaited_once()
    await worker.fetch_task


@pytest.mark.asyncio
async def test_input_attachment_uses_new_connection_after_reconnection():
    worker, source, bus = _worker()
    await worker.execute((AcquisitionCommand.get_metadata, CommandRequest("metadata")))
    old_receipt = {"generation": worker.generation, "acquisition_revision": worker.acquisition_revision, "processing_revision": "processing", "engine_revision": worker.engine_revision,
                   "connection_id": worker.connection_id, "source_fingerprint": source_fingerprint(worker._metadata_response()["descriptor"]),
                   "plan_fingerprint": "a" * 64, "input_id": "input"}
    async def disconnect():
        source.is_connected = False
    async def connect():
        source.is_connected = True
    source.disconnect = disconnect
    source.connect = connect
    await worker._on_disconnect()
    await worker._on_connect()
    await worker.execute((AcquisitionCommand.get_metadata, CommandRequest("reconnected-metadata")))
    stale_connection = {**old_receipt, "acquisition_revision": worker.acquisition_revision}
    await worker.execute((AcquisitionCommand.attach_processing, CommandRequest("stale-connection", stale_connection)))
    assert _result(bus).error_code == "PROCESSING_INPUT_REJECTED"
    source.start_stream.assert_not_awaited()

    receipt = {**stale_connection, "connection_id": worker.connection_id,
               "source_fingerprint": source_fingerprint(worker._metadata_response()["descriptor"])}
    await worker.execute((AcquisitionCommand.attach_processing, CommandRequest("prepared-input", receipt)))
    marker = worker.data_manager.put_data.call_args.args[0]
    assert isinstance(marker, ProcessingInputStart)
    assert marker.receipt.to_dict() == receipt
    assert worker.processing_active.is_set()
    source.start_stream.assert_not_awaited()


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
    assert worker.connection_id is None and worker._observation is None
    await worker._on_connect()
    assert worker.connection_id != connection_id


@pytest.mark.asyncio
async def test_scan45_metadata_reports_unsupported_query_separately_from_configured_output():
    from omnibci.infrastructure.plugins.acquisition.neuroscan.scan45.scan45 import Scan45DataReader
    source = Scan45DataReader(parameters={'sample_rate': 250, 'channel_count': 3}, resource_root=Path(__file__).parent)
    source._is_connected = True
    metadata = await source.observe()
    assert "not implemented" in metadata.native_text.lower()
    descriptor = metadata.signal_info
    assert descriptor.output_row_count == 3
    assert descriptor.signal_channel_count == 2
    assert descriptor.sample_rate == 250
    assert metadata.provenance == "user-declared"
