"""Desktop cleanup retains unresponsive children for an explicit user decision."""
from unittest.mock import AsyncMock, MagicMock

import pytest

from omnibci.infrastructure.actuation import process_manager as actuation
from omnibci.infrastructure.stimulation import process_manager as stimulation
from omnibci.infrastructure.neuroengine.engine import NeuroEngine
from omnibci.infrastructure.process_lifecycle import ChildProcessState, StartupReport
from omnibci.application.services.engine_service import NeuroEngineService


@pytest.mark.parametrize("module,manager_class,component", [
    (actuation, actuation.ActuationProcessManager, "Actuation"),
    (stimulation, stimulation.StimulationProcessManager, "Stimulation"),
])
def test_desktop_output_retains_live_child_without_termination(monkeypatch, module, manager_class, component):
    process = MagicMock()
    process.is_alive.return_value = True
    receiver, sender = MagicMock(), MagicMock()
    receiver.poll.return_value = True
    receiver.recv.return_value = StartupReport.ready(component)
    process_factory = MagicMock(return_value=process)
    monkeypatch.setattr(module.mp, "Process", process_factory)
    monkeypatch.setattr(module.mp, "Event", MagicMock())
    monkeypatch.setattr(module.mp, "Pipe", MagicMock(return_value=(receiver, sender)))
    manager = manager_class(allow_process_termination=False)
    manager.start()
    # Python itself must not terminate this child as a daemon on backend exit.
    assert process_factory.call_args.kwargs["daemon"] is False

    with pytest.raises(RuntimeError, match="did not stop gracefully"):
        manager.stop()
    assert manager._process is process
    assert manager.state is ChildProcessState.FAILED
    manager._stop_event.set.assert_called_once()
    process.terminate.assert_not_called()
    process.kill.assert_not_called()
    process.close.assert_not_called()
    with pytest.raises(RuntimeError, match="did not stop gracefully"):
        manager.start()
    assert process_factory.call_count == 1

    process.is_alive.return_value = False
    manager.stop()
    assert manager._process is None
    process.close.assert_called_once()


@pytest.mark.asyncio
async def test_desktop_engine_attempts_both_workers_and_retains_live_handles():
    engine = NeuroEngine(
        event_bus=MagicMock(stop=AsyncMock()), data_manager=MagicMock(),
        acquisition_worker=MagicMock(), acquisition_command_queue=MagicMock(),
        signal_processing_worker=MagicMock(), processor_command_queue=MagicMock(),
        peer_gateway=MagicMock(stop=AsyncMock()), allow_process_termination=False,
    )
    engine._acquisition_started = engine._processor_started = True
    engine._gateway_started = engine._event_bus_started = True
    for worker in (engine.acquisition_worker, engine.signal_processing_worker):
        worker.is_alive.return_value = True
    with pytest.raises(ExceptionGroup) as error:
        await engine._stop()
    assert len(error.value.exceptions) == 2
    for worker in (engine.acquisition_worker, engine.signal_processing_worker):
        worker.join.assert_called_once_with(timeout=3)
        worker.terminate.assert_not_called()
        worker.kill.assert_not_called()
        worker.close.assert_not_called()
    engine.acquisition_command_queue.put_nowait.assert_called_once()
    engine.processor_command_queue.put_nowait.assert_called_once()
    engine.acquisition_command_queue.close.assert_not_called()
    engine.processor_command_queue.close.assert_not_called()
    engine.peer_gateway.stop.assert_awaited_once()
    engine.event_bus.stop.assert_awaited_once()
    assert engine.shutdown_complete is False


@pytest.mark.asyncio
async def test_repeated_shutdown_does_not_ignore_a_retained_failed_graph():
    service = NeuroEngineService(engine_factory=MagicMock())
    engine = MagicMock(shutdown_complete=False)
    service._engine = engine
    service._restart_blocked = True
    service._running = False
    service._engine_thread = None
    with pytest.raises(RuntimeError, match="unreleased resources"):
        await service.shutdown()
    assert service._engine is engine
    assert service._restart_blocked is True
