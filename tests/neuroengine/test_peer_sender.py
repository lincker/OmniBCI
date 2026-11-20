"""Output peers must not hold up the shared EventBus consumer."""

import asyncio
import queue

import pytest

from omnibci.application.events import CommandRequest, CommandResult
from omnibci.infrastructure.ipc.event_bus import EventBus
from omnibci.infrastructure.ipc.network import PeerConnectionChanged
from omnibci.infrastructure.ipc import peer_channel as channel_module
from omnibci.infrastructure.ipc.peer_channel import PeerChannel
from omnibci.infrastructure.ipc.routing import PeerConfig, RouteSpec
from omnibci.shared.events import Event, EventType


class SlowTransport:
    def __init__(self):
        self.is_connected = True
        self.connection_token = object()
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.sent = []

    def set_inbound_handler(self, handler):
        self.inbound_handler = handler

    def set_liveness_handler(self, handler):
        self.liveness_handler = handler

    async def start(self):
        self.is_connected = True

    async def stop(self):
        self.is_connected = False

    async def send(self, event, *, connection_token=None):
        if connection_token is not self.connection_token:
            raise PeerConnectionChanged("connection replaced")
        self.entered.set()
        await self.release.wait()
        self.sent.append(event)
        return True


def make_channel(transport, bus, **options):
    return PeerChannel(PeerConfig(
        name="stimulation",
        transport=transport,
        routes=RouteSpec(
            outbound=frozenset({EventType.TrialCompleted, EventType.PauseStimulation}),
            inbound=frozenset({EventType.CommandResult}),
        ),
    ), bus, **options)


def test_slow_peer_does_not_block_later_command_result_on_real_bus():
    async def run():
        bus = EventBus(queue.Queue())
        transport = SlowTransport()
        channel = make_channel(transport, bus)
        received = asyncio.Event()
        bus.register_event_listener(EventType.CommandResult, lambda event: received.set())
        await channel.start()
        await bus.start()
        try:
            bus.put_event(Event(EventType.TrialCompleted, "trial"))
            await asyncio.wait_for(transport.entered.wait(), 0.5)
            bus.put_event(Event(EventType.CommandResult, "unrelated receipt"))
            await asyncio.wait_for(received.wait(), 0.1)
            assert transport.sent == []
        finally:
            transport.release.set()
            await channel.stop()
            await bus.stop()

    asyncio.run(run())


def fault(queue_):
    event = queue_.get_nowait()
    assert event.type == EventType.PeerDeliveryFault
    assert queue_.empty()
    return event.data


def command(command_id):
    return Event(EventType.PauseStimulation, CommandRequest(command_id))


def test_terminal_send_waits_for_write_and_preserves_queue_order():
    async def run():
        transport = SlowTransport()
        channel = make_channel(transport, EventBus(queue.Queue()))
        await channel.start()
        first = Event(EventType.TrialCompleted, 1)
        assert channel._forward(first)
        await transport.entered.wait()
        final = command("terminal")
        pending = asyncio.create_task(channel.send(final))
        await asyncio.sleep(0)
        assert not pending.done()
        transport.release.set()
        assert await pending is True
        assert transport.sent == [first, final]
        await channel.stop()

    asyncio.run(run())


def test_prepared_listener_without_peer_ignores_trials_and_can_connect_later():
    async def run():
        events = queue.Queue()
        transport = SlowTransport()
        channel = make_channel(transport, EventBus(events))
        await channel.start()
        transport.is_connected = False
        assert channel._forward(Event(EventType.TrialCompleted, "trial")) is False
        assert channel.is_ready
        assert events.empty()
        transport.is_connected = True
        transport.release.set()
        assert await channel.send(command("connected"))
        await channel.stop()

    asyncio.run(run())


def test_queue_overflow_rejects_only_unsent_commands_and_requires_restart():
    async def run():
        events = queue.Queue()
        transport = SlowTransport()
        channel = make_channel(transport, EventBus(events), outbound_capacity=1)
        await channel.start()
        original_id = channel.channel_id
        active = asyncio.create_task(channel.send(command("active")))
        await transport.entered.wait()
        queued = asyncio.create_task(channel.send(command("queued")))
        await asyncio.sleep(0)
        assert channel._forward(command("rejected")) is False
        assert await active is False
        assert await queued is False
        await asyncio.gather(channel._sender_task, return_exceptions=True)
        failure = fault(events)
        assert failure["channel_id"] == original_id
        assert failure["unsent_command_ids"] == ["queued", "rejected"]
        assert channel.is_ready is False
        assert channel.is_connected is False
        transport.connection_token = object()
        transport.is_connected = True
        assert channel._forward(command("later")) is False
        assert fault(events)["unsent_command_ids"] == ["later"]
        assert transport.sent == []
        await channel.start()
        assert channel.channel_id != original_id
        transport.release.set()
        fresh = command("fresh")
        assert await channel.send(fresh)
        assert transport.sent == [fresh]
        await channel.stop()

    asyncio.run(run())


