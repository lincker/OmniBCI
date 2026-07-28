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


@pytest.fixture(autouse=True)
def _running_engine(engine_service):
    """Command-focused tests exercise a runtime-ready EnginePort."""
    engine_service._running = True
    engine_service._accepting_commands = True
    engine_service._engine_thread = MagicMock()
    engine_service._engine_thread.is_alive.return_value = True


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
