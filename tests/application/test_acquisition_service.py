"""
Unit tests for AcquisitionAppService.
"""
from omnibci.application.commands.acquisition_commands import (
    ConnectDataProviderCommand,
    DisconnectDataProviderCommand,
    StartStreamCommand,
    StopStreamCommand,
)
from omnibci.application.queries.acquisition_queries import GetAcquisitionStatusQuery
from omnibci.shared.events import EventType


class TestAcquisitionConnect:
    def test_connect_dispatches_event(self, acquisition_service, mock_engine):
        acquisition_service.handle_connect(ConnectDataProviderCommand())
        mock_engine.event_bus.put_event.assert_called_once()
        event = mock_engine.event_bus.put_event.call_args[0][0]
        assert event.type == EventType.ConnectDataProvider

    def test_connect_sets_connected_flag(self, acquisition_service):
        acquisition_service.handle_connect(ConnectDataProviderCommand())
        assert acquisition_service._connected is True

    def test_disconnect_clears_flags(self, acquisition_service):
        acquisition_service._connected = True
        acquisition_service._streaming = True
        acquisition_service.handle_disconnect(DisconnectDataProviderCommand())
        assert acquisition_service._connected is False
        assert acquisition_service._streaming is False

    def test_disconnect_dispatches_event(self, acquisition_service, mock_engine):
        acquisition_service.handle_disconnect(DisconnectDataProviderCommand())
        event = mock_engine.event_bus.put_event.call_args[0][0]
        assert event.type == EventType.DisconnectDataProvider


class TestAcquisitionStream:
    def test_start_stream_dispatches_event(self, acquisition_service, mock_engine):
        acquisition_service.handle_start_stream(StartStreamCommand())
        event = mock_engine.event_bus.put_event.call_args[0][0]
        assert event.type == EventType.StartStream

    def test_start_stream_sets_streaming_flag(self, acquisition_service):
        acquisition_service.handle_start_stream(StartStreamCommand())
        assert acquisition_service._streaming is True

    def test_stop_stream_dispatches_event(self, acquisition_service, mock_engine):
        acquisition_service.handle_stop_stream(StopStreamCommand())
        event = mock_engine.event_bus.put_event.call_args[0][0]
        assert event.type == EventType.StopStream

    def test_stop_stream_clears_streaming_flag(self, acquisition_service):
        acquisition_service._streaming = True
        acquisition_service.handle_stop_stream(StopStreamCommand())
        assert acquisition_service._streaming is False


class TestAcquisitionStatus:
    def test_initial_status(self, acquisition_service):
        result = acquisition_service.handle_get_status(GetAcquisitionStatusQuery())
        assert result.connected is False
        assert result.streaming is False

    def test_status_after_connect_and_stream(self, acquisition_service):
        acquisition_service.handle_connect(ConnectDataProviderCommand())
        acquisition_service.handle_start_stream(StartStreamCommand())
        result = acquisition_service.handle_get_status(GetAcquisitionStatusQuery())
        assert result.connected is True
        assert result.streaming is True
