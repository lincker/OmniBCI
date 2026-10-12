"""Metadata normalization and stream preparation at the acquisition boundary."""
from unittest.mock import AsyncMock, MagicMock

import pytest

from omnibci.application.events import CommandRequest
from omnibci.infrastructure.acquisition.acquisition import Acquisition, AcquisitionCommand
from omnibci.infrastructure.acquisition.metadata import source_metadata
from omnibci.shared.events import EventType


SETTINGS = {"ACQUISITION.signal_source": "test_source", "ACQUISITION.sample_rate": 250,
            "ACQUISITION.channel_number": 3, "SIGNAL.setup": {}}
RAW = {"sample_rate": 250, "output_channel_count": 3, "channel_labels": ["C3", "C4"],
       "provenance": "device-reported", "native_event": "separate-event-packets"}


def test_manual_fallback_keeps_unknown_units_and_signal_types():
    metadata = source_metadata(None, SETTINGS, source_id="test_source", connection_id="connected")
    assert [channel.name for channel in metadata.channels] == ["CH1", "CH2"]
    assert all(channel.unit is None and channel.signal_type == "unknown" for channel in metadata.channels)
    assert metadata.trigger_row_index == 2
    assert metadata.output_row_count == 3
    assert metadata.provenance == "user-declared"
    assert all(channel.name_provenance == "generated" for channel in metadata.channels)


def test_observation_excludes_manual_montage_legacy_names_and_reference():
    from omnibci.domain.montage import ChannelDescriptor, SourceMetadata
    manual = SourceMetadata("test_source", 250, (
        ChannelDescriptor(0, "C3", unit="uV"), ChannelDescriptor(1, "C4", unit="uV"),
    ), 2, original_reference="Cz")
    settings = {**SETTINGS, "ACQUISITION.channel_names": ["C3", "C4"],
                "ACQUISITION.manual_montage": manual.to_dict(),
                "SIGNAL.setup": {"manual_metadata": manual.to_dict()}}
    observed = source_metadata({}, settings, source_id="test_source", connection_id="connection",
                               include_manual=False, validate_configuration=False)
    assert [channel.name for channel in observed.channels] == ["CH1", "CH2"]
    assert all(channel.name_provenance == "generated" and channel.unit is None for channel in observed.channels)
    assert observed.original_reference is None
    effective = source_metadata({}, settings, source_id="test_source", connection_id="connection")
    assert [channel.name for channel in effective.channels] == ["C3", "C4"]
    assert all(channel.name_provenance is None and channel.unit == "uV" for channel in effective.channels)
    assert effective.original_reference == "Cz"


def test_profile_manual_montage_precedes_legacy_manual_and_rejects_known_unit_conflicts():
    from omnibci.domain.montage import ChannelDescriptor, SourceMetadata
    manual = SourceMetadata("test_source", 250, (
        ChannelDescriptor(0, "C3", unit="uV"), ChannelDescriptor(1, "C4", unit="uV"),
    ), 2)
    settings = {**SETTINGS, "ACQUISITION.manual_montage": manual.to_dict(),
                "SIGNAL.setup": {"manual_metadata": {"invalid": "legacy declaration must not be consulted"}}}
    effective = source_metadata(RAW, settings, source_id="test_source", connection_id="connection")
    assert effective.channels[0].unit == "uV"
    with pytest.raises(ValueError, match="Manual unit conflicts"):
        source_metadata({**RAW, "channel_units": ["V", "V"]}, settings,
                        source_id="test_source", connection_id="connection")


def test_manual_metadata_fills_only_missing_observations():
    from omnibci.domain.montage import ChannelDescriptor, SourceMetadata
    manual = SourceMetadata("test_source", 250, (
        ChannelDescriptor(0, "C3", "eeg", "uV"),
        ChannelDescriptor(1, "C4", "eeg", "uV", position=(0.01, 0.02, 0.03),
                          coordinate_frame="head", position_provenance="user-declared"),
    ), 2, original_reference="Cz")
    settings = {**SETTINGS, "SIGNAL.setup": {"manual_metadata": manual.to_dict()}}
    metadata = source_metadata({**RAW, "channel_units": ["V", None]}, settings,
                               source_id="test_source", connection_id="connection")
    assert [channel.name for channel in metadata.channels] == ["C3", "C4"]
    assert [channel.unit for channel in metadata.channels] == ["V", "uV"]
    assert all(channel.signal_type == "eeg" for channel in metadata.channels)
    assert metadata.channels[1].position == (0.01, 0.02, 0.03)
    assert metadata.original_reference == "Cz"
    settings["ACQUISITION.sample_rate"] = 500
    with pytest.raises(ValueError, match="Manual metadata sample rate"):
        source_metadata(None, settings, source_id="test_source", connection_id="connection")


