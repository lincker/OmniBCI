"""
Tests for the unified error envelope + ErrorCode (frontend-separation step 1).

Pins:
  - every failure returns ErrorResponse{success:false, code, message, detail?};
  - HTTP status → ErrorCode mapping;
  - 404 / 422 paths through the real app produce the unified shape.
"""
from __future__ import annotations

import pytest

from omnibci.presentation.schemas.common import ErrorCode, ErrorResponse
from omnibci.presentation.errors import status_to_code, ApiError


# ---------------------------------------------------------------------------
# Pure unit — no FastAPI needed
# ---------------------------------------------------------------------------

class TestStatusToCode:
    @pytest.mark.parametrize(
        "status,expected",
        [
            (400, ErrorCode.BAD_REQUEST),
            (401, ErrorCode.UNAUTHORIZED),
            (403, ErrorCode.FORBIDDEN),
            (404, ErrorCode.NOT_FOUND),
            (409, ErrorCode.CONFLICT),
            (422, ErrorCode.VALIDATION_ERROR),
            (500, ErrorCode.INTERNAL_ERROR),
            (503, ErrorCode.SERVICE_UNAVAILABLE),
            (504, ErrorCode.GATEWAY_TIMEOUT),
            (418, ErrorCode.ERROR),  # unmapped → generic
        ],
    )
    def test_mapping(self, status, expected):
        assert status_to_code(status) == expected


class TestErrorResponseModel:
    def test_defaults_and_shape(self):
        body = ErrorResponse(code=ErrorCode.NOT_FOUND, message="x")
        dumped = body.model_dump(mode="json")
        assert dumped["success"] is False
        assert dumped["code"] == "NOT_FOUND"
        assert dumped["message"] == "x"
        assert dumped["detail"] is None


class TestApiError:
    def test_carries_fields(self):
        err = ApiError(409, ErrorCode.CONFLICT, "Engine already running", detail={"x": 1})
        assert err.status_code == 409
        assert err.code == ErrorCode.CONFLICT
        assert err.message == "Engine already running"
        assert err.detail == {"x": 1}


# ---------------------------------------------------------------------------
# Through the real app (requires FastAPI; uses the api_client fixture)
# ---------------------------------------------------------------------------

class TestUnifiedErrorThroughApp:
    def test_404_uses_unified_envelope(self, api_client):
        resp = api_client.get("/api/v1/does-not-exist")
        assert resp.status_code == 404
        body = resp.json()
        assert body["success"] is False
        assert body["code"] == "NOT_FOUND"
        assert isinstance(body["message"], str) and body["message"]



    def test_422_validation_uses_unified_envelope(self, api_client):
        resp = api_client.patch("/api/v1/config", json={"updates": []})
        assert resp.status_code == 422
        body = resp.json()
        assert body["success"] is False
        assert body["code"] == "VALIDATION_ERROR"
        assert isinstance(body["detail"], list)  # pydantic error list preserved
