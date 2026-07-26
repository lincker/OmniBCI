"""ActuationProcessManager: cooperative shutdown and escalation semantics."""
import multiprocessing as mp
import threading
from unittest.mock import MagicMock, patch

import pytest

from omnibci.infrastructure.actuation.process_manager import (
    ActuationProcessManager,
    _run_actuation,
)


def _cooperative_child(stop_event, stopped_flag):
    """Top-level target so the real-process test also works with spawn."""
    stop_event.wait(timeout=10.0)
    if stop_event.is_set():
        stopped_flag.value = 1


def _mock_process(*, exits_after_join=None, alive=True, pid=1234):
    """Process mock whose liveness changes on a selected join attempt."""
    process = MagicMock()
    state = {"alive": alive, "join_count": 0}
    process.pid = pid
    process.exitcode = 0
    process.is_alive.side_effect = lambda: state["alive"]

    def join(*, timeout):
        state["join_count"] += 1
        if exits_after_join == state["join_count"]:
            state["alive"] = False

    process.join.side_effect = join
    return process, state


class TestNoSpawnMode:
    def test_all_public_methods_are_noops(self):
        m = ActuationProcessManager(auto_spawn=False)
        m.start(effector="logger")
        assert m.is_alive is False and m.current_effector is None
        m.stop()   # no error


