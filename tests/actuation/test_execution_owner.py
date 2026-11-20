"""Device lifecycle stays on one owner while the event loop remains responsive."""
import asyncio
from contextlib import suppress
from pathlib import Path
import subprocess
import sys
import threading
from unittest.mock import AsyncMock, Mock

import pytest

from tests.actuation_helpers import actuation_payload
from omnibci.application.events import CommandRequest
from omnibci.infrastructure.actuation.application import Actuation
from omnibci.shared.events import Event, EventType


@pytest.fixture
def app():
    instance = Actuation(settings_snapshot={"CONFIG.generation": "test-run", "CONFIG.operation_id": "host-operation",
                                            "CONFIG.module_revisions": {"actuation": "a" * 64}})
    instance.event_bus = Mock(start=AsyncMock(), stop=AsyncMock())
    instance.peer_channel = Mock(start=AsyncMock(), stop=AsyncMock())
    instance._await_managed_connection = AsyncMock()
    instance._heartbeat_interval = 0.001
    yield instance
    executor = getattr(instance, "_device_executor", None)
    if executor is not None:
        executor.shutdown(wait=True)
    instance._close_event_queue()


def _results(app):
    return [call.args[0].data for call in app.event_bus.put_event.call_args_list
            if call.args[0].type == EventType.CommandResult]


def test_completed_device_cancellation_does_not_spin_the_event_loop():
    # Isolate a broken wait loop so a regression cannot hang the whole test run.
    subprocess.run([sys.executable, "-B", "-c", """
import asyncio
from omnibci.infrastructure.async_lifecycle import await_owned

async def main():
    result = asyncio.get_running_loop().create_future()
    result.set_exception(asyncio.CancelledError('device cancelled'))
    try:
        await await_owned(result)
    except asyncio.CancelledError:
        return
    raise AssertionError('device cancellation was swallowed')

asyncio.run(main())
"""], cwd=Path(__file__).resolve().parents[2], timeout=5, check=True, capture_output=True)


@pytest.mark.asyncio
async def test_setup_execute_quiesce_and_teardown_share_one_non_loop_thread(app):
    operations = []

    def record(name):
        operations.append((name, threading.get_ident()))

    def execute(command):
        record("execute")
        app.should_stop = True

    app.actuator = Mock(
        setup=lambda: record("setup"), execute=execute,
        quiesce=lambda: record("quiesce"), teardown=lambda: record("teardown"),
    )
    app._enqueue_command(Event(EventType.ActuationCommand, actuation_payload({'target_id': 1})))
    await app.serve()

    assert [name for name, _ in operations] == ["setup", "execute", "quiesce", "teardown"]
    assert len({owner for _, owner in operations}) == 1
    assert operations[0][1] != threading.get_ident()


@pytest.mark.asyncio
async def test_setup_does_not_block_the_control_loop(app):
    loop = asyncio.get_running_loop()
    tick = threading.Event()

    def setup():
        loop.call_soon_threadsafe(tick.set)
        assert tick.wait(1), "device setup blocked the control loop"
        app.should_stop = True

    app.actuator = Mock(setup=setup)
    await app.serve()
    app.actuator.teardown.assert_called_once()


@pytest.mark.asyncio
async def test_repeated_cancellation_waits_for_execution_before_cleanup(app):
    started, release = threading.Event(), threading.Event()
    operations = []

    def execute(command):
        started.set()
        assert release.wait(5)
        operations.append("finished")

    app.actuator = Mock(execute=execute,
                        quiesce=lambda: operations.append("quiesce"),
                        teardown=lambda: operations.append("teardown"))
    app._enqueue_command(Event(EventType.ActuationCommand, actuation_payload({'target_id': 1})))
    serving = asyncio.create_task(app.serve())
    try:
        assert await asyncio.to_thread(started.wait, 1)
        serving.cancel()
        await asyncio.sleep(0.01)
        serving.cancel()
        await asyncio.sleep(0.01)
        assert not serving.done()
        assert operations == []
    finally:
        release.set()
        with suppress(asyncio.CancelledError):
            await asyncio.wait_for(serving, 2)
    assert operations == ["finished", "quiesce", "teardown"]


@pytest.mark.asyncio
async def test_shutdown_releases_resources_but_keeps_quiescence_failure(app):
    app.actuator = Mock(quiesce=Mock(side_effect=RuntimeError("device still moving")))
    app._await_managed_connection.side_effect = lambda: setattr(app, "should_stop", True)

    with pytest.raises(Exception, match="device still moving|cleanup"):
        await app.serve()

    app.actuator.teardown.assert_called_once()
    app.peer_channel.stop.assert_awaited_once()
    app.event_bus.stop.assert_awaited_once()


