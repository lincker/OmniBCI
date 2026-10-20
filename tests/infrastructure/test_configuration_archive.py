"""Immutable runtime configurations remain readable independently of profiles."""
from copy import deepcopy
import sqlite3
from types import SimpleNamespace

import pytest

from omnibci.application.services.configuration_archive_service import (
    ConfigurationArchiveService, configuration_snapshot,
)
from omnibci.infrastructure.repositories.sqlite_configuration_archive import SQLiteConfigurationArchive
from omnibci.infrastructure.repositories.sqlite_session_repository import SQLiteSessionRepository
from omnibci.domain.channel_mapping import ProcessingInput


def _settings(revision="revision-1", generation="generation-1"):
    return {
        "CONFIG.revision": revision, "CONFIG.generation": generation,
        "ALGORITHM.baseline_duration": 0.25,
        "ALGORITHM.filters": ["high_pass", "low_pass"],
        "ACQUISITION.sample_rate": 250,
        "PLUGINS.bindings": [
            {"slot": "processing.filter.first", "kind": "filter",
             "selection": {"package_id": "test.high_pass", "contribution_id": "default", "version": "1.2.3"},
             "configuration_version": 1, "parameters": {"cutoff": 1.0}},
            {"slot": "processing.filter.second", "kind": "filter",
             "selection": {"package_id": "test.low_pass", "contribution_id": "default", "version": "2.0.0"},
             "configuration_version": 2, "parameters": {"cutoff": 40.0}},
        ],
        "LOGGING.file_path": "private.log",
        "PRIVATE.api_key": "not-runtime-data",
    }


@pytest.fixture
def archive(tmp_path):
    repository = SQLiteConfigurationArchive(tmp_path / "sessions.sqlite3")
    service = ConfigurationArchiveService(repository)
    yield service
    repository.close()


def test_snapshot_preserves_order_versions_parameters_and_excludes_host_state():
    settings = _settings()
    snapshot = configuration_snapshot(settings)
    assert snapshot == {
        "schema_version": 1,
        "settings": {"ALGORITHM.baseline_duration": 0.25,
                     "ACQUISITION.sample_rate": 250},
        "bindings": settings["PLUGINS.bindings"],
    }
    settings["PLUGINS.bindings"][0]["parameters"]["cutoff"] = 7
    assert snapshot["bindings"][0]["parameters"]["cutoff"] == 1


def test_schema_declared_secrets_are_redacted_in_bindings_and_source_mirrors():
    settings = _settings()
    settings["ACQUISITION.data_file_path"] = "credential-bearing-path"
    settings["PLUGINS.bindings"] = [{
        "slot": "acquisition.source", "kind": "acquisition",
        "selection": {"package_id": "test.source", "contribution_id": "default", "version": "1"},
        "parameters": {"data_file_path": "credential-bearing-path", "rate": 250,
                       "accounts": [{"name": "test", "credential": "private-token"}]},
    }]
    schema = {"properties": {
        "data_file_path": {"x-scope": "secret"},
        "accounts": {"items": {"properties": {"credential": {"x-scope": "secret"}}}},
    }}
    catalog = SimpleNamespace(get=lambda *args: SimpleNamespace(schema=schema))
    snapshot = configuration_snapshot(settings, catalog=catalog)
    binding = snapshot["bindings"][0]
    assert binding["parameters"]["rate"] == 250
    assert binding["parameters"]["accounts"][0]["name"] == "test"
    assert binding["redacted_parameters"] == ["data_file_path", "accounts[0].credential"]
    assert snapshot["settings"]["ACQUISITION.data_file_path"] == "[redacted]"
    assert "private-token" not in repr(snapshot)
    assert "credential-bearing-path" not in repr(snapshot)


