"""
Unit tests for AcquisitionAppService.
"""
import pytest
from unittest.mock import MagicMock

from omnibci.application.commands.acquisition_commands import (
    ConnectDataProviderCommand,
    DisconnectDataProviderCommand,
    StartStreamCommand,
    StopStreamCommand,
)
from omnibci.application.events import AcquisitionSnapshot
from omnibci.application.queries.acquisition_queries import GetAcquisitionStatusQuery
from omnibci.shared.events import Event, EventType
from omnibci.application.services.acquisition_service import AcquisitionAppService


@pytest.fixture(autouse=True)
def _running_engine(engine_service):
    """Command-focused tests exercise a runtime-ready EnginePort."""
    engine_service._running = True
    engine_service._accepting_commands = True
    engine_service._engine_thread = MagicMock()
    engine_service._engine_thread.is_alive.return_value = True


def test_command_executor_is_required():
    with pytest.raises(TypeError):
        AcquisitionAppService()


class TestAcquisitionConnect:
    def test_connect_dispatches_event(self, acquisition_service, mock_engine):
        acquisition_service.handle_connect(ConnectDataProviderCommand())
        mock_engine.event_bus.put_event.assert_called_once()
        event = mock_engine.event_bus.put_event.call_args[0][0]
        assert event.type == EventType.ConnectDataProvider

    def test_connect_does_not_guess_state(self, acquisition_service):
        # Projection semantics: commands dispatch intent; only worker-reported
        # AcquisitionState events (on_acquisition_state) move the flags.
        acquisition_service.handle_connect(ConnectDataProviderCommand())
        assert acquisition_service._connected is False

    def test_disconnect_dispatches_event(self, acquisition_service, mock_engine):
        acquisition_service.handle_disconnect(DisconnectDataProviderCommand())
        event = mock_engine.event_bus.put_event.call_args[0][0]
        assert event.type == EventType.DisconnectDataProvider


class TestAcquisitionStream:
    def test_start_stream_dispatches_event(self, acquisition_service, mock_engine):
        acquisition_service.handle_start_stream(StartStreamCommand())
        event = mock_engine.event_bus.put_event.call_args[0][0]
        assert event.type == EventType.StartStream

    def test_start_stream_does_not_guess_state(self, acquisition_service):
        acquisition_service.handle_start_stream(StartStreamCommand())
        assert acquisition_service._streaming is False

    def test_stop_stream_dispatches_event(self, acquisition_service, mock_engine):
        acquisition_service.handle_stop_stream(StopStreamCommand())
        event = mock_engine.event_bus.put_event.call_args[0][0]
        assert event.type == EventType.StopStream

    def test_start_waits_for_preparation_receipt(self):
        executor = MagicMock()
        service = AcquisitionAppService(executor)
        receipt = {"connection_id": "connection"}
        service.set_signal_preparer(lambda: receipt)
        service.handle_start_stream(StartStreamCommand())
        executor.request.assert_called_once_with(EventType.StartStream, receipt)

    def test_failed_preparation_does_not_start_stream(self):
        executor = MagicMock()
        service = AcquisitionAppService(executor)
        service.set_signal_preparer(MagicMock(side_effect=ValueError("Missing C3")))
        with pytest.raises(ValueError, match="Missing C3"):
            service.handle_start_stream(StartStreamCommand())
        executor.request.assert_not_called()



