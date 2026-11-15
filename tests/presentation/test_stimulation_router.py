"""
Tests for /api/v1/stimulation endpoints.
"""
from unittest.mock import MagicMock

import pytest

from omnibci.application.events import CommandResult
from omnibci.application.services.command_result_service import CommandRejectedError

@pytest.fixture(autouse=True)
def _running_engine(engine_service):
    engine_service._running = True
    engine_service._accepting_commands = True
    engine_service._engine_thread = MagicMock()
    engine_service._engine_thread.is_alive.return_value = True


class TestStimulationStop:
    @pytest.mark.parametrize("execution_unknown", [False, True])
    def test_delivery_failure_returns_503_with_known_outcome(
        self, api_client, stimulation_service, execution_unknown,
    ):
        from omnibci.application.services.command_result_service import CommandDeliveryError

        stimulation_service.handle_stop_stimulation = MagicMock(side_effect=CommandDeliveryError(
            "stim-stop", "StopStimulation", "Peer connection failed",
            execution_unknown=execution_unknown,
        ))

        response = api_client.post("/api/v1/stimulation/stop")

        assert response.status_code == 503
        assert response.json()["code"] == "PEER_DELIVERY_FAILED"
        assert response.json()["detail"]["execution_unknown"] is execution_unknown

    def test_stop_returns_200(self, api_client, mock_engine):
        resp = api_client.post("/api/v1/stimulation/stop")
        assert resp.status_code == 200

    def test_stop_without_engine_returns_503(self, api_client, engine_service):
        engine_service._engine = None
        resp = api_client.post("/api/v1/stimulation/stop")
        assert resp.status_code == 503


class TestStimulationTrial:
    @pytest.fixture(autouse=True)
    def enabled(self, stimulation_service, engine_service):
        stimulation_service._active_identity = stimulation_service._identity(engine_service.applied_snapshot)

    def test_trial_start_returns_200(self, api_client, mock_engine):
        resp = api_client.post("/api/v1/stimulation/trial/start")
        assert resp.status_code == 200

    def test_trial_start_with_params(self, api_client, mock_engine):
        resp = api_client.post(
            "/api/v1/stimulation/trial/start",
            json={"n_trials": 1, "trial_params": [7]},
        )
        assert resp.status_code == 200

    def test_trial_dispatches_event_with_spec(self, api_client, mock_engine):
        api_client.post(
            "/api/v1/stimulation/trial/start",
            json={"n_trials": 1, "trial_params": [2]},
        )
        from omnibci.shared.events import EventType
        calls = mock_engine.event_bus.put_event.call_args_list
        trial_calls = [c for c in calls if c[0][0].type == EventType.StartStimTrial]
        assert len(trial_calls) == 1
        assert trial_calls[0][0][0].data == {"n_trials": 1, "trial_params": [2]}

    def test_renderer_rejection_is_not_reported_as_started(
        self, api_client, stimulation_service
    ):
        stimulation_service.handle_start_trial = MagicMock(side_effect=
            CommandRejectedError(CommandResult(
                command_id="stim-command",
                command="StartStimTrial",
                ok=False,
                message="stimulation window closed",
                error_code="STIMULATION_WINDOW_CLOSED",
            ))
        )

        resp = api_client.post("/api/v1/stimulation/trial/start")

        assert resp.status_code == 409
        assert resp.json()["code"] == "COMMAND_REJECTED"
        assert resp.json()["detail"]["error_code"] == "STIMULATION_WINDOW_CLOSED"


def test_trial_endpoint_requires_explicit_stimulation_start(api_client, mock_engine):
    response = api_client.post("/api/v1/stimulation/trial/start")
    assert response.status_code == 503
    assert response.json()["message"] == "Start stimulation before starting a trial"
    mock_engine.event_bus.put_event.assert_not_called()
