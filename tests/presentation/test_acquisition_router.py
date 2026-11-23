"""
Tests for /api/v1/acquisition endpoints.
"""
from copy import deepcopy
from omnibci.application.source_metadata import source_descriptor
from omnibci.domain.ports.acquisition import SourceObservation
import pytest
from unittest.mock import AsyncMock, MagicMock

from omnibci.application.events import CommandFailureKind, CommandResult
from omnibci.application.services.command_result_service import (
    CommandExecutionError,
    CommandRejectedError,
    CommandTimeoutError,
)

READ_IDENTITY = {"expected_revision": "source-revision", "generation": "engine-generation", "connection_id": "connection"}


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


    def test_disconnect_returns_confirmed_success(self, api_client, mock_engine):
        resp = api_client.post("/api/v1/acquisition/disconnect")
        assert resp.status_code == 200
        assert resp.json()["message"] == "Data provider disconnected"



    def test_disconnect_stops_only_acquisition_and_clears_its_projection(
        self, api_client, mock_engine, acquisition_service,
    ):
        mock_engine.acquisition_worker.is_alive.return_value = True
        mock_engine.signal_processing_worker.is_alive.return_value = True
        acquisition_service._connected = True
        response = api_client.post("/api/v1/acquisition/disconnect")
        assert response.status_code == 200
        mock_engine.stop_module.assert_awaited_once_with("acquisition")
        assert not acquisition_service._connected
        assert mock_engine.signal_processing_worker.is_alive()

    def test_disconnect_recovers_an_already_exited_acquisition(
        self, api_client, engine_service, mock_engine,
    ):
        mock_engine.acquisition_worker.is_alive.return_value = False
        engine_service.admission.quarantine("source exited", "acquisition")
        response = api_client.post("/api/v1/acquisition/disconnect")
        assert response.status_code == 200
        assert not engine_service.admission.requires_recovery("acquisition")
        mock_engine.stop_module.assert_awaited_once_with("acquisition")

    def test_disconnect_failure_still_releases_acquisition_worker(
        self, api_client, acquisition_service, mock_engine,
    ):
        mock_engine.acquisition_worker.is_alive.return_value = True
        acquisition_service._connected = True
        acquisition_service.handle_disconnect = MagicMock(side_effect=_rejected("DisconnectDataProvider"))
        response = api_client.post("/api/v1/acquisition/disconnect")
        assert response.status_code == 409
        mock_engine.stop_module.assert_awaited_once_with("acquisition")
        assert not acquisition_service._connected