@pytest.mark.parametrize("label_source", ["observed", "legacy"])
def test_manual_row_metadata_cannot_follow_reordered_source_labels(label_source):
    from omnibci.domain.montage import ChannelDescriptor, SourceMetadata
    manual = SourceMetadata("test_source", 250, (
        ChannelDescriptor(0, "C3", "eeg", "uV", reference="Cz"),
        ChannelDescriptor(1, "C4", "eeg", "V", reference="M1", position=(0.01, 0.02, 0.03),
                          coordinate_frame="head", position_provenance="user-declared"),
    ), 2)
    settings = {**SETTINGS, "SIGNAL.setup": {"manual_metadata": manual.to_dict()}}
    raw = {**RAW, "channel_labels": ["C4", "C3"]}
    if label_source == "legacy":
        settings["ACQUISITION.channel_names"] = raw.pop("channel_labels")
    with pytest.raises(ValueError, match="Source row 0 changed: manual metadata expects 'C3', got 'C4'"):
        source_metadata(raw, settings, source_id="test_source", connection_id="connection")


def test_manual_original_reference_cannot_override_conflicting_device_reference():
    from omnibci.domain.montage import ChannelDescriptor, SourceMetadata
    manual = SourceMetadata("test_source", 250, (
        ChannelDescriptor(0, "C3", "eeg", "uV"), ChannelDescriptor(1, "C4", "eeg", "uV"),
    ), 2, original_reference="Cz")
    settings = {**SETTINGS, "SIGNAL.setup": {"manual_metadata": manual.to_dict()}}
    with pytest.raises(ValueError, match="Manual original reference differs from the source"):
        source_metadata({**RAW, "original_reference": "M1"}, settings,
                        source_id="test_source", connection_id="connection")


@pytest.mark.parametrize("raw, message", [
    ({**RAW, "sample_rate": 500}, "sample rate 500 differs"),
    ({**RAW, "output_channel_count": 4}, "row count 4 differs"),
    ({**RAW, "sample_rate": 249.5}, "positive integer"),
    ({**RAW, "sample_rate": float("inf")}, "positive integer"),
    ({**RAW, "channel_labels": ["C3"]}, "exactly 2"),
    ({**RAW, "channel_labels": "C3"}, "must be a sequence"),
])
def test_inconsistent_or_unsupported_metadata_is_rejected(raw, message):
    with pytest.raises(ValueError, match=message):
        source_metadata(raw, SETTINGS, source_id="test_source", connection_id="connected")


def _worker():
    source = MagicMock(is_connected=True, is_streaming=False)
    source.get_metadata = AsyncMock(return_value=RAW)
    source.start_stream = AsyncMock()
    bus = MagicMock()
    worker = Acquisition(source, MagicMock(), MagicMock(), event_bus=bus,
                         generation="generation", config_revision="revision", metadata_settings=SETTINGS)
    return worker, source, bus


def _result(bus):
    return [call.args[0].data for call in bus.put_event.call_args_list
            if call.args[0].type == EventType.CommandResult][-1]


@pytest.mark.asyncio
async def test_metadata_command_returns_serializable_descriptor_and_never_reads_while_streaming():
    worker, source, bus = _worker()
    await worker.execute((AcquisitionCommand.get_metadata, CommandRequest("metadata")))
    result = _result(bus)
    assert result.ok and result.command == "GetSourceMetadata"
    assert result.data["connection_id"] == worker.connection_id
    assert result.data["trigger_row_index"] == 2
    source.is_streaming = True
    await worker.execute((AcquisitionCommand.get_metadata, CommandRequest("cached")))
    assert _result(bus).ok
    source.get_metadata.assert_awaited_once()
    await worker.execute((AcquisitionCommand.get_metadata, CommandRequest("refresh", {"refresh": True})))
    assert _result(bus).error_code == "METADATA_REQUIRES_STOP"
    source.get_metadata.assert_awaited_once()