class TestAcquisitionStatus:
    def test_initial_status(self, acquisition_service):
        result = acquisition_service.handle_get_status(GetAcquisitionStatusQuery())
        assert result.connected is False
        assert result.streaming is False

    def test_status_reflects_worker_snapshots(self, acquisition_service):
        acquisition_service.on_acquisition_state(
            Event(EventType.AcquisitionState,
                  AcquisitionSnapshot(connected=True, streaming=True)))
        result = acquisition_service.handle_get_status(GetAcquisitionStatusQuery())
        assert result.connected is True
        assert result.streaming is True

    def test_snapshot_can_clear_state(self, acquisition_service):
        acquisition_service.on_acquisition_state(
            Event(EventType.AcquisitionState,
                  AcquisitionSnapshot(connected=True, streaming=True)))
        # e.g. file playback EOF: worker reports stream closed
        acquisition_service.on_acquisition_state(
            Event(EventType.AcquisitionState,
                  AcquisitionSnapshot(connected=True, streaming=False)))
        result = acquisition_service.handle_get_status(GetAcquisitionStatusQuery())
        assert result.connected is True
        assert result.streaming is False

    def test_bad_payload_is_rejected(self, acquisition_service):
        acquisition_service.on_acquisition_state(
            Event(EventType.AcquisitionState,
                  AcquisitionSnapshot(connected=True, streaming=True)))
        acquisition_service.on_acquisition_state(
            Event(EventType.AcquisitionState, {"streaming": False}))  # wrong type
        result = acquisition_service.handle_get_status(GetAcquisitionStatusQuery())
        assert result.streaming is True   # projection untouched

    def test_reset_clears_projection(self, acquisition_service):
        acquisition_service.on_acquisition_state(
            Event(EventType.AcquisitionState,
                  AcquisitionSnapshot(connected=True, streaming=True)))
        acquisition_service.reset()
        result = acquisition_service.handle_get_status(GetAcquisitionStatusQuery())
        assert result.connected is False
        assert result.streaming is False

    def test_metadata_is_confirmed_then_invalidated_on_disconnect(self):
        from omnibci.domain.signal_info import ChannelDescriptor, SignalInfo
        metadata = SignalInfo("test", 250, (ChannelDescriptor(0, "C3"),), 1,
                                  connection_id="connection", provenance="device-reported")
        executor = MagicMock()
        executor.request.return_value.data = _metadata_response(metadata)
        service = AcquisitionAppService(executor)
        result = service.get_metadata(refresh=True)
        assert {key: result[key] for key in ("source_id", "connection_id", "metadata", "descriptor")} == {
            key: _metadata_response(metadata)[key]
            for key in ("source_id", "connection_id", "metadata", "descriptor")}
        assert result["comparison"]["status"] == "unconfirmed"
        executor.request.assert_called_once_with(EventType.GetSourceMetadata, {"refresh": True}, timeout_s=5.0)
        assert service.cached_descriptor == metadata.to_dict()
        status = service.handle_get_status(GetAcquisitionStatusQuery())
        assert status.sample_rate == 250 and status.channel_count == 1
        assert status.metadata_available and status.metadata_provenance == "device-reported"
        service.on_acquisition_state(Event(EventType.AcquisitionState, AcquisitionSnapshot(False, False)))
        assert service.cached_descriptor is None

    def test_configured_dimensions_are_not_reported_as_source_metadata(self):
        service = AcquisitionAppService(MagicMock(), settings={
            "ACQUISITION.signal_source": "test", "ACQUISITION.channel_number": 9,
            "ACQUISITION.sample_rate": 1000,
        })
        status = service.handle_get_status(GetAcquisitionStatusQuery())
        assert status.source == "test"
        assert status.channel_count is None
        assert status.sample_rate is None
        assert status.metadata_available is False


def _metadata_response(metadata):
    return {"source_id": metadata.source_id, "connection_id": metadata.connection_id,
            "metadata": "Test amplifier\nVendor status: recording enabled\n<unknown />\n",
            "descriptor": metadata.to_dict(), "descriptor_error": None}


def _metadata_service():
    from omnibci.application.events import CommandResult
    from omnibci.application.plugin_profiles import PluginSelection, ProfileBinding
    from omnibci.domain.signal_info import ChannelDescriptor, SignalInfo
    metadata = SignalInfo("test", 250, (ChannelDescriptor(0, "C3", unit="uV"),), 1,
                              connection_id="connection", provenance="device-reported")
    response = _metadata_response(metadata)
    executor = MagicMock()
    executor.request.return_value = CommandResult("read", "GetSourceMetadata", True, data=response)
    service = AcquisitionAppService(executor)
    binding = ProfileBinding("acquisition.source", "acquisition",
        "00000000-0000-0000-0000-000000000001", PluginSelection("test.source", "default", "1"),
        {"sample_rate": 250, "channel_number": 2}, profile_revision=1)
    provider = MagicMock(return_value=binding.to_dict())
    service.set_metadata_binding(provider)
    return service, executor, provider, binding, metadata, response


