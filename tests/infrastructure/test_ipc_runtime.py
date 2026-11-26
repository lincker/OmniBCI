"""Server IPC owns the stimulation listener independently of any Engine graph."""
import socket
import time
from threading import Event as Signal

import pytest

from omnibci.infrastructure.ipc.peer_configs import stimulation_peer_config
from omnibci.infrastructure.ipc.runtime import IpcRuntime
from omnibci.shared.events import Event, EventType


@pytest.mark.integration
def test_runtime_owns_listener_until_explicit_close(unused_tcp_port):
    runtime = IpcRuntime()
    event = Signal()
    runtime.subscribe(EventType.StimulationAck, lambda _: event.set())
    with pytest.raises(RuntimeError, match="not running"):
        runtime.dispatch(Event(EventType.StimulationAck, {}))
    runtime.start()
    try:
        config = stimulation_peer_config("127.0.0.1", unused_tcp_port)
        runtime.ensure_peer_sync("stimulation", config, identity_key="session")
        with socket.create_connection(("127.0.0.1", unused_tcp_port), timeout=2):
            runtime.ensure_peer_sync("stimulation", config, identity_key="session")
            deadline = time.monotonic() + 2
            while not runtime.is_peer_connected("stimulation") and time.monotonic() < deadline:
                time.sleep(.005)
            assert runtime.is_peer_connected("stimulation")
            runtime.dispatch(Event(EventType.StimulationAck, {}))
            assert event.wait(2)
        runtime.close_peer_sync("stimulation")
        assert not runtime.is_peer_ready("stimulation")
        with socket.socket() as listener:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind(("127.0.0.1", unused_tcp_port))
    finally:
        runtime.stop()
    assert runtime.shutdown_complete
    runtime.stop()


@pytest.mark.integration
def test_disconnected_correlated_write_reports_fault_for_its_channel(unused_tcp_port):
    from omnibci.application.events import CommandRequest
    received, ready = [], Signal()
    runtime = IpcRuntime()
    def observe(event):
        received.append(event.data)
        ready.set()
    runtime.subscribe(EventType.PeerDeliveryFault, observe)
    runtime.start()
    try:
        runtime.ensure_peer_sync("stimulation", stimulation_peer_config("127.0.0.1", unused_tcp_port), identity_key="session")
        runtime.dispatch(Event(EventType.StartStimTrial, CommandRequest("unsent", {"n_trials": 1, "trial_params": [{}]})))
        assert ready.wait(3)
        fault, = received
        assert fault["peer"] == "stimulation" and runtime.is_current_peer_channel("stimulation", fault["channel_id"])
        assert not runtime.is_peer_ready("stimulation")
    finally:
        runtime.stop()


@pytest.mark.parametrize("role,command,other_command", [
    ("actuation", EventType.SettleActuation, EventType.PauseStimulation),
    ("stimulation", EventType.PauseStimulation, EventType.ResumeActuation),
])
def test_only_the_command_owner_can_publish_its_result(role, command, other_command):
    from unittest.mock import Mock
    from omnibci.application.events import CommandResult
    from omnibci.infrastructure.ipc.peer_channel import PeerChannel
    from omnibci.infrastructure.ipc.peer_configs import actuation_peer_config

    factory = actuation_peer_config if role == "actuation" else stimulation_peer_config
    bus = Mock()
    channel = PeerChannel(factory("127.0.0.1", 0), bus)
    def result(kind):
        return Event(EventType.CommandResult, CommandResult("request", kind.name, True))
    assert not channel._on_inbound(result(other_command))
    bus.put_event.assert_not_called()
    valid = result(command)
    assert channel._on_inbound(valid)
    bus.put_event.assert_called_once_with(valid)


def test_startup_failure_with_clean_rollback_leaves_no_owned_resources():
    from unittest.mock import AsyncMock
    runtime = IpcRuntime()
    runtime._gateway.start = AsyncMock(side_effect=RuntimeError("gateway bind failed"))
    with pytest.raises(RuntimeError, match="gateway bind failed"):
        runtime.start()
    assert runtime.shutdown_complete
    assert not runtime.has_owned_resources
    runtime.stop()


def test_startup_failure_with_rollback_failure_preserves_original_error_and_ownership():
    from unittest.mock import AsyncMock
    runtime = IpcRuntime()
    runtime._gateway.start = AsyncMock(side_effect=RuntimeError("gateway bind failed"))
    runtime._gateway.stop = AsyncMock(side_effect=OSError("gateway stop failed"))
    with pytest.raises(RuntimeError, match="gateway bind failed") as exc_info:
        runtime.start()
    assert any("gateway stop failed" in note for note in getattr(exc_info.value, "__notes__", ()))
    assert runtime.has_owned_resources
    with pytest.raises(OSError, match="gateway stop failed"):
        runtime.stop()


def test_stop_handles_closed_loop_race_and_joins_thread():
    from unittest.mock import MagicMock
    runtime = IpcRuntime()
    closed_loop = MagicMock()
    closed_loop.call_soon_threadsafe.side_effect = RuntimeError("Event loop is closed")
    thread = MagicMock()
    thread.is_alive.return_value = False
    runtime._thread = thread
    runtime._loop = closed_loop
    runtime._exit = MagicMock()
    runtime._finished.set_result(None)
    runtime.stop()
    thread.join.assert_called_once_with(runtime._TIMEOUT)
