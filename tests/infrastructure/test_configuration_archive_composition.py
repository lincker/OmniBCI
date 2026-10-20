"""Production archive wiring and lifecycle boundaries without hardware workers."""

from pathlib import Path
import sqlite3
from threading import RLock
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock

import pytest

from omnibci import bootstrap
from omnibci.application.commands.engine_commands import StartEngineCommand
from omnibci.application.commands.session_commands import CreateSessionCommand
from omnibci.application.events import CommandResult, StreamDrainReceipt, TrialOutcome
from omnibci.application.services.configuration_archive_service import ConfigurationArchiveService
from omnibci.application.services.session_service import SessionAppService
from omnibci.domain.classification import Classification
from omnibci.domain.repositories import SessionRecordState
from omnibci.infrastructure.acquisition.recording import LocalSignalRecorder
from omnibci.infrastructure.repositories.sqlite_configuration_archive import SQLiteConfigurationArchive
from omnibci.infrastructure.repositories.sqlite_session_repository import SQLiteSessionRepository
from omnibci.infrastructure.storage_lock import StorageLease
from omnibci.shared.events import Event, EventType
from tests.plugin_configuration_helpers import accept_profiles


@pytest.fixture
def configured_backend(tmp_path, monkeypatch):
    import omnibci.conf as conf

    path = tmp_path / "host.toml"
    path.write_text('''[ACQUISITION]
signal_source = "file_reader"
data_file_path = "unused-in-mocked-workers.cnt"
file_reader_chunk_size = 10
trigger = ""
[ALGORITHM]
decoder = "THUBCI_SSVEP_classic40CCA"
epoch_detector = "DEFAULT_EPOCH_DETECTOR"
filters = []
baseline_duration = 0.2
[STIMULATION]
paradigm = "STIMULATION_THUBCI_SSVEP_classic40"
auto_spawn = false
[ACTUATION]
enabled = false
auto_spawn = false
effector = ""
''', encoding="utf-8")
    settings = conf.OmniBCISettings(path)
    monkeypatch.setattr(conf, "settings", settings)
    monkeypatch.setenv("OMNIBCI_PLUGIN_DIR", str(tmp_path / "plugins"))
    return settings


@pytest.fixture
def fake_graphs(monkeypatch):
    state = SimpleNamespace(graphs=[], archive=None)

    def build(*, settings_snapshot, **kwargs):
        if state.archive is not None:
            application = state.archive.get(settings_snapshot["CONFIG.revision"])["applications"][-1]
            assert application["status"] == "pending"
            assert application["generation"] == settings_snapshot["CONFIG.generation"]
        graph = MagicMock(shutdown_complete=True)
        state.graphs.append(graph)
        return SimpleNamespace(build=lambda: graph)

    def create_thread(**kwargs):
        thread = MagicMock()
        thread.is_alive.return_value = True
        thread.join.side_effect = lambda timeout: setattr(thread.is_alive, "return_value", False)
        return thread

    monkeypatch.setattr(bootstrap, "NeuroEngineBuilder", build)
    monkeypatch.setattr("omnibci.application.services.engine_service.Thread", create_thread)
    return state


def _database_path(repository):
    return Path(repository._connection.execute("PRAGMA database_list").fetchone()[2])


def _select_source(container):
    accept_profiles(container.plugin_profile_service, [
        ("acquisition.source", "acquisition", "file_reader", {
            "data_file_path": "unused-in-mocked-workers.cnt", "file_reader_chunk_size": 10,
        }),
    ])


@pytest.mark.asyncio
async def test_default_composition_shares_database_archive_and_ownership(configured_backend):
    container = await bootstrap.build_container()
    archive = container.configuration_archive
    session_repository = container.session_repository
    assert isinstance(session_repository, SQLiteSessionRepository)
    assert isinstance(archive.repository, SQLiteConfigurationArchive)
    path = _database_path(session_repository)
    assert _database_path(archive.repository) == path
    assert archive.catalog is container.plugin_profile_service.catalog
    assert container.engine_service._configuration_archive is archive
    assert container.session_service._configuration_archive is archive
    assert container.runtime_configuration.executor.archive is archive
    container.close_storage()
    container.close_storage()
    with pytest.raises(RuntimeError, match="closed"):
        session_repository.find_all()
    with pytest.raises(RuntimeError, match="closed"):
        archive.get("missing")
    with StorageLease(str(path) + ".lock"):
        pass