@pytest.mark.parametrize("raw", [
    "", "Test amplifier\r\n  Vendor field: <unknown /> & value\n",
    '{"sample_rate": 999, "channel_count": 99, "provenance": "vendor-text"}',
])
def test_metadata_preserves_opaque_plugin_text_and_exposes_acquisition_descriptor(raw):
    service, executor, provider, _, metadata, response = _metadata_service()
    response["metadata"] = raw
    value = service.get_metadata(refresh=True)
    assert value == {
        **{key: response[key] for key in ("source_id", "connection_id", "metadata", "descriptor")},
        "comparison": {"status": "matched", "sample_rate": {"expected": 250, "actual": 250},
                       "channel_number": {"expected": 2, "actual": 2}},
    }
    assert value["metadata"] == raw
    value["metadata"] = "changed by caller"
    value["descriptor"]["channels"][0]["name"] = "changed by caller"
    assert service._raw_metadata["metadata"] == raw
    assert service._raw_metadata["descriptor"]["channels"][0]["name"] == "C3"
    assert service.cached_descriptor == metadata.to_dict()
    status = service.handle_get_status(GetAcquisitionStatusQuery())
    assert status.sample_rate == 250 and status.channel_count == 1
    assert status.metadata_provenance == "device-reported"
    provider.assert_called_once_with()
    executor.request.assert_called_once_with(EventType.GetSourceMetadata, {"refresh": True}, timeout_s=5.0)


def test_cached_signal_metadata_belongs_to_the_running_acquisition_binding():
    from dataclasses import replace
    service, _, _, binding, metadata, _ = _metadata_service()
    service.get_metadata()
    assert service.cached_descriptor_for(binding.to_dict()) == metadata.to_dict()
    other_profile = replace(binding, profile_id="00000000-0000-0000-0000-000000000002")
    assert service.cached_descriptor_for(other_profile.to_dict()) is None
    assert service.cached_descriptor_for(replace(binding, parameters={"sample_rate": 500}).to_dict()) is None
    assert service.cached_descriptor_for(None) is None
    assert service.cached_descriptor_for(replace(binding, profile_revision=2).to_dict()) == metadata.to_dict()


@pytest.mark.parametrize("invalidation", ["disconnect", "reset", "worker-disconnect", "failed-read"])
def test_raw_and_signal_metadata_are_cleared_with_the_connection(invalidation):
    service, executor, _, binding, _, _ = _metadata_service()
    service.get_metadata()
    if invalidation == "disconnect":
        service.handle_disconnect(DisconnectDataProviderCommand())
    elif invalidation == "reset":
        service.reset()
    elif invalidation == "worker-disconnect":
        service.on_acquisition_state(Event(EventType.AcquisitionState, AcquisitionSnapshot(False, False)))
    else:
        executor.request.side_effect = RuntimeError("Metadata unavailable")
        with pytest.raises(RuntimeError, match="unavailable"):
            service.get_metadata()
    assert service.cached_descriptor_for(binding.to_dict()) is None
    assert service._raw_metadata is None
    assert service.handle_get_status(GetAcquisitionStatusQuery()).metadata_available is False


def test_raw_metadata_remains_available_when_processing_cannot_normalize_it():
    from omnibci.application.services.command_result_service import CommandRejectedError
    service, _, _, _, _, response = _metadata_service()
    response["descriptor"] = None
    response["descriptor_error"] = "Source does not report a sample rate"
    result = service.get_metadata()
    assert result["metadata"] == response["metadata"]
    assert result["comparison"] is None
    assert service.handle_get_status(GetAcquisitionStatusQuery()).metadata_available is True
    with pytest.raises(CommandRejectedError, match="sample rate") as rejected:
        service.get_descriptor()
    assert rejected.value.result.error_code == "INVALID_ACQUISITION_DESCRIPTOR"
    assert service._raw_metadata["metadata"] == response["metadata"]
    assert service.cached_descriptor is None


@pytest.mark.parametrize("raw", [
    "sample_rate: 1000\nchannel_count: 64\nprovenance: device-reported",
    '{"sample_rate": 1000, "channel_count": 64, "provenance": "device-reported"}',
])
def test_status_does_not_extract_dimensions_or_provenance_from_metadata_text(raw):
    service, _, _, _, _, response = _metadata_service()
    response["descriptor"] = None
    response["descriptor_error"] = "Source does not describe signal rows"
    response["metadata"] = raw
    service.get_metadata()
    status = service.handle_get_status(GetAcquisitionStatusQuery())
    assert status.sample_rate is None
    assert status.channel_count is None
    assert status.metadata_provenance is None
    assert status.metadata_available is True