class TestSpawnMode:
    @patch("omnibci.infrastructure.actuation.process_manager.mp.Event")
    @patch("omnibci.infrastructure.actuation.process_manager.mp.Process")
    def test_start_spawns_with_effector_and_stop_event(
        self, mock_proc_cls, mock_event_cls
    ):
        proc, _ = _mock_process()
        stop_event = MagicMock()
        mock_proc_cls.return_value = proc
        mock_event_cls.return_value = stop_event
        m = ActuationProcessManager(auto_spawn=True)
        m.start(effector="logger")
        kwargs = mock_proc_cls.call_args.kwargs
        assert kwargs["args"] == ("logger", stop_event)
        assert kwargs["daemon"] is True
        assert m.is_alive is True and m.current_effector == "logger"

    @patch("omnibci.infrastructure.actuation.process_manager.mp.Event")
    @patch("omnibci.infrastructure.actuation.process_manager.mp.Process")
    def test_same_effector_is_idempotent(self, mock_proc_cls, mock_event_cls):
        proc, _ = _mock_process()
        mock_proc_cls.return_value = proc
        m = ActuationProcessManager(auto_spawn=True)
        m.start(effector="logger")
        m.start(effector="logger")
        assert mock_proc_cls.call_count == 1
        assert mock_event_cls.call_count == 1

    @patch("omnibci.infrastructure.actuation.process_manager.mp.Event")
    @patch("omnibci.infrastructure.actuation.process_manager.mp.Process")
    def test_concurrent_start_creates_one_controller(
        self, mock_proc_cls, mock_event_cls
    ):
        proc, _ = _mock_process()
        mock_proc_cls.return_value = proc
        manager = ActuationProcessManager(auto_spawn=True)

        threads = [
            threading.Thread(target=manager.start, kwargs={"effector": "logger"})
            for _ in range(2)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert mock_proc_cls.call_count == 1
        assert mock_event_cls.call_count == 1

    @patch("omnibci.infrastructure.actuation.process_manager.mp.Event")
    @patch("omnibci.infrastructure.actuation.process_manager.mp.Process")
    def test_effector_change_restarts_after_cooperative_exit(
        self, mock_proc_cls, mock_event_cls
    ):
        old_process, _ = _mock_process(exits_after_join=1, pid=1001)
        new_process, _ = _mock_process(pid=1002)
        old_stop, new_stop = MagicMock(), MagicMock()
        mock_proc_cls.side_effect = [old_process, new_process]
        mock_event_cls.side_effect = [old_stop, new_stop]
        m = ActuationProcessManager(auto_spawn=True)
        m.start(effector="logger")
        m.start(effector="arm_v2")

        assert mock_proc_cls.call_count == 2
        old_stop.set.assert_called_once_with()
        old_process.terminate.assert_not_called()
        assert m._process is new_process
        assert m._stop_event is new_stop

    @patch("omnibci.infrastructure.actuation.process_manager.mp.Event")
    @patch("omnibci.infrastructure.actuation.process_manager.mp.Process")
    def test_stop_exits_cooperatively_before_terminate(
        self, mock_proc_cls, mock_event_cls
    ):
        proc, _ = _mock_process(exits_after_join=1)
        stop_event = MagicMock()
        mock_proc_cls.return_value = proc
        mock_event_cls.return_value = stop_event
        m = ActuationProcessManager(auto_spawn=True)
        m.start()
        m.stop()

        stop_event.set.assert_called_once_with()
        proc.join.assert_called_once_with(timeout=m._GRACEFUL_TIMEOUT_S)
        proc.terminate.assert_not_called()
        proc.kill.assert_not_called()
        proc.close.assert_called_once_with()
        assert m.is_alive is False

    @patch("omnibci.infrastructure.actuation.process_manager.mp.Event")
    @patch("omnibci.infrastructure.actuation.process_manager.mp.Process")
    def test_stop_escalates_to_terminate_then_clears_state(
        self, mock_proc_cls, mock_event_cls
    ):
        proc, _ = _mock_process(exits_after_join=2)
        stop_event = MagicMock()
        mock_proc_cls.return_value = proc
        mock_event_cls.return_value = stop_event
        m = ActuationProcessManager()
        m.start(effector="logger")

        m.stop()

        assert [call.kwargs["timeout"] for call in proc.join.call_args_list] == [
            m._GRACEFUL_TIMEOUT_S,
            m._TERMINATE_TIMEOUT_S,
        ]
        proc.terminate.assert_called_once_with()
        proc.kill.assert_not_called()
        assert m._process is None and m._stop_event is None

    @patch("omnibci.infrastructure.actuation.process_manager.mp.Event")
    @patch("omnibci.infrastructure.actuation.process_manager.mp.Process")
    def test_stop_escalates_to_kill_when_terminate_is_ignored(
        self, mock_proc_cls, mock_event_cls
    ):
        proc, _ = _mock_process(exits_after_join=3)
        mock_proc_cls.return_value = proc
        mock_event_cls.return_value = MagicMock()
        m = ActuationProcessManager()
        m.start()

        m.stop()

        proc.terminate.assert_called_once_with()
        proc.kill.assert_called_once_with()
        assert [call.kwargs["timeout"] for call in proc.join.call_args_list] == [
            m._GRACEFUL_TIMEOUT_S,
            m._TERMINATE_TIMEOUT_S,
            m._KILL_TIMEOUT_S,
        ]
        assert m._process is None

    @patch("omnibci.infrastructure.actuation.process_manager.mp.Event")
    @patch("omnibci.infrastructure.actuation.process_manager.mp.Process")
    def test_unstoppable_child_is_retained_and_blocks_restart(
        self, mock_proc_cls, mock_event_cls
    ):
        proc, _ = _mock_process(exits_after_join=None)
        stop_event = MagicMock()
        mock_proc_cls.return_value = proc
        mock_event_cls.return_value = stop_event
        m = ActuationProcessManager()
        m.start(effector="logger")

        with pytest.raises(RuntimeError, match="still alive"):
            m.stop()

        assert m._process is proc
        assert m._stop_event is stop_event
        assert m._effector == "logger"
        proc.close.assert_not_called()

        with pytest.raises(RuntimeError, match="still alive"):
            m.start(effector="arm_v2")
        assert mock_proc_cls.call_count == 1

    def test_stop_cooperatively_joins_real_child(self):
        ctx = mp.get_context("spawn")
        stop_event = ctx.Event()
        stopped_flag = ctx.Value("b", 0)
        process = ctx.Process(
            target=_cooperative_child,
            args=(stop_event, stopped_flag),
        )
        process.start()
        m = ActuationProcessManager()
        m._process = process
        m._stop_event = stop_event
        m._effector = "logger"

        stopped = False
        try:
            m.stop()
            stopped = True
            assert stopped_flag.value == 1
            assert m._process is None and m._stop_event is None
        finally:
            if not stopped and process.is_alive():
                process.kill()
                process.join(timeout=3.0)


class TestChildEntryPoint:
    @patch("omnibci.infrastructure.actuation.application.Actuation")
    def test_forwards_stop_event_to_application(self, mock_actuation_cls):
        stop_event = object()

        _run_actuation(stop_event=stop_event)

        mock_actuation_cls.assert_called_once_with(stop_event=stop_event)
        mock_actuation_cls.return_value.run.assert_called_once_with()

    @patch("omnibci.conf.settings.set")
    @patch("omnibci.infrastructure.actuation.application.Actuation")
    def test_empty_effector_is_an_explicit_override(
        self, mock_actuation_cls, mock_settings_set
    ):
        _run_actuation(effector="", stop_event=object())

        mock_settings_set.assert_called_once_with("ACTUATION.effector", "")
        mock_actuation_cls.return_value.run.assert_called_once_with()


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
