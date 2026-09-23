"""Only graph/resource ownership retains package versions after runtime transitions."""
from unittest.mock import MagicMock, Mock

import pytest

from omnibci.application.commands.engine_commands import StartEngineCommand, StopEngineCommand
from omnibci.application.services.engine_service import NeuroEngineService


def snapshot(version):
    return {"CONFIG.revision": f"revision-{version}", "PLUGINS.bindings": [{
        "selection": {"package_id": "acme.device", "contribution_id": "output", "version": version},
        "parameters": {"gain": 1.0},
    }]}


@pytest.mark.parametrize("running,alive,blocked", [(True, False, False), (False, True, False), (False, False, True)])
def test_paused_or_unresolved_graph_retains_acknowledged_and_attempted_packages(running, alive, blocked):
    service = NeuroEngineService(Mock())
    service._applied_snapshot = snapshot("1.0.0")
    service._graph_snapshot = snapshot("2.0.0")
    service._running = running
    service._restart_blocked = blocked
    service._engine_thread = Mock(is_alive=Mock(return_value=alive))
    service._accepting_commands = False

    references = service.runtime_package_snapshots

    assert references == (snapshot("1.0.0"), snapshot("2.0.0"))
    references[0]["PLUGINS.bindings"][0]["selection"]["version"] = "changed"
    references[1]["PLUGINS.bindings"][0]["parameters"]["gain"] = 99
    assert service._applied_snapshot == snapshot("1.0.0")
    assert service._graph_snapshot == snapshot("2.0.0")
    assert not service.is_engine_ready


def test_clean_stopped_engine_does_not_retain_historical_package_snapshots():
    service = NeuroEngineService(Mock())
    service._applied_snapshot = snapshot("1.0.0")
    service._graph_snapshot = snapshot("2.0.0")
    service._engine_thread = Mock(is_alive=Mock(return_value=False))
    assert service.runtime_package_snapshots == ()
    assert service.applied_snapshot == snapshot("1.0.0")


@pytest.mark.asyncio
async def test_successful_stop_releases_runtime_references_but_preserves_history(monkeypatch):
    thread = Mock(is_alive=Mock(return_value=True))
    thread.join.side_effect = lambda timeout: setattr(thread.is_alive, "return_value", False)
    monkeypatch.setattr("omnibci.application.services.engine_service.Thread", Mock(return_value=thread))
    service = NeuroEngineService(Mock(return_value=MagicMock(shutdown_complete=True)),
                                 snapshot_provider=lambda: snapshot("1.0.0"))
    await service.startup()
    await service.handle_start_engine(StartEngineCommand())
    applied = service.applied_snapshot
    assert service.runtime_package_snapshots == (applied, applied)

    await service.handle_stop_engine(StopEngineCommand())

    assert service.runtime_package_snapshots == ()
    assert service.applied_snapshot == applied


@pytest.mark.asyncio
async def test_unconfirmed_shutdown_keeps_package_ownership_until_successful_recovery(monkeypatch):
    thread = Mock(is_alive=Mock(return_value=True))
    thread.join.side_effect = lambda timeout: setattr(thread.is_alive, "return_value", False)
    monkeypatch.setattr("omnibci.application.services.engine_service.Thread", Mock(return_value=thread))
    graph = MagicMock(shutdown_complete=True)
    service = NeuroEngineService(Mock(return_value=graph), snapshot_provider=lambda: snapshot("1.0.0"))
    await service.startup()
    await service.handle_start_engine(StartEngineCommand())
    graph.shutdown_complete = False
    with pytest.raises(RuntimeError):
        await service.handle_stop_engine(StopEngineCommand())
    assert service.restart_blocked and not service.is_engine_ready
    assert service.runtime_package_snapshots

    graph.shutdown_complete = True
    await service.handle_stop_engine(StopEngineCommand())
    assert not service.runtime_package_snapshots
