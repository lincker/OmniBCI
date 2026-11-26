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

        stimulation_service.stop = MagicMock(side_effect=CommandDeliveryError(
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

    def test_stop_without_engine_is_idempotent(self, api_client, engine_service):
        engine_service._engine = None
        resp = api_client.post("/api/v1/stimulation/stop")
        assert resp.status_code == 200


class TestStimulationTrial:
    @pytest.fixture(autouse=True)
    def enabled(self, stimulation_service, stimulation_host, stimulation_snapshot):
        from omnibci.application.commands.stimulation_commands import StartStimulationCommand
        stimulation_host.is_peer_connected.return_value = True
        stimulation_service.handle_start_stimulation(StartStimulationCommand(), settings_snapshot=stimulation_snapshot)

    def test_trial_start_returns_200(self, api_client, mock_engine):
        resp = api_client.post("/api/v1/stimulation/trial/start", json={"trial_params": [{"event_id": 0}]})
        assert resp.status_code == 200

    def test_trial_start_with_params(self, api_client, mock_engine):
        resp = api_client.post(
            "/api/v1/stimulation/trial/start",
            json={"n_trials": 1, "trial_params": [{"event_id": 7}]},
        )
        assert resp.status_code == 200

    def test_trial_dispatches_event_with_spec(self, api_client, stimulation_host):
        api_client.post(
            "/api/v1/stimulation/trial/start",
            json={"n_trials": 1, "trial_params": [{"event_id": 2}]},
        )
        from omnibci.shared.events import EventType
        calls = stimulation_host.dispatch.call_args_list
        trial_calls = [c for c in calls if c[0][0].type == EventType.StartStimTrial]
        assert len(trial_calls) == 1
        assert trial_calls[0][0][0].data == {"n_trials": 1, "trial_params": [{"event_id": 2}]}

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

        resp = api_client.post("/api/v1/stimulation/trial/start", json={"trial_params": [{"event_id": 0}]})

        assert resp.status_code == 409
        assert resp.json()["code"] == "COMMAND_REJECTED"
        assert resp.json()["detail"]["error_code"] == "STIMULATION_WINDOW_CLOSED"


def test_trial_endpoint_requires_explicit_stimulation_start(api_client, mock_engine):
    response = api_client.post("/api/v1/stimulation/trial/start", json={"trial_params": [{"event_id": 0}]})
    assert response.status_code == 503
    assert response.json()["message"] == "Start stimulation before starting a trial"
    mock_engine.dispatch.assert_not_called()


from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from omnibci.presentation.dependencies import get_runtime_configuration


def test_stimulation_start_uses_single_server_operation(api_client):
    operation_id = "00000000-0000-0000-0000-000000000001"
    configuration = {"expected_revision": "previous", "settings": {}, "bindings": []}
    result = {
        "module": "stimulation", "affected_modules": ["stimulation"], "operation_id": operation_id,
        "plan_id": "internal", "target_revisions": {"stimulation": "current"},
        "status": "succeeded", "stage": "complete", "error": None, "rolled_back": False,
        "result": {"configuration_applied": True, "action": "started"},
    }
    runtime = SimpleNamespace(execute_current=AsyncMock(return_value=result))
    api_client.app.dependency_overrides[get_runtime_configuration] = lambda: runtime
    try:
        response = api_client.post("/api/v1/stimulation/start", json={
            "operation_id": operation_id, "configuration": configuration,
        })
    finally:
        api_client.app.dependency_overrides.pop(get_runtime_configuration, None)
    assert response.status_code == 200
    assert response.json()["data"] == result
    runtime.execute_current.assert_awaited_once_with(
        "stimulation", "start", operation_id=operation_id, configuration=configuration,
    )


def test_stimulation_start_requires_configuration(api_client):
    api_client.app.dependency_overrides[get_runtime_configuration] = lambda: SimpleNamespace()
    try:
        assert api_client.post("/api/v1/stimulation/start").status_code == 422
    finally:
        api_client.app.dependency_overrides.pop(get_runtime_configuration, None)


def test_stimulation_prepare_targets_only_stimulation_module(api_client, stimulation_service):
    with patch.object(stimulation_service, "prepare_external") as operation:
        response = api_client.post("/api/v1/stimulation/prepare")
    assert response.status_code == 200
    operation.assert_called_once_with()


def test_stimulation_stop_targets_only_stimulation_module(api_client, stimulation_service):
    with patch.object(stimulation_service, "stop") as operation:
        response = api_client.post("/api/v1/stimulation/stop")
    assert response.status_code == 200
    operation.assert_called_once_with()
