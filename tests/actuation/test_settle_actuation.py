"""Output settlement requires joined execution and explicit driver confirmation."""
import asyncio
import threading
from queue import Queue
from unittest.mock import Mock

from omnibci.application.events import CommandRequest
from omnibci.domain.ports.effector import Effector
from omnibci.infrastructure.actuation.actuator import Actuator
from omnibci.infrastructure.actuation.application import Actuation
from omnibci.infrastructure.plugins.effectors.logger.effector import LogEffector
from omnibci.shared.events import Event, EventType


def app(actuator):
    instance = Actuation.__new__(Actuation)
    instance.command_queue = Queue(maxsize=1)
    instance.event_bus = Mock()
    instance.actuator = actuator
    instance._paused = False
    instance._execution_task = None
    instance._settlement_tasks = set()
    instance._settlement_lock = asyncio.Lock()
    instance._max_command_age = 0
    instance._runtime_snapshot = {"CONFIG.revision": "revision", "CONFIG.generation": "generation"}
    return instance


def receipts(instance):
    return [call.args[0].data for call in instance.event_bus.put_event.call_args_list
            if call.args[0].type == EventType.CommandResult]


def test_settlement_joins_execution_cancels_pending_and_keeps_dispatch_nonblocking():
    async def scenario():
        started = threading.Event()
        release = threading.Event()
        operations = []
        actuator = Mock()

        def play(payload):
            operations.append(("execute", payload["target_id"]))
            started.set()
            assert release.wait(5)
            operations.append(("finished", payload["target_id"]))

        actuator.execute.side_effect = play
        actuator.quiesce.side_effect = lambda: operations.append(("quiesce", None))
        instance = app(actuator)
        instance._enqueue_command(Event(EventType.ActuationCommand, {"target_id": 1, "seq": 1}))
        execution = asyncio.create_task(instance.execute_command())
        try:
            assert await asyncio.to_thread(started.wait, 1)
            instance._enqueue_command(Event(EventType.ActuationCommand, {"target_id": 2, "seq": 2}))
            # Synchronous listener returns immediately: EventBus can keep forwarding heartbeats.
            instance._request_settlement(Event(EventType.SettleActuation, CommandRequest("settle")))
            tasks = tuple(instance._settlement_tasks)
            assert instance._paused
            await asyncio.sleep(0)
            assert receipts(instance) == []
            assert instance.command_queue.empty()
            actuator.quiesce.assert_not_called()
            instance._enqueue_command(Event(EventType.ActuationCommand, {"target_id": 3, "seq": 3}))
            assert instance.command_queue.empty()
            release.set()
            await execution
            await asyncio.gather(*tasks)
        finally:
            release.set()
            await execution
        assert operations == [("execute", 1), ("finished", 1), ("quiesce", None)]
        result = receipts(instance)[0]
        assert result.ok
        assert result.data == {
            "paused": True, "pending_cancelled": 1,
            "execution_complete": True, "outputs_settled": True,
            "generation": "generation", "config_revision": "revision",
        }
        cancelled = [call.args[0].data["seq"] for call in instance.event_bus.put_event.call_args_list
                     if call.args[0].type == EventType.ActuationAck
                     and call.args[0].data["status"] == "cancelled"]
        assert cancelled == [2, 3]

    asyncio.run(scenario())


def test_legacy_effector_cannot_claim_physical_completion():
    class LegacyEffector(Effector):
        def setup(self): pass
        def teardown(self): pass
        def play(self, command): pass

    async def scenario():
        actuator = Actuator()
        actuator.effector = LegacyEffector()
        instance = app(actuator)
        await instance._settle_actuation(Event(EventType.SettleActuation, CommandRequest("settle")))
        result = receipts(instance)[0]
        assert not result.ok
        assert result.error_code == "ACTUATION_SETTLE_FAILED"
        assert "cannot confirm" in result.message
        assert result.data is None
        assert instance._paused

    asyncio.run(scenario())


def test_logger_explicitly_confirms_quiescence():
    async def scenario():
        actuator = Actuator()
        actuator.effector = LogEffector(parameters={})
        instance = app(actuator)
        await instance._settle_actuation(Event(EventType.SettleActuation, CommandRequest("settle")))
        assert receipts(instance)[0].ok
        assert receipts(instance)[0].data["outputs_settled"]

    asyncio.run(scenario())
