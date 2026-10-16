"""Thread-safe request/result correlation for subprocess control commands."""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from omnibci.application.events import (
    CommandFailureKind,
    CommandRequest,
    CommandResult,
)
from omnibci.application.services.command_result_service import (
    CommandExecutionError,
    CommandRejectedError,
    CommandResultBroker,
    CommandTimeoutError,
)
from omnibci.shared.events import Event, EventType


class _Engine:
    is_engine_ready = True

    def __init__(self) -> None:
        self.dispatched: list[Event] = []
        self.on_dispatch = None

    def dispatch(self, event: Event) -> None:
        self.dispatched.append(event)
        if self.on_dispatch is not None:
            self.on_dispatch(event)


def _result(
    request_event: Event,
    *,
    ok: bool,
    message=None,
    error_code=None,
    failure_kind=None,
):
    request = request_event.data
    assert isinstance(request, CommandRequest)
    return Event(
        EventType.CommandResult,
        CommandResult(
            command_id=request.command_id,
            command=request_event.type.name,
            ok=ok,
            message=message,
            error_code=error_code,
            failure_kind=failure_kind,
        ),
    )


def test_success_returns_only_after_matching_result():
    engine = _Engine()
    broker = CommandResultBroker(engine, timeout_s=0.2)
    engine.on_dispatch = lambda event: broker.on_result(_result(event, ok=True))

    result = broker.request(EventType.ConnectDataProvider)

    assert result.ok is True
    assert result.command == "ConnectDataProvider"
    assert broker.pending_count == 0


def test_rejection_preserves_consumer_detail():
    engine = _Engine()
    broker = CommandResultBroker(engine, timeout_s=0.2)
    engine.on_dispatch = lambda event: broker.on_result(_result(
        event,
        ok=False,
        message="not connected",
        error_code="DATA_SOURCE_NOT_CONNECTED",
    ))

    with pytest.raises(CommandRejectedError) as raised:
        broker.request(EventType.StartStream)

    assert str(raised.value) == "not connected"
    assert raised.value.result.error_code == "DATA_SOURCE_NOT_CONNECTED"
    assert broker.pending_count == 0


def test_mismatched_command_name_is_execution_failure():
    engine = _Engine()
    broker = CommandResultBroker(engine, timeout_s=0.2)

    def respond_with_wrong_command(event: Event) -> None:
        request = event.data
        assert isinstance(request, CommandRequest)
        broker.on_result(Event(
            EventType.CommandResult,
            CommandResult(
                command_id=request.command_id,
                command="StopStream",
                ok=True,
            ),
        ))

    engine.on_dispatch = respond_with_wrong_command

    with pytest.raises(CommandExecutionError) as raised:
        broker.request(EventType.StartStream)

    assert raised.value.result.error_code == "COMMAND_RESULT_MISMATCH"


def test_execution_failure_is_distinct_from_rejection():
    engine = _Engine()
    broker = CommandResultBroker(engine, timeout_s=0.2)
    engine.on_dispatch = lambda event: broker.on_result(_result(
        event,
        ok=False,
        message="device unavailable",
        error_code="OSError",
        failure_kind=CommandFailureKind.EXECUTION_FAILED,
    ))

    with pytest.raises(CommandExecutionError) as raised:
        broker.request(EventType.ConnectDataProvider)

    assert raised.value.result.error_code == "OSError"


def test_first_terminal_result_wins_over_duplicate():
    engine = _Engine()
    broker = CommandResultBroker(engine, timeout_s=0.2)

    def respond_twice(event: Event) -> None:
        broker.on_result(_result(event, ok=True))
        broker.on_result(_result(
            event,
            ok=False,
            message="late duplicate",
            error_code="DUPLICATE",
        ))

    engine.on_dispatch = respond_twice

    result = broker.request(EventType.StartStream)

    assert result.ok is True


def test_timeout_removes_waiter_and_late_result_is_ignored(caplog):
    engine = _Engine()
    broker = CommandResultBroker(engine, timeout_s=0.01)

    with pytest.raises(CommandTimeoutError):
        broker.request(EventType.StopStream)

    assert broker.pending_count == 0
    broker.on_result(_result(engine.dispatched[0], ok=True))
    assert "Late or unknown command result ignored" in caplog.text


def test_engine_failure_completes_pending_command_before_its_deadline():
    engine = _Engine()
    broker = CommandResultBroker(engine, timeout_s=2)
    dispatched = threading.Event()
    engine.on_dispatch = lambda event: dispatched.set()
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(broker.request, EventType.StartStream)
        assert dispatched.wait(1)
        engine.is_engine_ready = False
        with pytest.raises(CommandExecutionError) as raised:
            future.result(timeout=0.5)

    assert raised.value.result.command_id == engine.dispatched[0].data.command_id
    assert raised.value.result.command == EventType.StartStream.name
    assert raised.value.result.error_code == "ENGINE_STOPPED"
    assert broker.pending_count == 0


def test_received_result_wins_when_engine_stops_at_the_same_time():
    engine = _Engine()
    broker = CommandResultBroker(engine, timeout_s=0.2)

    def respond_and_stop(event):
        broker.on_result(_result(event, ok=True))
        engine.is_engine_ready = False

    engine.on_dispatch = respond_and_stop
    assert broker.request(EventType.StopStream).ok
    assert broker.pending_count == 0


def test_result_arriving_during_engine_failure_check_wins():
    class StoppingEngine(_Engine):
        @property
        def is_engine_ready(self):
            broker.on_result(_result(self.dispatched[0], ok=True))
            return False

    engine = StoppingEngine()
    broker = CommandResultBroker(engine, timeout_s=0.5)

    assert broker.request(EventType.StopStream).ok
    assert broker.pending_count == 0


def test_concurrent_results_do_not_cross_requests():
    engine = _Engine()
    broker = CommandResultBroker(engine, timeout_s=1.0)
    returned: dict[str, CommandResult] = {}

    def call(name: str, event_type: EventType) -> None:
        returned[name] = broker.request(event_type)

    first = threading.Thread(target=call, args=("connect", EventType.ConnectDataProvider))
    second = threading.Thread(target=call, args=("stream", EventType.StartStream))
    first.start()
    second.start()
    deadline = time.monotonic() + 1.0
    while len(engine.dispatched) < 2 and time.monotonic() < deadline:
        time.sleep(0.001)
    assert len(engine.dispatched) == 2

    # Complete in reverse order; command_id correlation must keep results apart.
    for event in reversed(engine.dispatched):
        broker.on_result(_result(event, ok=True))
    first.join(timeout=1)
    second.join(timeout=1)

    assert returned["connect"].command == "ConnectDataProvider"
    assert returned["stream"].command == "StartStream"
    assert broker.pending_count == 0