def test_stop_retains_sender_ownership_if_cancellation_is_not_finished(monkeypatch):
    monkeypatch.setattr(channel_module, "_SENDER_STOP_TIMEOUT_S", 0.01)

    class SlowCancellation(SlowTransport):
        async def send(self, event, *, connection_token=None):
            self.entered.set()
            try:
                await self.release.wait()
            except asyncio.CancelledError:
                await self.release.wait()
            return True

    async def run():
        transport = SlowCancellation()
        channel = make_channel(transport, EventBus(queue.Queue()))
        await channel.start()
        pending = asyncio.create_task(channel.send(command("active")))
        await transport.entered.wait()
        sender = channel._sender_task
        with pytest.raises(ExceptionGroup, match="cleanup failed"):
            await channel.stop()
        assert channel.is_dirty
        assert not channel.is_connected
        assert channel._sender_task is sender
        assert not sender.done()
        assert channel.has_lifecycle_resources
        transport.release.set()
        assert await pending is False
        await channel.stop()
        assert not channel.has_lifecycle_resources

    asyncio.run(run())


def test_write_timeout_is_unknown_but_queued_command_is_unsent(monkeypatch):
    monkeypatch.setattr(channel_module, "_SEND_TIMEOUT_S", 0.01)

    async def run():
        events = queue.Queue()
        transport = SlowTransport()
        channel = make_channel(transport, EventBus(events))
        await channel.start()
        active = asyncio.create_task(channel.send(command("active")))
        await transport.entered.wait()
        assert channel._forward(command("queued"))
        assert await active is False
        await channel._sender_task
        assert fault(events)["unsent_command_ids"] == ["queued"]
        assert not transport.is_connected
        await channel.stop()

    asyncio.run(run())


def test_reconnection_does_not_replay_a_queued_command():
    async def run():
        events = queue.Queue()
        transport = SlowTransport()
        channel = make_channel(transport, EventBus(events))
        await channel.start()
        first = command("first")
        assert channel._forward(first)
        await transport.entered.wait()
        queued = asyncio.create_task(channel.send(command("queued")))
        await asyncio.sleep(0)
        transport.connection_token = object()
        transport.release.set()
        assert await queued is False
        await channel._sender_task
        assert transport.sent == [first]
        assert fault(events)["unsent_command_ids"] == ["queued"]
        await channel.stop()

    asyncio.run(run())


def test_stop_finishes_pending_sends_without_replaying_on_restart():
    async def run():
        events = queue.Queue()
        transport = SlowTransport()
        channel = make_channel(transport, EventBus(events))
        await channel.start()
        active = asyncio.create_task(channel.send(command("active")))
        await transport.entered.wait()
        queued = asyncio.create_task(channel.send(command("queued")))
        await asyncio.sleep(0)
        await asyncio.wait_for(channel.stop(), 0.2)
        assert await active is False
        assert await queued is False
        assert events.empty()
        assert not channel.has_lifecycle_resources
        await channel.start()
        transport.release.set()
        fresh = command("fresh")
        assert await channel.send(fresh)
        assert transport.sent == [fresh]
        await channel.stop()

    asyncio.run(run())


def test_lost_node_result_never_becomes_an_unsent_command_or_fake_receipt():
    async def run():
        events = queue.Queue()
        transport = SlowTransport()
        channel = PeerChannel(PeerConfig(
            name="engine", transport=transport,
            routes=RouteSpec(frozenset({EventType.CommandResult}), frozenset()),
        ), EventBus(events), outbound_capacity=1)
        await channel.start()
        result = Event(EventType.CommandResult, CommandResult("executed", "Stop", True))
        pending = asyncio.create_task(channel.send(result))
        await transport.entered.wait()
        assert channel._forward(result)
        assert channel._forward(result) is False
        assert await pending is False
        await asyncio.gather(channel._sender_task, return_exceptions=True)
        assert fault(events)["unsent_command_ids"] == []
        assert not transport.is_connected
        await channel.stop()

    asyncio.run(run())


def test_cancelling_a_terminal_send_closes_the_failed_channel():
    async def run():
        events = queue.Queue()
        transport = SlowTransport()
        channel = make_channel(transport, EventBus(events))
        await channel.start()
        pending = asyncio.create_task(channel.send(command("active")))
        await transport.entered.wait()
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        await asyncio.gather(channel._sender_task, return_exceptions=True)
        assert fault(events)["unsent_command_ids"] == []
        assert not channel.is_ready
        assert not transport.is_connected
        await channel.stop()

    asyncio.run(run())
