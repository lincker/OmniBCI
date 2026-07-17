"""ActuationProcessManager: no-op semantics and effector override plumbing."""
from unittest.mock import MagicMock, patch

from omnibci.infrastructure.actuation.process_manager import ActuationProcessManager


class TestNoSpawnMode:
    def test_all_public_methods_are_noops(self):
        m = ActuationProcessManager(auto_spawn=False)
        m.start(effector="logger")
        assert m.is_alive is False and m.current_effector is None
        m.stop()   # no error

class TestSpawnMode:
    @patch("omnibci.infrastructure.actuation.process_manager.mp.Process")
    def test_start_spawns_with_effector_arg(self, mock_proc_cls):
        proc = MagicMock(); proc.is_alive.return_value = True
        mock_proc_cls.return_value = proc
        m = ActuationProcessManager(auto_spawn=True)
        m.start(effector="logger")
        kwargs = mock_proc_cls.call_args.kwargs
        assert kwargs["args"] == ("logger",) and kwargs["daemon"] is True
        assert m.is_alive is True and m.current_effector == "logger"

    @patch("omnibci.infrastructure.actuation.process_manager.mp.Process")
    def test_same_effector_is_idempotent(self, mock_proc_cls):
        proc = MagicMock(); proc.is_alive.return_value = True
        mock_proc_cls.return_value = proc
        m = ActuationProcessManager(auto_spawn=True)
        m.start(effector="logger")
        m.start(effector="logger")
        assert mock_proc_cls.call_count == 1

    @patch("omnibci.infrastructure.actuation.process_manager.mp.Process")
    def test_effector_change_restarts(self, mock_proc_cls):
        proc = MagicMock(); proc.is_alive.return_value = True
        mock_proc_cls.return_value = proc
        m = ActuationProcessManager(auto_spawn=True)
        m.start(effector="logger")
        m.start(effector="arm_v2")
        assert mock_proc_cls.call_count == 2
        proc.terminate.assert_called_once()

    @patch("omnibci.infrastructure.actuation.process_manager.mp.Process")
    def test_stop_terminates(self, mock_proc_cls):
        proc = MagicMock(); proc.is_alive.return_value = True
        mock_proc_cls.return_value = proc
        m = ActuationProcessManager(auto_spawn=True)
        m.start()
        m.stop()
        proc.terminate.assert_called_once()
        assert m.is_alive is False


class TestEngineLifecycleHook:
    def test_engine_service_starts_and_stops_manager(self):
        # NeuroEngineService calls manager.start() after engine start and
        # manager.stop() after engine stop (duck-typed injection).
        import asyncio
        from unittest.mock import MagicMock
        from omnibci.application.services.engine_service import NeuroEngineService
        from omnibci.application.commands.engine_commands import (
            StartEngineCommand, StopEngineCommand)

        manager = MagicMock()
        engine = MagicMock()
        svc = NeuroEngineService(engine_factory=lambda: engine,
                                 actuation_process_manager=manager)
        asyncio.run(svc.startup())
        asyncio.run(svc.handle_start_engine(StartEngineCommand()))
        manager.start.assert_called_once()
        asyncio.run(svc.handle_stop_engine(StopEngineCommand()))
        manager.stop.assert_called_once()