@pytest.mark.asyncio
async def test_in_memory_composition_does_not_open_archive_database(configured_backend):
    container = await bootstrap.build_container(host_settings={
        **configured_backend.snapshot(), "PERSISTENCE.session_repository": "in_memory",
    })
    try:
        assert container.configuration_archive is None
        assert container.engine_service._configuration_archive is None
        assert container.session_service._configuration_archive is None
        assert container.runtime_configuration.executor.archive is None
        assert not bootstrap._session_database_path().exists()
    finally:
        container.close_storage()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_boundary", ["archive_creation", "service_wiring"])
async def test_failed_composition_closes_open_stores_and_releases_lease(configured_backend, monkeypatch, failure_boundary):
    opened = []

    def session_repository(path):
        repository = SQLiteSessionRepository(path)
        opened.append(repository)
        return repository

    def archive_repository(path):
        if failure_boundary == "archive_creation":
            raise OSError("Archive initialization failed")
        repository = SQLiteConfigurationArchive(path)
        opened.append(repository)
        return repository

    async def fail_services(*args, **kwargs):
        raise OSError("Service wiring failed")

    monkeypatch.setattr(bootstrap, "SQLiteSessionRepository", session_repository)
    monkeypatch.setattr(bootstrap, "SQLiteConfigurationArchive", archive_repository)
    monkeypatch.setattr(bootstrap, "_build_container_services", fail_services)
    with pytest.raises(OSError, match="failed"):
        await bootstrap.build_container()
    assert len(opened) == (1 if failure_boundary == "archive_creation" else 2)
    for repository in opened:
        assert repository._closed
    with StorageLease(str(bootstrap._session_database_path()) + ".lock"):
        pass


class _RecordingController:
    """A stopped fake source with a real local recording lifecycle."""

    def __init__(self, engine, root):
        self.engine = engine
        self.lifecycle_lock = RLock()
        self.is_streaming = False
        self.writer = LocalSignalRecorder(root)

    def handle_stop_and_drain(self, *, session_id=None):
        return StreamDrainReceipt(self.engine.generation, self.engine.applied_revision,
                                  -1, session_id, 0, False)

    def begin_raw_recording(self, metadata):
        return self.writer.start({**metadata, "config_revision": self.engine.applied_revision})


@pytest.mark.asyncio
async def test_engine_session_archive_survives_backend_restart(configured_backend, fake_graphs, tmp_path):
    container = await bootstrap.build_container()
    _select_source(container)
    archive = container.configuration_archive
    fake_graphs.archive = archive
    engine = container.engine_service
    await engine.handle_start_engine(StartEngineCommand())
    revision, generation = engine.applied_revision, engine.generation
    assert archive.get(revision)["applications"][0]["status"] == "applied"
    acquisition = _RecordingController(engine, tmp_path / "raw")
    service = container.session_service
    service.set_recording_controller(acquisition)
    service.set_signal_preparer(lambda: {"config_revision": revision}, acquisition.lifecycle_lock)
    starts = []

    def request(event_type, payload=None, **kwargs):
        assert event_type is EventType.StartSession
        history = archive.get_for_session(payload["session_id"])
        assert history[0]["config_revision"] == revision
        assert container.session_repository.find_by_id(payload["session_id"]) is not None
        starts.append(payload)
        return CommandResult("started", event_type.name, True)

    service._command_results = SimpleNamespace(request=request)
    session_id = service.handle_create_session(CreateSessionCommand(subject_id="subject-1"))
    service.on_trial_completed(Event(EventType.TrialCompleted, TrialOutcome(
        session_id, 1, Classification(target_id=2, event_id=100), revision,
    )))
    history = service.handle_get_configurations(session_id)
    assert starts[0]["config_revision"] == revision
    acquisition.writer.finish(status="interrupted")
    await engine.shutdown()
    container.close_storage()

    restarted = await bootstrap.build_container()
    try:
        stored = restarted.session_repository.find_by_id(session_id)
        assert stored.state is SessionRecordState.ENDED
        assert stored.subject_id == "subject-1"
        assert stored.trials[0].config_revision == revision
        assert stored.recording["status"] == "interrupted"
        assert restarted.session_service.handle_get_configurations(session_id) == history
        assert history[0]["applications"][0]["generation"] == generation
        assert restarted.engine_service.applied_snapshot is None
        assert not restarted.engine_service.is_engine_ready
        assert len(fake_graphs.graphs) == 1
    finally:
        restarted.close_storage()