class TestAcquisitionStream:
    @pytest.fixture(autouse=True)
    def connected_source(self, engine_service, acquisition_service):
        engine_service._acquisition = acquisition_service
        engine_service._applied_snapshot = {
            "CONFIG.generation": READ_IDENTITY["generation"],
            "CONFIG.module_revisions": {"acquisition": READ_IDENTITY["expected_revision"]},
        }
        acquisition_service._connected = True
        acquisition_service._connection_id = READ_IDENTITY["connection_id"]

    def test_start_stream_returns_confirmed_success(self, api_client, mock_engine):
        resp = api_client.post("/api/v1/acquisition/stream/start", json=READ_IDENTITY)
        assert resp.status_code == 200
        assert resp.json()["success"] is True
        assert resp.json()["message"] == "Stream started"

    def test_stop_stream_returns_confirmed_success(self, api_client, mock_engine):
        resp = api_client.post("/api/v1/acquisition/stream/stop")
        assert resp.status_code == 200
        assert resp.json()["message"] == "Stream stopped"

    def test_start_stream_without_engine_returns_503(self, api_client, engine_service):
        engine_service._engine = None
        resp = api_client.post("/api/v1/acquisition/stream/start", json=READ_IDENTITY)
        assert resp.status_code == 503

    def test_worker_rejection_returns_409_with_command_detail(
        self, api_client, acquisition_service
    ):
        acquisition_service.handle_start_stream = MagicMock(
            side_effect=_rejected()
        )

        resp = api_client.post("/api/v1/acquisition/stream/start", json=READ_IDENTITY)

        assert resp.status_code == 409
        assert resp.json()["code"] == "COMMAND_REJECTED"
        assert resp.json()["detail"] == {
            "command_id": "cmd-rejected",
            "command": "StartStream",
            "error_code": "DATA_SOURCE_NOT_CONNECTED",
        }

    def test_source_continuity_failure_returns_409_without_preparing_processing(
        self, api_client, acquisition_service, command_executor, monkeypatch
    ):
        from omnibci.shared.events import EventType

        message = (
            "Acquisition lost stream continuity. Stop and restart the engine, "
            "then connect the source before reading again. "
            "The current stream cannot be resumed or checkpointed (ConnectionResetError)"
        )
        request = MagicMock(side_effect=CommandRejectedError(CommandResult(
            command_id="cmd-continuity-lost",
            command="StartStream",
            ok=False,
            message=message,
            error_code="ACQUISITION_STREAM_CONTINUITY_LOST",
            failure_kind=CommandFailureKind.REJECTED,
        )))
        monkeypatch.setattr(command_executor, "request", request)
        prepare = MagicMock()
        acquisition_service.set_signal_preparer(prepare)

        response = api_client.post("/api/v1/acquisition/stream/start", json=READ_IDENTITY)

        assert response.status_code == 409
        assert response.json()["code"] == "COMMAND_REJECTED"
        assert response.json()["message"] == message
        assert response.json()["detail"] == {
            "command_id": "cmd-continuity-lost",
            "command": "StartStream",
            "error_code": "ACQUISITION_STREAM_CONTINUITY_LOST",
        }
        assert [item.args[0] for item in request.call_args_list] == [
            EventType.GetSourceMetadata, EventType.StartStream,
        ]
        prepare.assert_not_called()

    def test_missing_terminal_result_returns_504(
        self, api_client, acquisition_service
    ):
        acquisition_service.handle_start_stream = MagicMock(side_effect=
            CommandTimeoutError("cmd-timeout", "StartStream", 0.25)
        )

        resp = api_client.post("/api/v1/acquisition/stream/start", json=READ_IDENTITY)

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

        resp = api_client.post("/api/v1/acquisition/stream/start", json=READ_IDENTITY)

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


    def test_status_reflects_worker_snapshot(self, api_client, acquisition_service, mock_engine):
        from omnibci.application.events import AcquisitionSnapshot
        from omnibci.shared.events import Event, EventType

        acquisition_service.on_acquisition_state(
            Event(EventType.AcquisitionState,
                  AcquisitionSnapshot(connected=True, streaming=True)))
        resp = api_client.get("/api/v1/acquisition/status")
        data = resp.json()["data"]
        assert data["connected"] is True
        assert data["streaming"] is True

    def test_disconnect_and_reconnect_do_not_expose_previous_connection_metadata(
        self, api_client, acquisition_service, command_executor, monkeypatch
    ):
        from copy import deepcopy
        from omnibci.application.events import AcquisitionSnapshot
        from omnibci.application.plugin_management.identity import PluginSelection
        from omnibci.application.configuration.models import ProfileBinding
        from omnibci.domain.signal_info import ChannelDescriptor, SignalInfo
        from omnibci.shared.events import Event, EventType

        binding = ProfileBinding(
            "acquisition.source", "acquisition",
            "00000000-0000-0000-0000-000000000001",
            PluginSelection("test.source", "default", "1"),
            {"sample_rate": 250, "channel_count": 2}, profile_revision=1,
        ).to_dict()
        saved_binding = deepcopy(binding)
        acquisition_service.set_metadata_binding(lambda: binding)
        descriptor = source_descriptor(SourceObservation(native_text="native", signal_info=SignalInfo(sample_rate=250, channels=(ChannelDescriptor(0, "C3", unit="uV", channel_id="channel-0"),)), native_event="final-row", provenance="device-reported"), source_id="test", connection_id="previous")
        metadata_result = CommandResult("read", "GetSourceMetadata", True, data={
            "source_id": "test", "connection_id": "previous",
            "metadata": "Amplifier metadata from previous connection",
            "descriptor": deepcopy(descriptor), "descriptor_error": None,
        })
        request = MagicMock(return_value=metadata_result)
        monkeypatch.setattr(command_executor, "request", request)
        acquisition_service.on_acquisition_state(Event(
            EventType.AcquisitionState, AcquisitionSnapshot(True, False, "previous"),
        ))
        assert api_client.get("/api/v1/acquisition/metadata").status_code == 200
        acquisition_service.on_acquisition_state(Event(
            EventType.AcquisitionState, AcquisitionSnapshot(True, True, "previous"),
        ))
        before = api_client.get("/api/v1/acquisition/status").json()["data"]
        assert before["connected"] is True and before["streaming"] is True
        assert before["metadata_available"] is True
        assert before["sample_rate"] == 250 and before["channel_count"] == 1
        assert before["metadata_provenance"] == "device-reported"

        for snapshot in (
            AcquisitionSnapshot(False, False, None),
            AcquisitionSnapshot(True, False, "reconnected"),
        ):
            acquisition_service.on_acquisition_state(Event(EventType.AcquisitionState, snapshot))
            response = api_client.get("/api/v1/acquisition/status")
            assert response.status_code == 200
            status = response.json()["data"]
            assert status["connected"] is snapshot.connected
            assert status["streaming"] is False
            assert status["connection_id"] == snapshot.connection_id
            assert status["metadata_available"] is False
            assert status["sample_rate"] is None
            assert status["channel_count"] is None
            assert status["metadata_provenance"] is None
            assert acquisition_service.cached_descriptor_for(binding) is None
        request.assert_called_once()

        # Only metadata read from the new connection may restore its dimensions.
        replacement = source_descriptor(SourceObservation(native_text="native", signal_info=SignalInfo(sample_rate=500, channels=(ChannelDescriptor(0, "C3", unit="uV", channel_id="channel-0"),)), native_event="final-row", provenance="device-reported"), source_id="test", connection_id="reconnected")
        request.return_value = CommandResult("read-new", "GetSourceMetadata", True, data={
            "source_id": "test", "connection_id": "reconnected",
            "metadata": "Amplifier metadata from new connection",
            "descriptor": deepcopy(replacement), "descriptor_error": None,
        })
        response = api_client.get("/api/v1/acquisition/metadata")
        assert response.status_code == 200
        assert response.json()["data"]["descriptor"]["connection_id"] == "reconnected"
        assert response.json()["data"]["descriptor"]["sample_rate"] == 500
        assert acquisition_service.cached_descriptor_for(binding) == deepcopy(replacement)
        after = api_client.get("/api/v1/acquisition/status").json()["data"]
        assert after["connection_id"] == "reconnected"
        assert after["metadata_available"] is True
        assert after["sample_rate"] == 500
        assert binding == saved_binding


