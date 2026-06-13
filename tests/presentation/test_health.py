"""
Tests for /healthz and basic app wiring.
"""


class TestHealth:
    def test_healthz_returns_200(self, api_client):
        resp = api_client.get("/healthz")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"

    def test_openapi_schema_loads(self, api_client):
        resp = api_client.get("/openapi.json")
        assert resp.status_code == 200
        data = resp.json()
        assert "paths" in data
        # All three domains are present
        paths = data["paths"]
        assert any("/engine" in p for p in paths)
        assert any("/acquisition" in p for p in paths)
        assert any("/stimulation" in p for p in paths)