@pytest.mark.asyncio
async def test_failed_settlement_cannot_be_bypassed_by_resume(app):
    app.actuator = Mock(quiesce=Mock(side_effect=RuntimeError("device still moving")))
    await app._settle_actuation(Event(EventType.SettleActuation, CommandRequest("settle")))
    assert not _results(app)[-1].ok
    await app._resume_actuation(Event(EventType.ResumeActuation, CommandRequest("resume")))
    assert not _results(app)[-1].ok
    assert app._paused
    app._enqueue_command(Event(EventType.ActuationCommand, actuation_payload({'target_id': 2})))
    assert app.command_queue.empty()
    app.actuator.execute.assert_not_called()


@pytest.mark.asyncio
async def test_cancelled_settlement_keeps_resume_blocked_until_confirmed(app):
    started, release = threading.Event(), threading.Event()

    def quiesce():
        started.set()
        assert release.wait(5)

    app.actuator = Mock(quiesce=quiesce)
    settlement = asyncio.create_task(app._settle_actuation(
        Event(EventType.SettleActuation, CommandRequest("settle")),
    ))
    try:
        assert await asyncio.to_thread(started.wait, 1)
        settlement.cancel()
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await settlement
    await app._resume_actuation(Event(EventType.ResumeActuation, CommandRequest("resume")))
    assert not _results(app)[-1].ok
    assert app._paused
    await app._settle_actuation(Event(EventType.SettleActuation, CommandRequest("retry-settle")))
    await app._resume_actuation(Event(EventType.ResumeActuation, CommandRequest("retry-resume")))
    assert _results(app)[-1].ok


@pytest.mark.asyncio
async def test_settlement_confirms_physical_state_after_execution_failure(app):
    started, release = threading.Event(), threading.Event()

    def execute(command):
        started.set()
        assert release.wait(5)
        raise RuntimeError("device failed after output began")

    app.actuator = Mock(execute=execute)
    app._enqueue_command(Event(EventType.ActuationCommand, actuation_payload({'target_id': 1}, command_id='4')))
    execution = asyncio.create_task(app.execute_command())
    settlement = None
    try:
        assert await asyncio.to_thread(started.wait, 1)
        settlement = asyncio.create_task(app._settle_actuation(
            Event(EventType.SettleActuation, CommandRequest("settle")),
        ))
        await asyncio.sleep(0.01)
    finally:
        release.set()
        await execution
        if settlement is not None:
            await settlement
    app.actuator.quiesce.assert_called_once()
    assert _results(app)[-1].ok
    faults = [call.args[0].data for call in app.event_bus.put_event.call_args_list
              if call.args[0].type == EventType.ActuationFault]
    assert faults[0]["code"] == "COMMAND_FAILED" and faults[0]["command_id"] == '4'


@pytest.mark.asyncio
async def test_shutdown_accounts_for_pending_intent_without_executing_it(app):
    app.actuator = Mock()
    app._enqueue_command(Event(EventType.ActuationCommand, actuation_payload({'target_id': 3}, command_id='8')))
    app._await_managed_connection.side_effect = lambda: setattr(app, "should_stop", True)
    await app.serve()
    app.actuator.execute.assert_not_called()
    assert app.command_queue.empty()
    acknowledgments = [call.args[0].data for call in app.event_bus.put_event.call_args_list
                       if call.args[0].type == EventType.ActuationAck]
    assert acknowledgments[0]["command_id"] == '8'
    assert acknowledgments[0]["status"] == "cancelled"


@pytest.mark.asyncio
async def test_failed_shutdown_receipt_does_not_skip_device_or_ipc_cleanup(app):
    app.actuator = Mock()
    app._enqueue_command(Event(EventType.ActuationCommand, actuation_payload({'target_id': 3}, command_id='8')))
    app._await_managed_connection.side_effect = lambda: setattr(app, "should_stop", True)
    app.event_bus.put_event.side_effect = RuntimeError("result channel closed")
    with pytest.raises(Exception, match="result channel closed|cleanup"):
        await app.serve()
    app.actuator.quiesce.assert_called_once()
    app.actuator.teardown.assert_called_once()
    app.peer_channel.stop.assert_awaited_once()
    app.event_bus.stop.assert_awaited_once()


@pytest.mark.asyncio
async def test_shutdown_still_quiesces_when_settlement_was_cancelled_while_joining(app):
    started, release = threading.Event(), threading.Event()

    def execute(command):
        started.set()
        assert release.wait(5)

    app.actuator = Mock(execute=execute)
    app._enqueue_command(Event(EventType.ActuationCommand, actuation_payload({'target_id': 1})))
    serving = asyncio.create_task(app.serve())
    settlement = None
    try:
        assert await asyncio.to_thread(started.wait, 1)
        app._request_settlement(Event(EventType.SettleActuation, CommandRequest("settle")))
        settlement = next(iter(app._settlement_tasks))
        await asyncio.sleep(0.01)
        settlement.cancel()
        await asyncio.sleep(0.01)
    finally:
        release.set()
        if settlement is not None:
            with pytest.raises(asyncio.CancelledError):
                await settlement
        app.should_stop = True
        await serving
    app.actuator.quiesce.assert_called_once()
    app.actuator.teardown.assert_called_once()
