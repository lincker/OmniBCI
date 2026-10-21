"""Decoded results remain readable without creating an experiment session."""

from datetime import datetime, timezone

from omnibci.presentation.dependencies import get_results_service


def test_results_snapshot_has_engine_generation_and_decoded_trials(api_client):
    class Results:
        def snapshot(self):
            return {"generation": "engine-7", "trials": [{
                "generation": "engine-7", "trial_number": 4, "target_id": 2,
                "event_id": 8, "confidence": 0.9, "forced": False,
                "produced_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
                "config_revision": "configuration-2",
            }]}

    api_client.app.dependency_overrides[get_results_service] = Results
    try:
        response = api_client.get("/api/v1/results")
    finally:
        api_client.app.dependency_overrides.pop(get_results_service, None)
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["generation"] == "engine-7"
    assert data["trials"][0] == {
        "generation": "engine-7", "trial_number": 4, "target_id": 2,
        "event_id": 8, "confidence": 0.9, "forced": False,
        "produced_at": "2026-01-01T00:00:00Z", "config_revision": "configuration-2",
    }


def test_results_are_empty_before_engine_start(api_client):
    response = api_client.get("/api/v1/results")
    assert response.status_code == 200
    assert response.json()["data"] == {"generation": None, "trials": []}


def test_removed_lifecycle_routes_are_not_exposed(api_client):
    for path in ("/api/v1/sessions", "/api/v1/recording/start", "/api/v1/recording/stop"):
        assert api_client.post(path).status_code == 404
