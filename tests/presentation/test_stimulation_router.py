"""
Tests for /api/v1/stimulation endpoints.
"""
from unittest.mock import patch

import pytest

from omnibci.presentation.schemas.stimulation import ParadigmInfo

_FAKE_PARADIGM = ParadigmInfo(
    id="classic40SSVEP",
    plugin_type="STIMULATION_THUBCI_SSVEP_classic40",
    display_name="Classic 40-target SSVEP",
    source="builtin",
)

_PATCH_TARGET = "omnibci.infrastructure.stimulation.paradigm_discovery.list_paradigms"


class TestParadigms:
    @pytest.fixture(autouse=True)
    def mock_list_paradigms(self):
        with patch(_PATCH_TARGET, return_value=[_FAKE_PARADIGM]):
            yield

    def test_list_paradigms_returns_200(self, api_client):
        resp = api_client.get("/api/v1/stimulation/paradigms")
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert "paradigms" in data
        assert len(data["paradigms"]) >= 1

    def test_paradigm_has_name_field(self, api_client):
        resp = api_client.get("/api/v1/stimulation/paradigms")
        paradigm = resp.json()["data"]["paradigms"][0]
        assert "name" in paradigm

    def test_builtin_paradigm_present(self, api_client):
        resp = api_client.get("/api/v1/stimulation/paradigms")
        plugin_types = [p["plugin_type"] for p in resp.json()["data"]["paradigms"]]
        assert "STIMULATION_THUBCI_SSVEP_classic40" in plugin_types


class TestStimulationStart:
    def test_start_returns_200(self, api_client, mock_engine):
        resp = api_client.post("/api/v1/stimulation/start")
        assert resp.status_code == 200
        assert resp.json()["success"] is True

    def test_start_with_paradigm_body(self, api_client, mock_engine):
        resp = api_client.post(
            "/api/v1/stimulation/start",
            json={"paradigm": "classic40SSVEP"},
        )
        assert resp.status_code == 200

    def test_start_dispatches_event(self, api_client, mock_engine):
        api_client.post("/api/v1/stimulation/start")
        mock_engine.event_bus.put_event.assert_called()

    def test_start_without_engine_returns_503(self, api_client, engine_service):
        engine_service._engine = None
        resp = api_client.post("/api/v1/stimulation/start")
        assert resp.status_code == 503


class TestStimulationStop:
    def test_stop_returns_200(self, api_client, mock_engine):
        resp = api_client.post("/api/v1/stimulation/stop")
        assert resp.status_code == 200

    def test_stop_without_engine_returns_503(self, api_client, engine_service):
        engine_service._engine = None
        resp = api_client.post("/api/v1/stimulation/stop")
        assert resp.status_code == 503


class TestStimulationTrial:
    def test_trial_start_returns_200(self, api_client, mock_engine):
        resp = api_client.post("/api/v1/stimulation/trial/start")
        assert resp.status_code == 200

    def test_trial_start_with_target(self, api_client, mock_engine):
        resp = api_client.post(
            "/api/v1/stimulation/trial/start",
            json={"target_index": 7},
        )
        assert resp.status_code == 200

    def test_trial_dispatches_event_with_target(self, api_client, mock_engine):
        api_client.post("/api/v1/stimulation/trial/start", json={"target_index": 2})
        from omnibci.shared.events import EventType
        calls = mock_engine.event_bus.put_event.call_args_list
        trial_calls = [c for c in calls if c[0][0].type == EventType.StartStimTrial]
        assert len(trial_calls) == 1
        assert trial_calls[0][0][0].data == 2
