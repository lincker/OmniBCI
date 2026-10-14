"""
Tests for /api/v1/acquisition endpoints.
"""
import pytest
from unittest.mock import MagicMock

from omnibci.application.events import CommandFailureKind, CommandResult
from omnibci.application.services.command_result_service import (
    CommandExecutionError,
    CommandRejectedError,
    CommandTimeoutError,
)


def _rejected(command: str = "StartStream") -> CommandRejectedError:
    return CommandRejectedError(CommandResult(
        command_id="cmd-rejected",
        command=command,
        ok=False,
        message="source is disconnected",
        error_code="DATA_SOURCE_NOT_CONNECTED",
    ))


@pytest.fixture(autouse=True)
def _running_engine(engine_service):
    engine_service._running = True
    engine_service._accepting_commands = True
    engine_service._engine_thread = MagicMock()
    engine_service._engine_thread.is_alive.return_value = True


class TestAcquisitionConnect:
    def test_connect_returns_confirmed_success(self, api_client, mock_engine):
        resp = api_client.post("/api/v1/acquisition/connect")
        assert resp.status_code == 200
        assert resp.json()["success"] is True
        assert resp.json()["message"] == "Data provider connected"

    def test_connect_dispatches_event(self, api_client, mock_engine):
        api_client.post("/api/v1/acquisition/connect")
        mock_engine.event_bus.put_event.assert_called()

    def test_disconnect_returns_confirmed_success(self, api_client, mock_engine):
        resp = api_client.post("/api/v1/acquisition/disconnect")
        assert resp.status_code == 200
        assert resp.json()["message"] == "Data provider disconnected"

    def test_connect_without_engine_returns_503(self, api_client, engine_service):
        engine_service._engine = None
        resp = api_client.post("/api/v1/acquisition/connect")
        assert resp.status_code == 503


class TestAcquisitionStream:
    def test_start_stream_returns_confirmed_success(self, api_client, mock_engine):
        resp = api_client.post("/api/v1/acquisition/stream/start")
        assert resp.status_code == 200
        assert resp.json()["success"] is True
        assert resp.json()["message"] == "Stream started"

    def test_stop_stream_returns_confirmed_success(self, api_client, mock_engine):
        resp = api_client.post("/api/v1/acquisition/stream/stop")
        assert resp.status_code == 200
        assert resp.json()["message"] == "Stream stopped"

    def test_start_stream_without_engine_returns_503(self, api_client, engine_service):
        engine_service._engine = None
        resp = api_client.post("/api/v1/acquisition/stream/start")
        assert resp.status_code == 503

    def test_worker_rejection_returns_409_with_command_detail(
        self, api_client, acquisition_service
    ):
        acquisition_service.handle_start_stream = MagicMock(
            side_effect=_rejected()
        )

        resp = api_client.post("/api/v1/acquisition/stream/start")

        assert resp.status_code == 409
        assert resp.json()["code"] == "COMMAND_REJECTED"
        assert resp.json()["detail"] == {
            "command_id": "cmd-rejected",
            "command": "StartStream",
            "error_code": "DATA_SOURCE_NOT_CONNECTED",
        }

    def test_missing_terminal_result_returns_504(
        self, api_client, acquisition_service
    ):
        acquisition_service.handle_start_stream = MagicMock(side_effect=
            CommandTimeoutError("cmd-timeout", "StartStream", 0.25)
        )

        resp = api_client.post("/api/v1/acquisition/stream/start")

        assert resp.status_code == 504
        assert resp.json()["code"] == "COMMAND_TIMEOUT"
        assert resp.json()["detail"]["command_id"] == "cmd-timeout"

    def test_worker_execution_failure_returns_500_not_conflict(
        self, api_client, acquisition_service
    ):
        acquisition_service.handle_start_stream = MagicMock(side_effect=
            CommandExecutionError(CommandResult(
                command_id="cmd-failed",
                command="StartStream",
                ok=False,
                message="device unavailable",
                error_code="OSError",
                failure_kind=CommandFailureKind.EXECUTION_FAILED,
            ))
        )

        resp = api_client.post("/api/v1/acquisition/stream/start")

        assert resp.status_code == 500
        assert resp.json()["code"] == "COMMAND_EXECUTION_FAILED"
        assert resp.json()["detail"]["error_code"] == "OSError"


class TestAcquisitionStatus:
    def test_initial_status(self, api_client):
        resp = api_client.get("/api/v1/acquisition/status")
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["connected"] is False
        assert data["streaming"] is False

    def test_connect_alone_does_not_flip_status(self, api_client, acquisition_service, mock_engine):
        # Projection semantics (option A): the command only dispatches intent;
        # status flips when the worker reports back via AcquisitionState.
        api_client.post("/api/v1/acquisition/connect")
        resp = api_client.get("/api/v1/acquisition/status")
        assert resp.json()["data"]["connected"] is False

    def test_status_reflects_worker_snapshot(self, api_client, acquisition_service, mock_engine):
        from omnibci.application.events import AcquisitionSnapshot
        from omnibci.shared.events import Event, EventType

        api_client.post("/api/v1/acquisition/connect")
        api_client.post("/api/v1/acquisition/stream/start")
        acquisition_service.on_acquisition_state(
            Event(EventType.AcquisitionState,
                  AcquisitionSnapshot(connected=True, streaming=True)))
        resp = api_client.get("/api/v1/acquisition/status")
        data = resp.json()["data"]
        assert data["connected"] is True
        assert data["streaming"] is True


class TestAcquisitionMetadata:
    def test_normalized_descriptor_is_exposed_separately_from_source_text(self, api_client, acquisition_service):
        from omnibci.domain.acquisition_descriptor import AcquisitionDescriptor, ChannelDescriptor
        descriptor = AcquisitionDescriptor("test", 500, (ChannelDescriptor(0, "CH1"),), 1,
                                           connection_id="current")
        raw = '  Vendor reference: <unknown /> & value\r\n{"vendor_field": null}\n'
        acquisition_service.get_metadata = MagicMock(return_value={
            "source_id": "test", "connection_id": "current", "metadata": raw,
            "descriptor": descriptor.to_dict(),
        })
        response = api_client.get("/api/v1/acquisition/metadata")
        assert response.status_code == 200
        result = response.json()["data"]
        assert result["metadata"] == raw
        assert result["descriptor"]["signal_channel_count"] == 1
        assert result["descriptor"]["channels"][0]["channel_id"] == "channel-0"
        assert "original_reference" not in result["descriptor"]

    def test_read_and_explicit_refresh_use_service_metadata(self, api_client, acquisition_service):
        metadata = {"source_id": "test", "connection_id": "current", "descriptor": None,
                    "metadata": "Amplifier\nProtocol version: 1.2\nRecording enabled\n"}
        acquisition_service.get_metadata = MagicMock(return_value=metadata)
        response = api_client.get("/api/v1/acquisition/metadata")
        assert response.status_code == 200 and response.json()["data"] == metadata
        acquisition_service.get_metadata.assert_called_once_with(refresh=False)
        response = api_client.post("/api/v1/acquisition/metadata/refresh")
        assert response.status_code == 200
        acquisition_service.get_metadata.assert_called_with(refresh=True)

    def test_disconnected_metadata_returns_confirmed_rejection(self, api_client, acquisition_service):
        acquisition_service.get_metadata = MagicMock(side_effect=_rejected("GetSourceMetadata"))
        response = api_client.get("/api/v1/acquisition/metadata")
        assert response.status_code == 409
        assert response.json()["detail"]["command"] == "GetSourceMetadata"
