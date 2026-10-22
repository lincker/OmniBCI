"""REST preview contracts use finite selections and confirmed capture identity."""
from unittest.mock import MagicMock

import pytest

from omnibci.application.acquisition_preview import PreviewSelection, limits
from omnibci.application.events import CommandResult
from omnibci.application.plugin_profiles import ProfileRevisionConflict
from omnibci.application.services.command_result_service import CommandRejectedError, CommandTimeoutError


@pytest.fixture
def preview_service(acquisition_service):
    state = {"capture_id": "capture", "generation": "generation", "config_revision": "a" * 64,
             "source_id": "source", "connection_id": "connection", "status": "finished", "reason": "timeout",
             "selection": PreviewSelection().to_dict(), "observed": 1, "enqueued": 1, "dropped": 0,
             "oversized": 0, "enqueued_bytes": 128, "limits": limits(),
             "stream_chunk_count": 3, "stream_sample_count": 384, "last_sequence": 2,
             "available": True, "cache_errors": 0, "received": 0, "retained_bytes": 0, "chunks": []}
    for method in ("start_preview", "stop_preview", "get_preview"):
        setattr(acquisition_service, method, MagicMock(return_value=state))
    acquisition_service.get_preview_chunk = MagicMock(return_value={
        "capture_id": "capture", "generation": "generation", "config_revision": "a" * 64,
        "source_id": "source", "connection_id": "connection", "sequence_id": 7,
        "timestamp_ns": "1780000000000000001", "original_shape": [3, 5], "dtype": "float64",
        "row_indices": [1, 2], "sample_start": 1, "sample_count": 3, "trigger_row_index": 2,
        "values": [[1.25, "NaN", "Infinity"], ["9007199254740993", "-Infinity", True]], "descriptor": None,
    })
    return acquisition_service


def start_body(**changes):
    return {"generation": "generation", "expected_revision": "a" * 64, "connection_id": "connection", **changes}


def test_start_uses_default_bounded_selection(api_client, preview_service):
    response = api_client.post("/api/v1/acquisition/preview/start", json=start_body())
    assert response.status_code == 200
    preview_service.start_preview.assert_called_once_with(**start_body(), selection=PreviewSelection().to_dict())
    assert response.json()["data"]["limits"]["max_chunks"] == 32


@pytest.mark.parametrize("changes", [
    {"generation": ""}, {"connection_id": ""}, {"expected_revision": "stale"}, {"unexpected": True},
    {"selection": {"rows": [True]}}, {"selection": {"rows": [0, 0]}},
    {"selection": {"rows": list(range(17))}}, {"selection": {"sample_count": 513}},
    {"selection": {"rows": [], "include_trigger": False}}, {"selection": {"sample_start": -1}},
    {"selection": {"rows": list(range(16)), "sample_count": 512}}, {"selection": {"chunk_count": 33}},
])
def test_invalid_start_input_returns_validation_envelope(api_client, preview_service, changes):
    response = api_client.post("/api/v1/acquisition/preview/start", json=start_body(**changes))
    assert response.status_code == 422
    assert response.json()["code"] == "VALIDATION_ERROR"
    preview_service.start_preview.assert_not_called()


def test_stop_only_calls_preview_use_case(api_client, preview_service):
    response = api_client.post("/api/v1/acquisition/preview/stop", json={"generation": "generation", "capture_id": "capture"})
    assert response.status_code == 200
    preview_service.stop_preview.assert_called_once_with(generation="generation", capture_id="capture")


def test_capture_timeout_is_data_not_http_timeout(api_client, preview_service):
    response = api_client.get("/api/v1/acquisition/preview", params={"generation": "generation"})
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["reason"] == "timeout" and data["received"] == 0 and data["enqueued"] == 1
    preview_service.get_preview.assert_called_once_with(generation="generation", capture_id=None)


def test_missing_generation_is_invalid(api_client, preview_service):
    response = api_client.get("/api/v1/acquisition/preview")
    assert response.status_code == 422
    preview_service.get_preview.assert_not_called()


def test_detail_keeps_real_rows_samples_and_exact_value_strings(api_client, preview_service):
    response = api_client.get("/api/v1/acquisition/preview/chunks/7", params={"generation": "generation", "capture_id": "capture"})
    assert response.status_code == 200
    data = response.json()["data"]
    assert data == preview_service.get_preview_chunk.return_value
    assert data["values"][1][2] is True
    preview_service.get_preview_chunk.assert_called_once_with(7, generation="generation", capture_id="capture")


@pytest.mark.parametrize("path,params", [
    ("/api/v1/acquisition/preview/chunks/-1", {"generation": "generation", "capture_id": "capture"}),
    ("/api/v1/acquisition/preview/chunks/1", {"generation": "generation"}),
    ("/api/v1/acquisition/preview/chunks/1", {"generation": "generation", "capture_id": ""}),
])
def test_detail_requires_nonnegative_sequence_and_complete_identity(api_client, preview_service, path, params):
    assert api_client.get(path, params=params).status_code == 422
    preview_service.get_preview_chunk.assert_not_called()


@pytest.mark.parametrize("error,expected,code", [
    (LookupError("Preview chunk is not retained"), 404, "NOT_FOUND"),
    (ProfileRevisionConflict("Runtime generation changed"), 409, "CONFLICT"),
    (RuntimeError("Engine is not ready"), 503, "ENGINE_NOT_READY"),
    (CommandTimeoutError("read", "GetAcquisitionPreview", 5), 504, "COMMAND_TIMEOUT"),
    (CommandRejectedError(CommandResult("read", "GetAcquisitionPreview", False,
                                       message="Capture changed", error_code="PREVIEW_REJECTED")), 409, "COMMAND_REJECTED"),
])
def test_detail_errors_follow_existing_api_conventions(api_client, preview_service, error, expected, code):
    preview_service.get_preview_chunk.side_effect = error
    response = api_client.get("/api/v1/acquisition/preview/chunks/7", params={"generation": "generation", "capture_id": "capture"})
    assert response.status_code == expected
    assert response.json()["code"] == code


def test_openapi_publishes_bounds_and_confirmation_timeouts(api_client):
    spec = api_client.get("/openapi.json").json()
    for path, method in (("/preview/start", "post"), ("/preview/stop", "post"),
                         ("/preview", "get"), ("/preview/chunks/{sequence_id}", "get")):
        assert "504" in spec["paths"]["/api/v1/acquisition" + path][method]["responses"]
    selection = spec["components"]["schemas"]["AcquisitionPreviewSelectionModel"]
    assert selection["additionalProperties"] is False
    assert selection["properties"]["rows"]["maxItems"] == 16
    assert selection["properties"]["sample_count"]["maximum"] == 512
