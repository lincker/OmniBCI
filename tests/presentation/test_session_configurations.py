"""Historical configuration queries use immutable persisted inputs."""

from omnibci.application.commands.session_commands import CreateSessionCommand
from omnibci.application.services.configuration_archive_service import ConfigurationArchiveService
from omnibci.infrastructure.repositories.sqlite_configuration_archive import SQLiteConfigurationArchive


def test_session_configurations_returns_persisted_receipt(api_client, session_service, tmp_path):
    path = tmp_path / "archive.sqlite3"
    repository = SQLiteConfigurationArchive(path)
    archive = ConfigurationArchiveService(repository)
    snapshot = {"CONFIG.revision": "revision-1", "CONFIG.generation": "generation-1",
                "ACQUISITION.sample_rate": 256, "PLUGINS.bindings": []}
    attempt = archive.stage(snapshot, context="engine_start")
    archive.confirm(attempt, snapshot, receipt={"engine_ready": True})
    session_service.set_configuration_archive(archive)
    sid = session_service.handle_create_session(CreateSessionCommand(config_revision="revision-1"))
    repository.close()
    reopened = SQLiteConfigurationArchive(path)
    session_service.set_configuration_archive(ConfigurationArchiveService(reopened))
    try:
        response = api_client.get(f"/api/v1/sessions/{sid}/configurations")
        assert response.status_code == 200
        data = response.json()["data"]
        assert len(data) == 1
        assert data[0]["config_revision"] == "revision-1"
        assert data[0]["snapshot"]["settings"]["ACQUISITION.sample_rate"] == 256
        assert data[0]["applications"][0]["status"] == "applied"
        assert data[0]["applications"][0]["receipt"] == {"engine_ready": True}
    finally:
        reopened.close()


def test_legacy_session_without_archive_returns_empty_history(api_client):
    sid = api_client.post("/api/v1/sessions").json()["data"]["session_id"]
    response = api_client.get(f"/api/v1/sessions/{sid}/configurations")
    assert response.status_code == 200
    assert response.json()["data"] == []


def test_unknown_session_configuration_returns_404(api_client):
    response = api_client.get("/api/v1/sessions/unknown/configurations")
    assert response.status_code == 404
    assert response.json()["code"] == "SESSION_NOT_FOUND"
