"""ActuationProcessManager: cooperative shutdown and escalation semantics."""
import multiprocessing as mp
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from omnibci.infrastructure.process_lifecycle import (
    ChildProcessStartupError,
    ChildProcessStartupTimeout,
    ChildProcessState,
    StartupReport,
)
from omnibci.infrastructure.actuation import process_manager as process_manager_module
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


@pytest.fixture(autouse=True)
def startup_pipe(monkeypatch):
    """Every mocked child reports READY unless a test overrides the receiver."""
    receiver = MagicMock()
    receiver.poll.return_value = True
    receiver.recv.return_value = StartupReport.ready("Actuation")
    sender = MagicMock()
    pipe = MagicMock(return_value=(receiver, sender))
    monkeypatch.setattr(process_manager_module.mp, "Pipe", pipe)
    return SimpleNamespace(receiver=receiver, sender=sender, factory=pipe)


class TestNoSpawnMode:
    def test_all_public_methods_are_noops(self):
        m = ActuationProcessManager(auto_spawn=False)
        m.start(effector="logger")
        assert m.is_alive is False and m.current_effector is None
        assert m.state is ChildProcessState.STOPPED
        m.stop()   # no error


class TestSpawnMode:
    @patch("omnibci.infrastructure.actuation.process_manager.mp.Event")
    @patch("omnibci.infrastructure.actuation.process_manager.mp.Process")
    def test_start_spawns_with_effector_and_stop_event(
        self, mock_proc_cls, mock_event_cls, startup_pipe
    ):
        proc, _ = _mock_process()
        stop_event = MagicMock()
        mock_proc_cls.return_value = proc
        mock_event_cls.return_value = stop_event
        m = ActuationProcessManager(auto_spawn=True)
        m.start(effector="logger")
        kwargs = mock_proc_cls.call_args.kwargs
        assert kwargs["args"] == ("logger", stop_event, startup_pipe.sender)
        assert kwargs["daemon"] is True
        assert m.is_alive is True and m.is_ready is True
        assert m.state is ChildProcessState.READY
        assert m.current_effector == "logger"
        startup_pipe.sender.close.assert_called_once_with()

    @patch("omnibci.infrastructure.actuation.process_manager.mp.Event")
    @patch("omnibci.infrastructure.actuation.process_manager.mp.Process")
    def test_same_effector_is_idempotent(
        self, mock_proc_cls, mock_event_cls, startup_pipe
    ):
        proc, _ = _mock_process()
        mock_proc_cls.return_value = proc
        m = ActuationProcessManager(auto_spawn=True)
        m.start(effector="logger")
        m.start(effector="logger")
        assert mock_proc_cls.call_count == 1
        assert mock_event_cls.call_count == 1
        assert startup_pipe.factory.call_count == 1

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
        assert m.state is ChildProcessState.STOPPED

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
        assert m.state is ChildProcessState.FAILED
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

    @patch("omnibci.infrastructure.actuation.process_manager.mp.Event")
    @patch("omnibci.infrastructure.actuation.process_manager.mp.Process")
    def test_failed_startup_report_is_raised_and_child_is_reaped(
        self,
        mock_proc_cls,
        mock_event_cls,
        startup_pipe,
    ):
        process, _ = _mock_process(exits_after_join=1)
        mock_proc_cls.return_value = process
        mock_event_cls.return_value = MagicMock()
        startup_pipe.receiver.recv.return_value = StartupReport.failed(
            "Actuation",
            stage="initialization",
            error=RuntimeError("effector unavailable"),
        )
        manager = ActuationProcessManager()

        with pytest.raises(
            ChildProcessStartupError,
            match="effector unavailable",
        ):
            manager.start("logger")

        assert manager.state is ChildProcessState.FAILED
        assert manager.is_alive is False
        assert manager._process is None

    @patch("omnibci.infrastructure.actuation.process_manager.mp.Event")
    @patch("omnibci.infrastructure.actuation.process_manager.mp.Process")
    def test_startup_timeout_stops_child_and_remains_failed(
        self,
        mock_proc_cls,
        mock_event_cls,
        startup_pipe,
    ):
        process, _ = _mock_process(exits_after_join=1)
        mock_proc_cls.return_value = process
        mock_event_cls.return_value = MagicMock()
        startup_pipe.receiver.poll.return_value = False
        manager = ActuationProcessManager(startup_timeout_s=0.001)

        with pytest.raises(ChildProcessStartupTimeout):
            manager.start("logger")

        assert manager.state is ChildProcessState.FAILED
        assert manager._process is None

    @patch("omnibci.infrastructure.actuation.process_manager.mp.Event")
    @patch("omnibci.infrastructure.actuation.process_manager.mp.Process")
    def test_child_exit_before_report_is_reaped_without_waiting_for_timeout(
        self,
        mock_proc_cls,
        mock_event_cls,
        startup_pipe,
    ):
        process, _ = _mock_process(alive=False)
        process.exitcode = 9
        mock_proc_cls.return_value = process
        mock_event_cls.return_value = MagicMock()
        startup_pipe.receiver.poll.return_value = False
        manager = ActuationProcessManager(startup_timeout_s=1.0)

        with pytest.raises(
            ChildProcessStartupError,
            match=r"exitcode=9",
        ):
            manager.start("logger")

        assert manager.state is ChildProcessState.FAILED
        assert manager._process is None

    @patch("omnibci.infrastructure.actuation.process_manager.mp.Event")
    @patch("omnibci.infrastructure.actuation.process_manager.mp.Process")
    def test_stop_serializes_behind_startup_without_spawning_twice(
        self,
        mock_proc_cls,
        mock_event_cls,
        startup_pipe,
    ):
        process, _ = _mock_process(exits_after_join=1)
        stop_event = MagicMock()
        mock_proc_cls.return_value = process
        mock_event_cls.return_value = stop_event
        poll_entered = threading.Event()
        release_report = threading.Event()

        def poll(_timeout):
            poll_entered.set()
            release_report.wait(timeout=1.0)
            return True

        startup_pipe.receiver.poll.side_effect = poll
        manager = ActuationProcessManager()
        errors = []

        def start():
            try:
                manager.start("logger")
            except BaseException as exc:  # pragma: no cover - assertion below
                errors.append(exc)

        def stop():
            try:
                manager.stop()
            except BaseException as exc:  # pragma: no cover - assertion below
                errors.append(exc)

        start_thread = threading.Thread(target=start)
        stop_thread = threading.Thread(target=stop)
        start_thread.start()
        assert poll_entered.wait(timeout=1.0)
        stop_thread.start()
        release_report.set()
        start_thread.join(timeout=1.0)
        stop_thread.join(timeout=1.0)

        assert errors == []
        assert mock_proc_cls.call_count == 1
        stop_event.set.assert_called_once_with()
        assert manager.state is ChildProcessState.STOPPED

    @patch("omnibci.infrastructure.actuation.process_manager.mp.Event")
    @patch("omnibci.infrastructure.actuation.process_manager.mp.Process")
    def test_ready_state_detects_child_that_dies_later(
        self,
        mock_proc_cls,
        mock_event_cls,
    ):
        process, state = _mock_process()
        mock_proc_cls.return_value = process
        mock_event_cls.return_value = MagicMock()
        manager = ActuationProcessManager()
        manager.start("logger")

        state["alive"] = False

        assert manager.is_ready is False
        assert manager.state is ChildProcessState.FAILED


class TestChildEntryPoint:
    @patch("omnibci.infrastructure.actuation.application.Actuation")
    def test_forwards_stop_event_to_application(self, mock_actuation_cls):
        stop_event = object()

        _run_actuation(stop_event=stop_event)

        mock_actuation_cls.assert_called_once_with(stop_event=stop_event)
        mock_actuation_cls.return_value.run.assert_called_once_with()

    @patch("omnibci.infrastructure.actuation.application.Actuation")
    def test_entrypoint_reports_constructor_or_run_failure(
        self, mock_actuation_cls
    ):
        connection = MagicMock()
        mock_actuation_cls.return_value.run.side_effect = RuntimeError(
            "configuration failed"
        )

        with pytest.raises(RuntimeError, match="configuration failed"):
            _run_actuation(
                stop_event=object(),
                startup_connection=connection,
            )

        report = connection.send.call_args.args[0]
        assert report.state is ChildProcessState.FAILED
        assert report.stage == "entrypoint"
        assert report.message == "configuration failed"
        connection.close.assert_called_once_with()

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
