"""Stimulation subprocess cooperative lifecycle and escalation semantics."""

from __future__ import annotations

from unittest.mock import MagicMock, patch
import threading

import pytest

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


def test_no_spawn_mode_is_a_noop() -> None:
    manager = StimulationProcessManager(auto_spawn=False)
    manager.start("demo")
    manager.stop()
    assert manager.is_alive is False


@patch("omnibci.infrastructure.stimulation.process_manager.mp.Event")
@patch("omnibci.infrastructure.stimulation.process_manager.mp.Process")
def test_start_passes_cooperative_stop_event(mock_process_cls, mock_event_cls):
    process, _ = _mock_process()
    stop_event = MagicMock()
    mock_process_cls.return_value = process
    mock_event_cls.return_value = stop_event
    manager = StimulationProcessManager()

    manager.start("classic")

    assert mock_process_cls.call_args.kwargs["args"] == ("classic", stop_event)
    assert manager.is_alive is True


@patch("omnibci.infrastructure.stimulation.process_manager.mp.Event")
@patch("omnibci.infrastructure.stimulation.process_manager.mp.Process")
def test_same_paradigm_start_is_idempotent(mock_process_cls, mock_event_cls):
    process, _ = _mock_process()
    mock_process_cls.return_value = process
    manager = StimulationProcessManager()

    manager.start("classic")
    manager.start("classic")

    assert mock_process_cls.call_count == 1
    assert mock_event_cls.call_count == 1


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
    with pytest.raises(RuntimeError, match="still alive"):
        manager.start("other")
    assert mock_process_cls.call_count == 1


@patch("omnibci.infrastructure.stimulation.application.Stimulation")
def test_child_entrypoint_forwards_stop_event(mock_stimulation_cls):
    stop_event = object()

    _run_stimulation(paradigm=None, stop_event=stop_event)

    mock_stimulation_cls.assert_called_once_with(stop_event=stop_event)
    mock_stimulation_cls.return_value.run.assert_called_once_with()
