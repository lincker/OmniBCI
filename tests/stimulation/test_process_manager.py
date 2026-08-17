"""Stimulation subprocess cooperative lifecycle and escalation semantics."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch
import threading

import pytest

from omnibci.infrastructure.process_lifecycle import (
    ChildProcessStartupError,
    ChildProcessStartupTimeout,
    ChildProcessState,
    StartupReport,
)
from omnibci.infrastructure.stimulation import process_manager as process_manager_module
from omnibci.infrastructure.stimulation.process_manager import (
    StimulationProcessManager,
    _run_stimulation,
)


def _mock_process(*, exits_after_join=None, alive=True, pid=4321):
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
    receiver = MagicMock()
    receiver.poll.return_value = True
    receiver.recv.return_value = StartupReport.ready("Stimulation")
    sender = MagicMock()
    pipe = MagicMock(return_value=(receiver, sender))
    monkeypatch.setattr(process_manager_module.mp, "Pipe", pipe)
    return SimpleNamespace(receiver=receiver, sender=sender, factory=pipe)


def test_no_spawn_mode_is_a_noop() -> None:
    manager = StimulationProcessManager(auto_spawn=False)
    manager.start("demo")
    manager.stop()
    assert manager.is_alive is False
    assert manager.state is ChildProcessState.STOPPED


def test_default_timeout_allows_cold_renderer_startup() -> None:
    manager = StimulationProcessManager()

    assert manager._startup_timeout_s >= 30.0


@patch("omnibci.infrastructure.stimulation.process_manager.mp.Event")
@patch("omnibci.infrastructure.stimulation.process_manager.mp.Process")
def test_start_passes_cooperative_stop_event(
    mock_process_cls, mock_event_cls, startup_pipe
):
    process, _ = _mock_process()
    stop_event = MagicMock()
    mock_process_cls.return_value = process
    mock_event_cls.return_value = stop_event
    manager = StimulationProcessManager()

    manager.start("classic")

    assert mock_process_cls.call_args.kwargs["args"] == (
        "classic",
        stop_event,
        startup_pipe.sender,
    )
    assert manager.is_alive is True
    assert manager.is_ready is True
    assert manager.state is ChildProcessState.READY


@patch("omnibci.infrastructure.stimulation.process_manager.mp.Event")
@patch("omnibci.infrastructure.stimulation.process_manager.mp.Process")
def test_same_paradigm_start_is_idempotent(
    mock_process_cls, mock_event_cls, startup_pipe
):
    process, _ = _mock_process()
    mock_process_cls.return_value = process
    manager = StimulationProcessManager()

    manager.start("classic")
    manager.start("classic")

    assert mock_process_cls.call_count == 1
    assert mock_event_cls.call_count == 1
    assert startup_pipe.factory.call_count == 1


@patch("omnibci.infrastructure.stimulation.process_manager.mp.Event")
@patch("omnibci.infrastructure.stimulation.process_manager.mp.Process")
def test_concurrent_start_creates_one_renderer(mock_process_cls, mock_event_cls):
    process, _ = _mock_process()
    mock_process_cls.return_value = process
    manager = StimulationProcessManager()
    threads = [
        threading.Thread(target=manager.start, args=("classic",))
        for _ in range(2)
    ]

    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert mock_process_cls.call_count == 1
    assert mock_event_cls.call_count == 1


@patch("omnibci.infrastructure.stimulation.process_manager.mp.Event")
@patch("omnibci.infrastructure.stimulation.process_manager.mp.Process")
def test_stop_prefers_cooperative_exit(mock_process_cls, mock_event_cls):
    process, _ = _mock_process(exits_after_join=1)
    stop_event = MagicMock()
    mock_process_cls.return_value = process
    mock_event_cls.return_value = stop_event
    manager = StimulationProcessManager()
    manager.start("classic")

    manager.stop()

    stop_event.set.assert_called_once_with()
    process.terminate.assert_not_called()
    process.kill.assert_not_called()
    process.close.assert_called_once_with()
    assert manager._process is None and manager._stop_event is None
    assert manager.state is ChildProcessState.STOPPED


@patch("omnibci.infrastructure.stimulation.process_manager.mp.Event")
@patch("omnibci.infrastructure.stimulation.process_manager.mp.Process")
def test_stop_escalates_through_kill(mock_process_cls, mock_event_cls):
    process, _ = _mock_process(exits_after_join=3)
    mock_process_cls.return_value = process
    mock_event_cls.return_value = MagicMock()
    manager = StimulationProcessManager()
    manager.start()

    manager.stop()

    process.terminate.assert_called_once_with()
    process.kill.assert_called_once_with()
    assert [call.kwargs["timeout"] for call in process.join.call_args_list] == [
        manager._GRACEFUL_TIMEOUT_S,
        manager._TERMINATE_TIMEOUT_S,
        manager._KILL_TIMEOUT_S,
    ]


@patch("omnibci.infrastructure.stimulation.process_manager.mp.Event")
@patch("omnibci.infrastructure.stimulation.process_manager.mp.Process")
def test_unstoppable_process_is_retained_and_blocks_restart(
    mock_process_cls, mock_event_cls
):
    process, _ = _mock_process(exits_after_join=None)
    stop_event = MagicMock()
    mock_process_cls.return_value = process
    mock_event_cls.return_value = stop_event
    manager = StimulationProcessManager()
    manager.start("classic")

    with pytest.raises(RuntimeError, match="still alive"):
        manager.stop()

    assert manager._process is process
    assert manager._stop_event is stop_event
    assert manager.state is ChildProcessState.FAILED
    with pytest.raises(RuntimeError, match="still alive"):
        manager.start("other")
    assert mock_process_cls.call_count == 1


@patch("omnibci.infrastructure.stimulation.process_manager.mp.Event")
@patch("omnibci.infrastructure.stimulation.process_manager.mp.Process")
def test_failed_startup_report_is_raised_and_child_is_reaped(
    mock_process_cls,
    mock_event_cls,
    startup_pipe,
):
    process, _ = _mock_process(exits_after_join=1)
    mock_process_cls.return_value = process
    mock_event_cls.return_value = MagicMock()
    startup_pipe.receiver.recv.return_value = StartupReport.failed(
        "Stimulation",
        stage="initialization",
        error=RuntimeError("renderer unavailable"),
    )
    manager = StimulationProcessManager()

    with pytest.raises(
        ChildProcessStartupError,
        match="renderer unavailable",
    ):
        manager.start("classic")

    assert manager.state is ChildProcessState.FAILED
    assert manager.is_alive is False
    assert manager._process is None


@patch("omnibci.infrastructure.stimulation.process_manager.mp.Event")
@patch("omnibci.infrastructure.stimulation.process_manager.mp.Process")
def test_startup_timeout_stops_child_and_remains_failed(
    mock_process_cls,
    mock_event_cls,
    startup_pipe,
):
    process, _ = _mock_process(exits_after_join=1)
    mock_process_cls.return_value = process
    mock_event_cls.return_value = MagicMock()
    startup_pipe.receiver.poll.return_value = False
    manager = StimulationProcessManager(startup_timeout_s=0.001)

    with pytest.raises(ChildProcessStartupTimeout):
        manager.start("classic")

    assert manager.state is ChildProcessState.FAILED
    assert manager._process is None


@patch("omnibci.infrastructure.stimulation.process_manager.mp.Event")
@patch("omnibci.infrastructure.stimulation.process_manager.mp.Process")
def test_ready_state_detects_child_that_dies_later(
    mock_process_cls,
    mock_event_cls,
):
    process, state = _mock_process()
    mock_process_cls.return_value = process
    mock_event_cls.return_value = MagicMock()
    manager = StimulationProcessManager()
    manager.start("classic")

    state["alive"] = False

    assert manager.is_ready is False
    assert manager.state is ChildProcessState.FAILED


@patch("omnibci.infrastructure.stimulation.application.Stimulation")
def test_child_entrypoint_forwards_stop_event(mock_stimulation_cls):
    stop_event = object()

    _run_stimulation(paradigm=None, stop_event=stop_event)

    mock_stimulation_cls.assert_called_once_with(stop_event=stop_event)
    mock_stimulation_cls.return_value.run.assert_called_once_with()
