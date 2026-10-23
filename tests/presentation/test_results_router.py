"""Decoded results remain readable without creating an experiment session."""

from datetime import datetime, timezone

import pytest

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


def test_epoch_page_includes_unclassified_processing_records(api_client):
    class Results:
        def epochs(self, *, offset, limit):
            assert (offset, limit) == (25, 25)
            return {"generation": "run", "total": 26, "offset": offset, "limit": limit, "epochs": [{
                "generation": "run", "epoch_number": 1, "event_id": 8,
                "config_revision": "revision", "status": "no_result", "reason": "end_marker",
                "sample_rate": 1000, "channel_count": 9, "sample_count": 20,
                "pre_trigger_samples": 5, "trial_number": None, "result": None, "error": None,
                "started_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
                "completed_at": datetime(2026, 1, 1, 0, 0, 1, tzinfo=timezone.utc),
            }]}

    api_client.app.dependency_overrides[get_results_service] = Results
    try:
        response = api_client.get("/api/v1/results/epochs?offset=25&limit=25")
    finally:
        api_client.app.dependency_overrides.pop(get_results_service, None)
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["total"] == 26
    assert data["epochs"][0]["status"] == "no_result"
    assert data["epochs"][0]["result"] is None
    assert data["epochs"][0]["sample_count"] == 20
    assert "data" not in data["epochs"][0]


def test_epoch_page_is_empty_before_engine_start(api_client):
    response = api_client.get("/api/v1/results/epochs")
    assert response.status_code == 200
    assert response.json()["data"] == {"generation": None, "total": 0, "offset": 0, "limit": 25, "epochs": []}


@pytest.mark.parametrize("query", ["offset=-1", "limit=0", "limit=101"])
def test_epoch_page_rejects_invalid_selection(api_client, query):
    assert api_client.get(f"/api/v1/results/epochs?{query}").status_code == 422