@pytest.mark.parametrize("missing_schema", [False, True])
def test_missing_dormant_plugin_does_not_block_archiving_and_redacts_unknown_parameters(archive, missing_schema):
    settings = _settings()
    settings["PLUGINS.bindings"][0]["parameters"] = {"credential": "private-token"}
    original = deepcopy(settings)

    def definition(kind, selection):
        if selection.package_id == "test.high_pass":
            if missing_schema:
                return SimpleNamespace(schema=None)
            raise ValueError("Plugin is not installed")
        return SimpleNamespace(schema={"properties": {"cutoff": {"type": "number"}}})

    archive.catalog = SimpleNamespace(get=definition)

    attempt = archive.stage(settings, context="engine_start")
    archive.confirm(attempt, settings, receipt={"engine_ready": True})

    stored = archive.get(settings["CONFIG.revision"])
    first, second = stored["snapshot"]["bindings"]
    assert first["selection"] == settings["PLUGINS.bindings"][0]["selection"]
    assert first["parameters"] == "[redacted]"
    assert first["redacted_parameters"] == ["$"]
    assert second["parameters"] == {"cutoff": 40.0}
    assert stored["applications"][0]["status"] == "applied"
    assert "private-token" not in repr(stored)
    assert settings == original


def test_removing_duplicate_aliases_keeps_existing_archive_immutable(archive):
    settings = _settings()
    previous = configuration_snapshot(settings)
    previous["settings"].update({
        "ALGORITHM.filters": ["high_pass", "low_pass"],
        "ALGORITHM.decoder": "decoder",
        "STIMULATION.paradigm": "paradigm",
    })
    archive.repository.stage(settings["CONFIG.revision"], previous, generation="generation-1",
                             context="startup", session_id=None)
    row = archive.repository._connection.execute(
        "SELECT snapshot FROM configuration_snapshots").fetchone()[0]
    settings.pop("ALGORITHM.filters")

    attempt = archive.stage(settings, context="engine_start")
    archive.confirm(attempt, settings, receipt={"engine_ready": True})

    assert archive.get(settings["CONFIG.revision"])["snapshot"] == previous
    assert archive.repository._connection.execute(
        "SELECT snapshot FROM configuration_snapshots").fetchone()[0] == row
    changed = deepcopy(settings)
    changed["ALGORITHM.baseline_duration"] = 1.0
    with pytest.raises(ValueError, match="immutable"):
        archive.stage(changed, context="engine_start")


def test_missing_plugin_reuses_recorded_redactions_without_changing_archive(archive):
    settings = _settings()
    settings["PLUGINS.bindings"][0]["parameters"] = {
        "cutoff": 1.0, "accounts": [{"name": "lab", "credential": "private-token"}],
    }
    schema = {"properties": {"accounts": {"items": {"properties": {
        "credential": {"x-scope": "secret"},
    }}}}}
    archive.catalog = SimpleNamespace(get=lambda *args: SimpleNamespace(schema=schema))
    first = archive.stage(settings, context="engine_start")
    archive.confirm(first, settings, receipt={"engine_ready": True})
    previous = archive.get(settings["CONFIG.revision"])["snapshot"]

    def missing(*args):
        raise ValueError("Plugin is not installed")

    archive.catalog = SimpleNamespace(get=missing)
    settings["CONFIG.generation"] = "generation-2"
    attempt = archive.stage(settings, context="engine_start")
    archive.confirm(attempt, settings, receipt={"engine_ready": True})

    assert archive.get(settings["CONFIG.revision"])["snapshot"] == previous
    assert "private-token" not in repr(previous)
    changed = deepcopy(settings)
    changed["PLUGINS.bindings"][0]["parameters"]["cutoff"] = 2.0
    with pytest.raises(ValueError, match="immutable"):
        archive.stage(changed, context="engine_start")
    with pytest.raises(ValueError, match="differs from the archived"):
        archive.confirm(attempt, changed, receipt={"engine_ready": True})


