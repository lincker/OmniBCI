"""A running graph and children started later share one pinned configuration."""

from copy import deepcopy
from unittest.mock import MagicMock, Mock

import pytest

from omnibci.application.commands.engine_commands import StartEngineCommand, StopEngineCommand
from omnibci.application.commands.stimulation_commands import StartStimulationCommand
from omnibci.application.services.engine_service import NeuroEngineService
from omnibci.application.services.stimulation_service import StimulationAppService


@pytest.fixture
def thread_factory(monkeypatch):
    def create(**kwargs):
        thread = MagicMock()
        thread.is_alive.return_value = True
        thread.join.side_effect = lambda _timeout: setattr(thread.is_alive, "return_value", False)
        return thread

    factory = Mock(side_effect=create)
    monkeypatch.setattr("omnibci.application.services.engine_service.Thread", factory)
    return factory


def _snapshot(revision="revision-1"):
    return {
        "CONFIG.revision": revision,
        "ACQUISITION.sample_rate": 250,
        "STIMULATION.paradigm": "selected-paradigm",
        "ACTUATION.effector": "selected-effector",
        "ACTUATION.enabled": True,
        "PLUGINS.bindings": [{"parameters": {"window_duration": 3.0}}],
    }


@pytest.mark.asyncio
async def test_graph_and_late_stimulation_ignore_saved_edits(thread_factory):
    saved = _snapshot()
    provider = Mock(return_value=saved)
    graph = MagicMock(shutdown_complete=True)
    factory = Mock(return_value=graph)
    actuation = Mock()
    service = NeuroEngineService(factory, actuation, snapshot_provider=provider)
    await service.startup()
    await service.handle_start_engine(StartEngineCommand())
    pinned = service.applied_snapshot

    saved["CONFIG.revision"] = "saved-later"
    saved["PLUGINS.bindings"][0]["parameters"]["window_duration"] = 9.0
    returned = service.applied_snapshot
    returned["PLUGINS.bindings"][0]["parameters"]["window_duration"] = 12.0
    manager = Mock(auto_spawn=True)
    stimulation = StimulationAppService(service, manager)
    stimulation.handle_start_stimulation(StartStimulationCommand())
    actuation.start.assert_not_called()
    service.prepare_actuation()

    provider.assert_called_once_with()
    assert factory.call_args.args == (pinned,)
    actuation.start.assert_called_once_with(settings_snapshot=pinned)
    manager.start.assert_called_once_with(paradigm=None, settings_snapshot=pinned)
    assert service.applied_snapshot == pinned
    assert service.applied_revision == "revision-1"
    assert isinstance(service.generation, str) and service.generation
    assert "CONFIG.generation" not in saved


@pytest.mark.asyncio
async def test_factory_cannot_mutate_pinned_snapshot(thread_factory):
    saved = _snapshot()

    def build(snapshot):
        snapshot["PLUGINS.bindings"][0]["parameters"]["window_duration"] = 99
        return MagicMock(shutdown_complete=True)

    service = NeuroEngineService(build, snapshot_provider=lambda: saved)
    await service.startup()
    await service.handle_start_engine(StartEngineCommand())

    assert service.applied_snapshot["PLUGINS.bindings"] == saved["PLUGINS.bindings"]


@pytest.mark.asyncio
async def test_restart_resolves_once_and_explicit_rollback_does_not_resolve(thread_factory):
    provider = Mock(side_effect=[_snapshot("first"), _snapshot("second")])
    factory = Mock(side_effect=lambda snapshot: MagicMock(shutdown_complete=True))
    service = NeuroEngineService(factory, snapshot_provider=provider)
    await service.startup()
    await service.handle_start_engine(StartEngineCommand())
    original = service.applied_snapshot
    await service.handle_stop_engine(StopEngineCommand())
    await service.handle_start_engine(StartEngineCommand())
    second_generation = service.generation

    assert service.applied_revision == "second"
    assert second_generation != original["CONFIG.generation"]
    await service.handle_stop_engine(StopEngineCommand())
    await service._start_engine_locked(StartEngineCommand(), settings_snapshot=original)

    assert provider.call_count == 2
    assert service.applied_revision == "first"
    assert service.generation not in (original["CONFIG.generation"], second_generation)
    assert factory.call_args.args[0]["CONFIG.revision"] == "first"


@pytest.mark.asyncio
async def test_factory_type_error_is_not_retried_as_legacy(thread_factory):
    factory = Mock(side_effect=TypeError("broken graph constructor"))
    service = NeuroEngineService(factory, snapshot_provider=lambda: _snapshot())
    await service.startup()

    with pytest.raises(TypeError, match="broken graph constructor"):
        await service.handle_start_engine(StartEngineCommand())

    factory.assert_called_once()
    thread_factory.assert_not_called()
    assert service.applied_snapshot is None


@pytest.mark.asyncio
async def test_failed_candidate_keeps_last_successful_revision(thread_factory):
    failed_graph = MagicMock(shutdown_complete=True)
    failed_graph.wait_until_started.side_effect = RuntimeError("initialization failed")
    factory = Mock(side_effect=[MagicMock(shutdown_complete=True), failed_graph])
    provider = Mock(side_effect=[_snapshot("first"), _snapshot("failed")])
    service = NeuroEngineService(factory, snapshot_provider=provider)
    await service.startup()
    await service.handle_start_engine(StartEngineCommand())
    original = deepcopy(service.applied_snapshot)
    await service.handle_stop_engine(StopEngineCommand())

    with pytest.raises(RuntimeError, match="initialization failed"):
        await service.handle_start_engine(StartEngineCommand())

    assert service.applied_snapshot == original
    assert not service.is_engine_ready


@pytest.mark.asyncio
async def test_missing_snapshot_is_not_silently_loaded_from_global_settings(thread_factory):
    factory = Mock()
    service = NeuroEngineService(factory, snapshot_provider=lambda: None)
    await service.startup()

    with pytest.raises(ValueError, match="snapshot_provider must return"):
        await service.handle_start_engine(StartEngineCommand())

    factory.assert_not_called()