class TestAcquisitionMetadata:
    def test_normalized_descriptor_is_exposed_separately_from_source_text(self, api_client, acquisition_service):
        from omnibci.domain.signal_info import SignalInfo, ChannelDescriptor
        descriptor = source_descriptor(SourceObservation(native_text="native", signal_info=SignalInfo(sample_rate=500, channels=(ChannelDescriptor(0, "CH1", channel_id="channel-0"),)), native_event="final-row", provenance="protocol-defined"), source_id="test", connection_id="current")
        raw = '  Vendor reference: <unknown /> & value\r\n{"vendor_field": null}\n'
        acquisition_service.get_metadata = MagicMock(return_value={
            "source_id": "test", "connection_id": "current", "metadata": raw,
            "descriptor": deepcopy(descriptor),
            "comparison": {"status": "mismatched", "sample_rate": {"expected": 250, "actual": 500},
                           "channel_count": {"expected": 3, "actual": 2}},
        })
        response = api_client.get("/api/v1/acquisition/metadata")
        assert response.status_code == 200
        result = response.json()["data"]
        assert result["metadata"] == raw
        assert result["comparison"]["status"] == "mismatched"
        assert result["comparison"]["channel_count"] == {"expected": 3, "actual": 2}
        assert result["descriptor"]["signal_channel_count"] == 1
        assert result["descriptor"]["channels"][0]["channel_id"] == "channel-0"
        assert "original_reference" not in result["descriptor"]

    def test_read_and_explicit_refresh_use_service_metadata(self, api_client, acquisition_service):
        metadata = {"source_id": "test", "connection_id": "current", "descriptor": None,
                    "metadata": "Amplifier\nProtocol version: 1.2\nRecording enabled\n", "comparison": None}
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