def test_old_implicit_decoder_input_keeps_archive_and_runtime_inputs_unchanged(archive):
    settings = _settings()
    settings["PLUGINS.bindings"] = [{
        "slot": "processing.decoder", "kind": "decoder",
        "selection": {"package_id": "test.decoder", "contribution_id": "default", "version": "1"},
        "parameters": {},
    }]
    original = deepcopy(settings)
    previous = configuration_snapshot(settings)
    previous["bindings"][0]["processing_input"] = ProcessingInput().to_dict()
    archive.repository.stage(settings["CONFIG.revision"], previous, generation="generation-1",
                             context="startup", session_id=None)
    row = archive.repository._connection.execute(
        "SELECT snapshot FROM configuration_snapshots").fetchone()[0]

    attempt = archive.stage(settings, context="engine_start")
    archive.confirm(attempt, settings, receipt={"engine_ready": True})

    assert settings == original
    assert "processing_input" not in configuration_snapshot(settings)["bindings"][0]
    assert archive.repository._connection.execute(
        "SELECT snapshot FROM configuration_snapshots").fetchone()[0] == row
    changed = deepcopy(settings)
    changed["PLUGINS.bindings"][0]["processing_input"] = {
        "schema_version": 2, "outputs": [{"name": "O1", "source_channel_id": "O1"}],
    }
    with pytest.raises(ValueError, match="immutable"):
        archive.stage(changed, context="engine_start")
    with pytest.raises(ValueError, match="differs from the archived"):
        archive.confirm(attempt, changed, receipt={"engine_ready": True})


def test_snapshot_is_detached_and_survives_same_database_reopen(tmp_path):
    path = tmp_path / "sessions.sqlite3"
    sessions = SQLiteSessionRepository(path)
    repository = SQLiteConfigurationArchive(path)
    service = ConfigurationArchiveService(repository)
    settings = _settings()
    attempt = service.stage(settings, context="startup")
    service.confirm(attempt, settings, receipt={"ready": True})
    service.link_session("session-1", settings["CONFIG.revision"])
    stored = service.get_for_session("session-1")
    repository.close()
    sessions.close()
    reopened = SQLiteConfigurationArchive(path)
    try:
        assert reopened.get_for_session("session-1") == stored
        with sqlite3.connect(path) as connection:
            assert connection.execute("PRAGMA user_version").fetchone()[0] == SQLiteSessionRepository.SCHEMA_VERSION
        stored[0]["snapshot"]["bindings"].clear()
        assert len(reopened.get("revision-1")["snapshot"]["bindings"]) == 2
    finally:
        reopened.close()


def test_same_revision_can_be_used_in_multiple_generations(archive):
    first = archive.stage(_settings(), context="startup")
    archive.confirm(first, _settings(), receipt={"ready": True})
    settings = _settings(generation="generation-2")
    second = archive.stage(settings, context="startup")
    archive.confirm(second, settings, receipt={"ready": True})
    applications = archive.get("revision-1")["applications"]
    assert [item["generation"] for item in applications] == ["generation-1", "generation-2"]
    assert all(item["status"] == "applied" for item in applications)


def test_editable_profile_references_do_not_change_runtime_snapshot_identity(archive):
    settings = _settings()
    settings["PLUGINS.bindings"][0].update(profile_id="first-profile", profile_revision=1)
    archive.stage(settings, context="startup")
    settings["PLUGINS.bindings"][0].update(profile_id="equivalent-profile", profile_revision=4)
    attempt = archive.stage(settings, context="no_change")
    archive.confirm(attempt, settings, receipt={"already_applied": True})
    assert len(archive.get("revision-1")["applications"]) == 2
    assert "profile_id" not in archive.get("revision-1")["snapshot"]["bindings"][0]


def test_rebuild_resolves_generation_only_after_acknowledgement(archive):
    candidate = _settings(generation=None)
    attempt = archive.stage(candidate, context="rebuild", session_id="session-1")
    pending = archive.get_for_session("session-1")[0]["applications"][0]
    assert pending["status"] == "pending" and pending["generation"] is None
    archive.confirm(attempt, _settings(), receipt={"engine_ready": True})
    assert archive.get("revision-1")["applications"][0]["generation"] == "generation-1"


