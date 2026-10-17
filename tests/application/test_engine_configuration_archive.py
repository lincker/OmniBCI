"""Engine generations archive inputs before startup and confirm actual readiness."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from omnibci.application.commands.engine_commands import StartEngineCommand, StopEngineCommand
from omnibci.application.plugin_profiles import ProfileRevisionConflict
from omnibci.application.services.configuration_archive_service import ConfigurationArchiveService
from omnibci.application.services.engine_service import NeuroEngineService
from omnibci.infrastructure.repositories.sqlite_configuration_archive import SQLiteConfigurationArchive


@pytest.fixture
def archived_engine(tmp_path, monkeypatch):
    repository = SQLiteConfigurationArchive(tmp_path / "configurations.sqlite3")
    archive = ConfigurationArchiveService(repository)
    engine = Mock(should_stop=False, shutdown_complete=True)
    thread = Mock()
    thread.is_alive.return_value = True
    snapshot = {"CONFIG.revision": "a" * 64, "PLUGINS.bindings": []}
    trace = []
    h = SimpleNamespace(repository=repository, archive=archive, engine=engine, thread=thread,
                        snapshot=snapshot, trace=trace, keep_thread_alive=False)

    def application():
        stored = archive.get(snapshot["CONFIG.revision"])
        return stored["applications"][-1] if stored else None

    h.application = application
    stage, confirm, fail = archive.stage, archive.confirm, archive.fail

    def stage_candidate(*args, **kwargs):
        trace.append("stage")
        return stage(*args, **kwargs)

    def confirm_ready(*args, **kwargs):
        trace.append("confirm")
        return confirm(*args, **kwargs)

    def fail_attempt(*args, **kwargs):
        trace.append("fail")
        return fail(*args, **kwargs)

    def factory(settings):
        assert application()["status"] == "pending"
        trace.append("factory")
        return engine

    def ready(timeout):
        assert application()["status"] == "pending"
        trace.append("ready")

    def thread_start():
        trace.append("thread_start")
        thread.is_alive.return_value = True

    def join(timeout):
        trace.append("join")
        thread.is_alive.return_value = h.keep_thread_alive

    monkeypatch.setattr(archive, "stage", stage_candidate)
    monkeypatch.setattr(archive, "confirm", confirm_ready)
    monkeypatch.setattr(archive, "fail", fail_attempt)
    monkeypatch.setattr("omnibci.application.services.engine_service.Thread", Mock(return_value=thread))
    thread.start.side_effect = thread_start
    thread.join.side_effect = join
    engine.wait_until_started.side_effect = ready
    h.factory = Mock(side_effect=factory)
    h.service = NeuroEngineService(h.factory, snapshot_provider=lambda: snapshot)
    h.service.set_configuration_archive(archive)
    yield h
    repository.close()


@pytest.mark.asyncio
async def test_stage_precedes_factory_and_confirmation_waits_for_all_readiness(archived_engine):
    h = archived_engine
    manager = Mock()

    def output_ready(**kwargs):
        assert h.application()["status"] == "pending"
        assert not h.service.is_engine_ready
        h.trace.append("output_ready")

    manager.start.side_effect = output_ready
    h.service._actuation_manager = manager
    await h.service.startup()
    await h.service.handle_start_engine(StartEngineCommand())
    assert h.trace == ["stage", "factory", "thread_start", "ready", "output_ready", "confirm"]
    application = h.application()
    assert application["status"] == "applied"
    assert application["context"] == "engine_start"
    assert application["receipt"] == {"engine_ready": True}
    assert application["generation"] == h.service.applied_snapshot["CONFIG.generation"]
    assert h.service.is_engine_ready
    assert "CONFIG.generation" not in h.snapshot


@pytest.mark.asyncio
async def test_failed_candidate_archive_never_builds_workers(archived_engine, monkeypatch):
    h = archived_engine

    def unavailable(*args, **kwargs):
        raise OSError("archive disk full")

    monkeypatch.setattr(h.repository, "stage", unavailable)
    await h.service.startup()
    with pytest.raises(OSError, match="archive disk full"):
        await h.service.handle_start_engine(StartEngineCommand())
    h.factory.assert_not_called()
    h.thread.start.assert_not_called()
    assert h.application() is None
    assert not h.service.is_engine_ready


@pytest.mark.asyncio
async def test_failed_factory_records_failed_attempt_before_any_thread_start(archived_engine):
    h = archived_engine

    def broken(settings):
        assert h.application()["status"] == "pending"
        raise RuntimeError("plugin construction failed")

    h.factory.side_effect = broken
    await h.service.startup()
    with pytest.raises(RuntimeError, match="plugin construction failed"):
        await h.service.handle_start_engine(StartEngineCommand())
    assert h.application()["status"] == "failed"
    assert "plugin construction failed" in h.application()["error"]
    assert h.trace == ["stage", "fail"]
    h.thread.start.assert_not_called()


@pytest.mark.asyncio
async def test_failed_readiness_records_failure_only_after_confirmed_shutdown(archived_engine):
    h = archived_engine
    h.engine.wait_until_started.side_effect = RuntimeError("worker setup failed")
    await h.service.startup()
    with pytest.raises(RuntimeError, match="worker setup failed"):
        await h.service.handle_start_engine(StartEngineCommand())
    assert h.trace[-2:] == ["join", "fail"]
    assert h.application()["status"] == "failed"
    assert "worker setup failed" in h.application()["error"]
    assert h.service._needs_rebuild
    assert not h.service.is_engine_ready


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["live_thread", "unreleased_resources"])
async def test_uncertain_startup_cleanup_retains_pending_attempt(archived_engine, failure):
    h = archived_engine
    h.engine.wait_until_started.side_effect = TimeoutError("readiness unknown")
    h.keep_thread_alive = failure == "live_thread"
    h.engine.shutdown_complete = failure != "unreleased_resources"
    await h.service.startup()
    with pytest.raises(TimeoutError, match="readiness unknown"):
        await h.service.handle_start_engine(StartEngineCommand())
    assert h.application()["status"] == "pending"
    assert "fail" not in h.trace and "confirm" not in h.trace
    assert h.service._restart_blocked
    assert not h.service.is_engine_ready
    with pytest.raises(RuntimeError, match="restart is blocked"):
        await h.service.handle_start_engine(StartEngineCommand())


@pytest.mark.asyncio
@pytest.mark.parametrize("uncertain", [False, True])
async def test_output_start_failure_is_failed_only_when_engine_stop_is_confirmed(archived_engine, uncertain):
    h = archived_engine
    h.service._actuation_manager = Mock()
    h.service._actuation_manager.start.side_effect = RuntimeError("output setup failed")
    h.keep_thread_alive = uncertain
    await h.service.startup()
    with pytest.raises(RuntimeError, match="did not stop" if uncertain else "output setup failed"):
        await h.service.handle_start_engine(StartEngineCommand())
    assert h.application()["status"] == ("pending" if uncertain else "failed")
    assert "confirm" not in h.trace
    assert not h.service.is_engine_ready


@pytest.mark.asyncio
@pytest.mark.parametrize("uncertain", [False, True])
async def test_confirmation_failure_quarantines_admission_and_stops_generation(archived_engine, monkeypatch, uncertain):
    h = archived_engine

    def unavailable(*args, **kwargs):
        raise OSError("receipt archive unavailable")

    monkeypatch.setattr(h.repository, "confirm", unavailable)
    h.keep_thread_alive = uncertain
    await h.service.startup()
    with pytest.raises(RuntimeError if uncertain else OSError,
                       match="did not stop" if uncertain else "receipt archive unavailable"):
        await h.service.handle_start_engine(StartEngineCommand())
    assert h.application()["status"] == "pending"
    assert h.engine.should_stop
    h.thread.join.assert_called_once()
    assert h.service.admission.recovery_required
    assert not h.service.is_engine_ready
    assert h.service.applied_snapshot is None
    assert h.service._running is uncertain
    with pytest.raises(ProfileRevisionConflict, match="requires recovery"):
        await h.service.handle_start_engine(StartEngineCommand())


@pytest.mark.asyncio
async def test_restart_archives_distinct_generation_attempts(archived_engine):
    h = archived_engine
    await h.service.startup()
    await h.service.handle_start_engine(StartEngineCommand())
    first = h.application()
    await h.service.handle_stop_engine(StopEngineCommand())
    await h.service.handle_start_engine(StartEngineCommand())
    applications = h.archive.get(h.snapshot["CONFIG.revision"])["applications"]
    assert len(applications) == 2
    assert all(item["status"] == "applied" for item in applications)
    assert applications[0] == first
    assert applications[0]["generation"] != applications[1]["generation"]
    assert h.trace.count("stage") == h.trace.count("confirm") == 2
