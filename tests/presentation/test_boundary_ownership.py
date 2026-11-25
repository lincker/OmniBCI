"""Presentation owns transport projection and distinguishes invalid input from defects."""
from types import SimpleNamespace
from unittest.mock import Mock

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from omnibci.application.configuration.errors import PluginProfileValidationError
from omnibci.application.events import TrialOutcome
from omnibci.domain.classification import Classification
from omnibci.presentation.dependencies import get_configuration_service
from omnibci.presentation.errors import register_exception_handlers
from omnibci.presentation.routers.plugin_config import router


@pytest.mark.parametrize("error,status,code", [
    (PluginProfileValidationError("invalid input"), 422, "VALIDATION_ERROR"),
    (ValueError("private implementation detail"), 500, "INTERNAL_ERROR"),
])
def test_profile_route_distinguishes_input_validation_from_internal_errors(error, status, code):
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(router)
    service = SimpleNamespace(list_profiles=Mock(side_effect=error))
    app.dependency_overrides[get_configuration_service] = lambda: service
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/config/plugin-profiles")
    assert response.status_code == status
    assert response.json()["code"] == code
    if status == 500:
        assert "private implementation detail" not in response.text


def test_trial_projection_is_application_owned_and_detached():
    trial = TrialOutcome("run", 2, Classification(1, 3, forced=True), "processing", "acquisition")
    value = trial.to_dict()
    assert value["generation"] == "run" and value["trial_number"] == 2
    assert value["processing_revision"] == "processing"
    assert value["acquisition_revision"] == "acquisition"
    assert value["forced"] is True
    value["generation"] = "changed"
    assert trial.to_dict()["generation"] == "run"