@pytest.mark.asyncio
async def test_observed_device_values_survive_configured_rate_and_count_mismatch():
    worker, source, bus = _worker()
    source.get_metadata.return_value = {**RAW, "sample_rate": 500, "output_channel_count": 4,
                                       "channel_labels": ["C3", "C4", "Cz"]}
    await worker.execute((AcquisitionCommand.get_metadata, CommandRequest("observe", {"include_observation": True})))
    result = _result(bus)
    assert result.ok
    assert result.data["observation"]["sample_rate"] == 500
    assert result.data["observation"]["output_row_count"] == 4
    assert result.data["metadata"] is None
    assert "sample rate 500 differs" in result.data["metadata_error"]


@pytest.mark.asyncio
async def test_failed_device_read_allows_manual_effective_metadata_without_inventing_observation():
    from omnibci.domain.montage import ChannelDescriptor, SourceMetadata
    worker, source, bus = _worker()
    manual = SourceMetadata("test_source", 250, (
        ChannelDescriptor(0, "C3", unit="uV"), ChannelDescriptor(1, "C4", unit="uV"),
    ), 2, original_reference="Cz")
    worker._metadata_settings = {**SETTINGS, "ACQUISITION.manual_montage": manual.to_dict()}
    source.get_metadata.side_effect = RuntimeError("Device has no metadata command")
    await worker.execute((AcquisitionCommand.get_metadata, CommandRequest("observe", {"include_observation": True})))
    result = _result(bus)
    assert result.ok and result.data["observation"] is None
    assert result.data["observation_error"] == "Device has no metadata command"
    assert result.data["metadata_error"] is None
    assert result.data["metadata"]["channels"][0]["name"] == "C3"
    assert result.data["metadata"]["channels"][0]["unit"] == "uV"


@pytest.mark.asyncio
@pytest.mark.parametrize("modern", [False, True])
async def test_prepared_start_rejects_missing_or_stale_receipt_before_starting_source(modern):
    worker, source, bus = _worker()
    if modern:
        from omnibci.application.plugin_profiles import PluginSelection, ProfileBinding
        from omnibci.domain.montage import ProcessingInput
        worker._metadata_settings = {key: value for key, value in SETTINGS.items() if key != "SIGNAL.setup"}
        worker._metadata_settings["PLUGINS.bindings"] = [ProfileBinding(
            "processing.decoder", "decoder", None, PluginSelection("test.decoder", "default", "1"),
            {}, processing_input=ProcessingInput(),
        ).to_dict()]
    await worker.execute((AcquisitionCommand.get_metadata, CommandRequest("metadata")))
    receipt = {"generation": worker.generation, "config_revision": worker.config_revision,
               "connection_id": worker.connection_id, "source_fingerprint": worker._metadata.fingerprint,
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
    worker._metadata_settings = {key: value for key, value in SETTINGS.items() if key != "SIGNAL.setup"}
    worker._metadata_settings["PLUGINS.bindings"] = [materialize_processing_binding(
        binding, requires_channels=True, legacy_setup=None)]
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
    assert worker.connection_id is None and worker._metadata is None
    await worker._on_connect()
    assert worker.connection_id != connection_id


@pytest.mark.asyncio
async def test_scan45_metadata_uses_declarations_without_socket_reads():
    from omnibci.infrastructure.plugins.acquisition.neuroscan.scan45.scan45 import Scan45DataReader
    source = Scan45DataReader(parameters={"sample_rate": 250, "channel_number": 3})
    source.request_basic_info = AsyncMock(side_effect=AssertionError("must not read header"))
    metadata = await source.get_metadata()
    assert metadata["output_channel_count"] == 3
    assert metadata["sample_rate"] == 250
    assert metadata["provenance"] == "user-declared"
