"""
REST contract freeze.

Pins the OpenAPI surface so an unintended change to an endpoint or a response
DTO fails CI rather than silently breaking the frontend (which generates its
TypeScript types from this spec).

If a change here is *intentional*, update the expectations in the same commit —
that's the signal that the published contract changed.
"""
from __future__ import annotations

import pytest

from omnibci.presentation.main import create_app


@pytest.fixture(scope="module")
def spec():
    return create_app().openapi()


# ---------------------------------------------------------------------------
# Frozen set of (path, method) — the published REST surface.
# ---------------------------------------------------------------------------

EXPECTED_OPERATIONS = {
    ("/healthz", "get"),
    # engine
    ("/api/v1/engine/start", "post"),
    ("/api/v1/engine/stop", "post"),
    ("/api/v1/engine/status", "get"),
    # acquisition
    ("/api/v1/acquisition/connect", "post"),
    ("/api/v1/acquisition/disconnect", "post"),
    ("/api/v1/acquisition/stream/start", "post"),
    ("/api/v1/acquisition/stream/stop", "post"),
    ("/api/v1/acquisition/status", "get"),
    # stimulation
    ("/api/v1/stimulation/status", "get"),
    # plugins
    ("/api/v1/plugins/{category}", "get"),
    ("/api/v1/stimulation/paradigms", "get"),
    ("/api/v1/stimulation/start", "post"),
    ("/api/v1/stimulation/stop", "post"),
    ("/api/v1/stimulation/trial/start", "post"),
    # sessions
    ("/api/v1/sessions", "post"),
    ("/api/v1/sessions", "get"),
    ("/api/v1/sessions/active", "get"),
    ("/api/v1/sessions/{session_id}", "get"),
    ("/api/v1/sessions/{session_id}", "delete"),
    ("/api/v1/sessions/{session_id}/trials", "get"),
    ("/api/v1/sessions/{session_id}/trials/last", "get"),
    # recording (session + stream orchestration)
    ("/api/v1/recording/start", "post"),
    ("/api/v1/recording/stop", "post"),
    # config (stage 4: defaults + named profiles)
    ("/api/v1/config", "get"),
    ("/api/v1/config", "patch"),
    ("/api/v1/config/profiles", "get"),
    ("/api/v1/config/profiles/{name}", "get"),
    ("/api/v1/config/profiles/{name}", "put"),
    ("/api/v1/config/profiles/{name}", "delete"),
    ("/api/v1/config/profiles/{name}/activate", "post"),
    ("/api/v1/config/profiles/deactivate", "post"),
}


def _operations(spec) -> set:
    ops = set()
    for path, methods in spec["paths"].items():
        for method in methods:
            if method in {"get", "post", "put", "patch", "delete"}:
                ops.add((path, method))
    return ops


class TestOperationSurface:
    def test_exact_operation_set(self, spec):
        assert _operations(spec) == EXPECTED_OPERATIONS

    def test_api_version_prefix(self, spec):
        api_paths = [p for p in spec["paths"] if p.startswith("/api/")]
        assert api_paths and all(p.startswith("/api/v1/") for p in api_paths)

    @pytest.mark.parametrize(
        "path",
        [
            "/api/v1/acquisition/connect",
            "/api/v1/acquisition/disconnect",
            "/api/v1/acquisition/stream/start",
            "/api/v1/acquisition/stream/stop",
        ],
    )
    def test_acquisition_commands_are_accepted_asynchronously(self, spec, path):
        responses = spec["paths"][path]["post"]["responses"]
        assert "202" in responses
        assert "200" not in responses


# ---------------------------------------------------------------------------
# Response DTO freeze — field names + required-ness.
# ---------------------------------------------------------------------------

def _schema(spec, name):
    schemas = spec["components"]["schemas"]
    assert name in schemas, f"missing component schema: {name}"
    return schemas[name]


class TestResponseDtos:
    def test_session_response_fields(self, spec):
        props = _schema(spec, "SessionResponse")["properties"]
        assert set(props) == {
            "session_id", "paradigm", "state", "trial_count", "started_at", "ended_at",
        }

    def test_trial_response_fields(self, spec):
        props = _schema(spec, "TrialResponse")["properties"]
        assert set(props) == {
            "trial_number", "target_id", "event_id", "confidence", "forced", "produced_at",
        }

    def test_engine_status_fields(self, spec):
        props = _schema(spec, "EngineStatusResponse")["properties"]
        assert set(props) == {"running", "acquisition_alive", "processor_alive", "stimulation_connected"}

    def test_acquisition_status_fields(self, spec):
        props = _schema(spec, "AcquisitionStatusResponse")["properties"]
        assert set(props) == {
            "connected", "streaming", "source", "sample_rate", "channel_count",
        }


# ---------------------------------------------------------------------------
# Unified error envelope is published.
# ---------------------------------------------------------------------------

class TestErrorContract:
    def test_error_response_schema_present(self, spec):
        props = _schema(spec, "ErrorResponse")["properties"]
        assert {"success", "code", "message", "detail"} <= set(props)

    def test_error_code_enum_values(self, spec):
        # ErrorCode is referenced by ErrorResponse.code; its enum must be stable.
        code = _schema(spec, "ErrorCode")
        assert set(code["enum"]) == {
            # generic
            "BAD_REQUEST", "UNAUTHORIZED", "FORBIDDEN", "NOT_FOUND", "CONFLICT",
            "VALIDATION_ERROR", "SERVICE_UNAVAILABLE", "INTERNAL_ERROR", "ERROR",
            # domain-specific
            "ENGINE_NOT_READY", "SESSION_NOT_FOUND", "NO_ACTIVE_SESSION", "NO_TRIALS",
            "ALREADY_RECORDING", "NO_ACTIVE_RECORDING",
            # layered configuration (stage 4)
            "CONFIG_KEY_NOT_ALLOWED", "CONFIG_PROFILE_NOT_FOUND", "CONFIG_PROFILE_IN_USE",
        }

    def test_endpoints_document_error_responses(self, spec):
        # A representative endpoint advertises the unified error envelope.
        responses = spec["paths"]["/api/v1/sessions/{session_id}"]["get"]["responses"]
        assert "404" in responses
