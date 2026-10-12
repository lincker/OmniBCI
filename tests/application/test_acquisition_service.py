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
        from omnibci.domain.montage import ChannelDescriptor, SourceMetadata
        metadata = SourceMetadata("test", 250, (ChannelDescriptor(0, "C3"),), 1,
                                  connection_id="connection", provenance="device-reported")
        executor = MagicMock()
        executor.request.return_value.data = metadata.to_dict()
        service = AcquisitionAppService(executor)
        assert service.get_metadata(refresh=True) == metadata.to_dict()
        executor.request.assert_called_once_with(EventType.GetSourceMetadata, {"refresh": True}, timeout_s=5.0)
        assert service.cached_metadata == metadata.to_dict()
        status = service.handle_get_status(GetAcquisitionStatusQuery())
        assert status.sample_rate == 250 and status.channel_count == 2
        assert status.metadata_available and status.metadata_provenance == "device-reported"
        service.on_acquisition_state(Event(EventType.AcquisitionState, AcquisitionSnapshot(False, False)))
        assert service.cached_metadata is None


def _observation_service():
    from omnibci.application.events import CommandResult
    from omnibci.application.plugin_profiles import PluginSelection, ProfileBinding
    from omnibci.domain.montage import ChannelDescriptor, SourceMetadata
    observed = SourceMetadata("test", 250, (ChannelDescriptor(0, "C3", "unknown"),), 1,
                              connection_id="connection", provenance="device-reported")
    effective = SourceMetadata("test", 250, (ChannelDescriptor(0, "C3", unit="uV"),), 1,
                               connection_id="connection", original_reference="Cz", provenance="device-reported")
    result = {"observation": observed.to_dict(), "metadata": effective.to_dict(),
              "metadata_error": None, "observation_error": None}
    executor = MagicMock()
    executor.request.return_value = CommandResult("read", "GetSourceMetadata", True, data=result)
    service = AcquisitionAppService(executor)
    binding = ProfileBinding("acquisition.source", "acquisition",
        "00000000-0000-0000-0000-000000000001", PluginSelection("test.source", "default", "1"),
        {"sample_rate": 250, "channel_number": 2}, profile_revision=1)
    provider, recorder = MagicMock(return_value=binding), MagicMock(return_value=object())
    service.set_metadata_recorder(provider, recorder)
    return service, executor, provider, recorder, binding, observed, effective, result


def test_cached_metadata_belongs_to_the_observed_acquisition_profile():
    from dataclasses import replace
    service, _, _, _, binding, _, effective, _ = _observation_service()
    service.get_metadata()
    assert service.cached_metadata_for(binding.to_dict()) == effective.to_dict()
    other_profile = replace(binding, profile_id="00000000-0000-0000-0000-000000000002")
    assert service.cached_metadata_for(other_profile.to_dict()) is None
    assert service.cached_metadata_for(replace(binding, parameters={"sample_rate": 500}).to_dict()) is None
    assert service.cached_metadata_for(replace(binding, manual_montage=effective).to_dict()) is None
    assert service.cached_metadata_for(None) is None
    assert service.cached_metadata_for(replace(binding, profile_revision=2).to_dict()) == effective.to_dict()


@pytest.mark.parametrize("invalidation", ["disconnect", "reset", "worker-disconnect", "failed-read"])
def test_metadata_ownership_is_cleared_with_the_observation(invalidation):
    service, executor, _, _, binding, _, _, _ = _observation_service()
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
    assert service.cached_metadata_for(binding.to_dict()) is None


def test_metadata_records_observation_separately_from_manual_effective_values():
    service, executor, provider, recorder, binding, observed, effective, _ = _observation_service()
    assert service.get_metadata(refresh=True, profile_id="target") == effective.to_dict()
    provider.assert_called_once_with("target")
    recorder.assert_called_once_with(binding, observed)
    assert recorder.call_args.args[1].channels[0].unit is None
    executor.request.assert_called_once_with(EventType.GetSourceMetadata,
                                            {"refresh": True, "include_observation": True}, timeout_s=5.0)


def test_device_observation_is_saved_before_reporting_effective_configuration_conflict():
    from omnibci.application.services.command_result_service import CommandRejectedError
    service, _, _, recorder, binding, observed, _, result = _observation_service()
    result["metadata"] = None
    result["metadata_error"] = "Source sample rate 250 differs from configured 500"
    with pytest.raises(CommandRejectedError, match="sample rate 250"):
        service.get_metadata(refresh=True, profile_id="target")
    recorder.assert_called_once_with(binding, observed)
    assert service.cached_metadata is None


def test_manual_fallback_is_available_to_prepare_but_explicit_read_reports_device_failure():
    from omnibci.application.services.command_result_service import CommandRejectedError
    service, _, _, recorder, _, _, effective, result = _observation_service()
    result["observation"] = None
    result["observation_error"] = "Device has no metadata command"
    assert service.get_metadata(allow_manual_fallback=True) == effective.to_dict()
    recorder.assert_not_called()
    with pytest.raises(CommandRejectedError, match="no metadata command"):
        service.get_metadata(refresh=True)
    assert service.cached_metadata is None


def test_explicit_read_reports_a_concurrent_profile_edit_instead_of_claiming_save():
    from omnibci.application.plugin_profiles import ProfileRevisionConflict
    service, _, _, recorder, _, _, effective, _ = _observation_service()
    recorder.return_value = None
    with pytest.raises(ProfileRevisionConflict, match="observation was not saved"):
        service.get_metadata(profile_id="target")
    assert service.get_metadata(allow_manual_fallback=True) == effective.to_dict()


def test_profile_guard_rejects_before_reading_source():
    from omnibci.application.plugin_profiles import ProfileRevisionConflict
    service, executor, provider, recorder, _, _, effective, _ = _observation_service()
    service._metadata = effective
    provider.side_effect = ProfileRevisionConflict("Requested profile is not the applied source")
    with pytest.raises(ProfileRevisionConflict, match="not the applied"):
        service.get_metadata(profile_id="target")
    executor.request.assert_not_called()
    recorder.assert_not_called()
    assert service.cached_metadata == effective.to_dict()


def test_invalid_worker_observation_is_reported_as_invalid_metadata():
    service, _, _, recorder, _, _, _, result = _observation_service()
    result["observation"] = {"invalid": True}
    with pytest.raises(RuntimeError, match="invalid acquisition metadata observation"):
        service.get_metadata()
    recorder.assert_not_called()


def test_connect_keeps_success_when_metadata_is_unavailable_but_exposes_storage_failure():
    from omnibci.application.plugin_profiles import PluginProfileStorageError
    service, executor, _, recorder, _, _, _, result = _observation_service()
    result["observation"] = None
    result["metadata"] = None
    result["metadata_error"] = "Source has no metadata"
    service.handle_connect(ConnectDataProviderCommand())
    assert executor.request.call_args_list[0].args == (EventType.ConnectDataProvider, None)
    recorder.assert_not_called()
    service, executor, _, recorder, *_ = _observation_service()
    recorder.side_effect = PluginProfileStorageError("disk full")
    with pytest.raises(PluginProfileStorageError, match="Source connected.*could not be saved"):
        service.handle_connect(ConnectDataProviderCommand())
    assert executor.request.call_args_list[0].args == (EventType.ConnectDataProvider, None)