@pytest.mark.asyncio
async def test_missing_runtime_revision_cannot_dispatch_session(configured_backend):
    container = await bootstrap.build_container()
    commands = Mock()
    service = SessionAppService(container.session_repository, container.engine_service, commands)
    service.set_configuration_archive(container.configuration_archive)
    try:
        with pytest.raises(RuntimeError, match="archived runtime configuration"):
            service.handle_create_session(CreateSessionCommand())
        assert container.session_repository.find_all() == []
        commands.request.assert_not_called()
        assert container.engine_service.applied_snapshot is None
    finally:
        container.close_storage()


def test_archive_link_failure_rolls_back_session_before_dispatch(tmp_path):
    sessions = SQLiteSessionRepository(tmp_path / "sessions.sqlite3")
    repository = SQLiteConfigurationArchive(tmp_path / "sessions.sqlite3")
    archive = ConfigurationArchiveService(repository)
    commands = Mock()
    service = SessionAppService(sessions, SimpleNamespace(applied_snapshot={"CONFIG.revision": "missing"}), commands)
    service.set_configuration_archive(archive)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            service.handle_create_session(CreateSessionCommand())
        assert sessions.find_all() == []
        commands.request.assert_not_called()
    finally:
        repository.close()
        sessions.close()


def test_stale_explicit_revision_cannot_prepare_or_dispatch_a_session(tmp_path):
    sessions = SQLiteSessionRepository(tmp_path / "sessions.sqlite3")
    repository = SQLiteConfigurationArchive(tmp_path / "sessions.sqlite3")
    archive = ConfigurationArchiveService(repository)
    for revision in ("old-revision", "current-revision"):
        settings = {"CONFIG.revision": revision, "CONFIG.generation": "generation-1"}
        attempt = archive.stage(settings, context="startup")
        archive.confirm(attempt, settings, receipt={"ready": True})
    commands, prepare = Mock(), Mock()
    service = SessionAppService(
        sessions, SimpleNamespace(applied_snapshot={"CONFIG.revision": "current-revision"}), commands,
    )
    service.set_configuration_archive(archive)
    service.set_signal_preparer(prepare, RLock())
    try:
        with pytest.raises((ValueError, RuntimeError), match="revision|configuration"):
            service.handle_create_session(CreateSessionCommand(config_revision="old-revision"))
        assert sessions.find_all() == []
        prepare.assert_not_called()
        commands.request.assert_not_called()
    finally:
        repository.close()
        sessions.close()


@pytest.mark.asyncio
async def test_archive_failure_prevents_graph_creation(configured_backend, fake_graphs, monkeypatch):
    container = await bootstrap.build_container()
    _select_source(container)

    def fail(*args, **kwargs):
        raise OSError("Cannot persist candidate")

    monkeypatch.setattr(container.configuration_archive.repository, "stage", fail)
    try:
        with pytest.raises(OSError, match="Cannot persist"):
            await container.engine_service.handle_start_engine(StartEngineCommand())
        assert fake_graphs.graphs == []
        assert not container.engine_service.is_engine_ready
    finally:
        container.close_storage()


@pytest.mark.asyncio
async def test_archive_receipt_failure_stops_graph_and_retains_pending(configured_backend, fake_graphs, monkeypatch):
    container = await bootstrap.build_container()
    _select_source(container)
    fake_graphs.archive = container.configuration_archive

    def fail(*args, **kwargs):
        raise OSError("Cannot persist acknowledgement")

    monkeypatch.setattr(container.configuration_archive.repository, "confirm", fail)
    try:
        with pytest.raises(OSError, match="Cannot persist"):
            await container.engine_service.handle_start_engine(StartEngineCommand())
        revision = container.plugin_profile_service.repository.get_activation().revision
        assert container.configuration_archive.get(revision)["applications"][0]["status"] == "pending"
        assert container.engine_service.admission.recovery_required
        assert not container.engine_service.is_engine_ready
        assert fake_graphs.graphs[0].should_stop is True
    finally:
        await container.engine_service.shutdown()
        container.close_storage()