def test_status_does_not_present_configuration_metadata_as_device_dimensions():
    service, _, _, _, _, response = _metadata_service()
    response["descriptor"]["provenance"] = "user-declared"
    response["metadata"] = "This source does not support querying metadata."
    service.get_metadata()
    status = service.handle_get_status(GetAcquisitionStatusQuery())
    assert status.sample_rate is None
    assert status.channel_count is None
    assert status.metadata_available is True
    assert status.metadata_provenance == "user-declared"


def test_processing_receives_only_normalized_signal_metadata():
    service, _, _, _, metadata, _ = _metadata_service()
    assert service.get_descriptor() == metadata.to_dict()


def test_reconnect_changes_status_identity_and_invalidates_previous_connection_metadata():
    service, _, _, binding, _, _ = _metadata_service()
    service.get_metadata()
    assert service.handle_get_status(GetAcquisitionStatusQuery()).connection_id == "connection"
    service.on_acquisition_state(Event(EventType.AcquisitionState, AcquisitionSnapshot(True, False, "connection")))
    assert service.cached_descriptor_for(binding.to_dict()) is not None
    service.on_acquisition_state(Event(EventType.AcquisitionState, AcquisitionSnapshot(True, False, "reconnected")))
    status = service.handle_get_status(GetAcquisitionStatusQuery())
    assert status.connected is True
    assert status.connection_id == "reconnected"
    assert status.metadata_available is False
    assert service.cached_descriptor_for(binding.to_dict()) is None
    assert service._raw_metadata is None
    service.on_acquisition_state(Event(EventType.AcquisitionState, AcquisitionSnapshot(False, False)))
    assert service.handle_get_status(GetAcquisitionStatusQuery()).connection_id is None


@pytest.mark.parametrize("change", [
    {"metadata": []}, {"metadata": {"sample_rate": 250}}, {"metadata": None},
    {"connection_id": ""}, {"source_id": None}, {"descriptor_error": []},
    {"descriptor": {"invalid": True}},
])
def test_invalid_worker_metadata_is_rejected(change):
    service, _, _, _, _, response = _metadata_service()
    response.update(change)
    with pytest.raises(RuntimeError, match="invalid acquisition"):
        service.get_metadata()
    assert service.cached_descriptor is None
    assert service._raw_metadata is None


def test_signal_metadata_must_match_the_raw_metadata_connection():
    service, _, _, _, _, response = _metadata_service()
    response["descriptor"]["connection_id"] = "stale"
    with pytest.raises(RuntimeError, match="different source connection"):
        service.get_metadata()
    assert service.cached_descriptor is None


def test_connect_waits_for_an_explicit_metadata_request():
    service, executor, provider, *_ = _metadata_service()
    service.handle_connect(ConnectDataProviderCommand())
    executor.request.assert_called_once_with(EventType.ConnectDataProvider, None)
    provider.assert_not_called()
    assert service.cached_descriptor is None
    assert service._raw_metadata is None


def test_metadata_comparison_uses_the_pinned_profile_without_replacing_its_expectations():
    from dataclasses import replace
    service, _, provider, binding, _, response = _metadata_service()
    changed = replace(binding, parameters={"sample_rate": 1000, "channel_number": 65}).to_dict()
    provider.return_value = changed
    response["metadata"] = '{"sample_rate": 1000, "channel_number": 65}'
    result = service.get_metadata()
    assert result["comparison"] == {
        "status": "mismatched",
        "sample_rate": {"expected": 1000, "actual": 250},
        "channel_number": {"expected": 65, "actual": 2},
    }
    result["comparison"]["sample_rate"]["expected"] = 250
    assert changed["parameters"] == {"sample_rate": 1000, "channel_number": 65}
    assert service._raw_metadata["comparison"]["sample_rate"]["expected"] == 1000


def test_scan45_declared_dimensions_are_not_presented_as_a_verified_match():
    service, _, _, _, _, response = _metadata_service()
    response["descriptor"]["provenance"] = "user-declared"
    assert service.get_metadata()["comparison"]["status"] == "unverified"