def test_changed_content_for_same_revision_rejected_without_new_attempt(archive):
    archive.stage(_settings(), context="startup")
    changed = _settings()
    changed["PLUGINS.bindings"].reverse()
    with pytest.raises(ValueError, match="immutable"):
        archive.stage(changed, context="startup", session_id="session-2")
    assert len(archive.get("revision-1")["applications"]) == 1
    assert archive.get_for_session("session-2") == []


@pytest.mark.parametrize("field,value", [("CONFIG.revision", "wrong"), ("CONFIG.generation", "wrong")])
def test_acknowledgement_identity_mismatch_stays_pending(archive, field, value):
    attempt = archive.stage(_settings(), context="startup")
    changed = _settings()
    changed[field] = value
    with pytest.raises(ValueError):
        archive.confirm(attempt, changed, receipt={"ready": True})
    assert archive.get("revision-1")["applications"][0]["status"] == "pending"


def test_acknowledged_snapshot_cannot_change_candidate_content(archive):
    attempt = archive.stage(_settings(), context="startup")
    changed = _settings()
    changed["ALGORITHM.baseline_duration"] = 2
    with pytest.raises(ValueError, match="differs from the archived"):
        archive.confirm(attempt, changed, receipt={"ready": True})


def test_acknowledgement_is_idempotent_but_receipt_is_immutable(archive):
    attempt = archive.stage(_settings(), context="startup")
    archive.confirm(attempt, _settings(), receipt={"ready": True})
    original = deepcopy(archive.get("revision-1"))
    archive.confirm(attempt, _settings(), receipt={"ready": True})
    archive.fail(attempt, "Later operation failed")
    assert archive.get("revision-1") == original
    with pytest.raises(ValueError, match="receipt is immutable"):
        archive.confirm(attempt, _settings(), receipt={"different": True})


def test_known_failure_is_terminal_and_not_applied(archive):
    attempt = archive.stage(_settings(), context="trial_boundary")
    archive.fail(attempt, "Worker rejected parameters")
    application = archive.get("revision-1")["applications"][0]
    assert application["status"] == "failed"
    assert application["error"] == "Worker rejected parameters"
    assert application["receipt"] is None
    with pytest.raises(ValueError, match="failed configuration"):
        archive.confirm(attempt, _settings(), receipt={"ready": True})


def test_session_links_preserve_revision_order_and_are_idempotent(archive):
    archive.stage(_settings(), context="startup")
    archive.link_session("session-1", "revision-1")
    archive.link_session("session-1", "revision-1")
    archive.stage(_settings(revision="revision-2"), context="trial_boundary", session_id="session-1")
    assert [item["config_revision"] for item in archive.get_for_session("session-1")] == ["revision-1", "revision-2"]
    assert archive.get_for_session("other-session") == []
    assert archive.get("missing") is None
    with pytest.raises(sqlite3.IntegrityError):
        archive.link_session("session-2", "missing")


def test_revision_and_generation_are_required_at_their_respective_boundaries(archive):
    with pytest.raises(ValueError, match="requires a revision"):
        archive.stage({}, context="startup")
    settings = _settings(generation=None)
    attempt = archive.stage(settings, context="selected")
    with pytest.raises(ValueError, match="requires an engine generation"):
        archive.confirm(attempt, settings, receipt={"ready": True})


def test_invalid_json_rolls_back_without_application_or_session_links(archive):
    settings = _settings()
    settings["ALGORITHM.baseline_duration"] = float("nan")
    with pytest.raises(ValueError):
        archive.stage(settings, context="startup", session_id="session-1")
    assert archive.get("revision-1") is None
    assert archive.get_for_session("session-1") == []
