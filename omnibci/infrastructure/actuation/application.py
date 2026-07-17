"""
Actuation — main class of the actuation subsystem process.

Mirrors stimulation's application.Stimulation: an EventBus + a PeerChannel
that dials into the engine's actuation port, and an Actuator that drives the
loaded Effector plugin. Differences from stimulation:

  - commands are executed one at a time as they arrive (no busy-gating —
    play() is expected to be short; long-running devices go async internally);
  - a heartbeat task publishes ActuationState every watchdog_timeout_s / 2 —
    the engine's link watchdog treats inbound silence past the timeout as
    LINK_LOST, so a dialled-in peer must keep talking;
  - one failed command → ActuationFault + keep serving; only a failed
    setup() exits the process (with a Fault as its last word). Stopping is
    process-level (ActuationProcessManager.terminate / Ctrl-C) — there is no
    StopActuation wire value in v1.
"""

import asyncio
import logging
import multiprocessing as mp
from queue import Queue

from omnibci.infrastructure.ipc.event_bus import EventBus, Event, EventType
from omnibci.infrastructure.outbound import make_actuation_ack, make_actuation_fault
from omnibci.infrastructure.neuroengine.gateway.peer_channel import PeerChannel
from omnibci.infrastructure.actuation.engine_channel import engine_peer_config
from omnibci.infrastructure.actuation.actuator import Actuator

logger = logging.getLogger(__name__)


class Actuation:
    def __init__(self):
        self.event_bus = EventBus()
        self.event_bus.set_event_queue(queue=mp.Queue())

        from omnibci.conf import settings
        host = settings.get("ACTUATION.server_ip", "127.0.0.1")
        port = int(settings.get("ACTUATION.server_port", 8001))
        self._heartbeat_interval = float(
            settings.get("ACTUATION.watchdog_timeout_s", 1.0)) / 2.0
        self.peer_channel = PeerChannel(engine_peer_config(host, port), self.event_bus)

        self.should_stop = False
        self.command_queue = Queue()
        self.actuator = Actuator()
        self._heartbeat_task = None

    def run(self):
        logger.info('---ActuationMain run---')
        asyncio.run(self.serve())

    async def serve(self):
        try:
            await self.start()
        except Exception as exc:
            # Without a working effector the subsystem cannot serve; leave a
            # Fault as the last word (best effort — the channel may not have
            # connected yet) and exit.
            logger.error("Actuation setup failed: %s", exc)
            self.event_bus.put_event(Event(
                EventType.ActuationFault,
                make_actuation_fault("SETUP_FAILED", message=str(exc)),
            ))
            await asyncio.sleep(0.5)   # give the channel a beat to flush
            await self.stop()
            return

        self._heartbeat_task = asyncio.create_task(self._heartbeat())
        try:
            while not self.should_stop:
                await self.execute_command()
                await asyncio.sleep(0.01)
        finally:
            self._heartbeat_task.cancel()
            self.actuator.teardown()
            await self.stop()

    async def start(self):
        logger.info('---ActuationMain start---')
        await self.event_bus.start()
        self.peer_channel.start()
        self.event_bus.register_event_listener(
            event_type=EventType.ActuationCommand, handler=self._enqueue_command)
        self.actuator.setup()

    async def execute_command(self):
        if self.command_queue.empty():
            return
        command = self.command_queue.get()
        payload = command.data if isinstance(command.data, dict) else {}
        target = payload.get("target_id")
        try:
            self.actuator.execute(payload)
            reply = Event(EventType.ActuationAck,
                          make_actuation_ack(target_id=target))
        except Exception as exc:
            logger.error("Effector command failed (target_id=%s): %s", target, exc)
            reply = Event(EventType.ActuationFault,
                          make_actuation_fault("COMMAND_FAILED",
                                               target_id=target, message=str(exc)))
        # PeerChannel's outbound subscription forwards these to the engine.
        self.event_bus.put_event(reply)

    def _enqueue_command(self, command: Event):
        if command.type == EventType.ActuationCommand:
            self.command_queue.put(command)

    async def _heartbeat(self):
        """Keep the engine's link watchdog quiet while we are alive."""
        while True:
            await asyncio.sleep(self._heartbeat_interval)
            self.event_bus.put_event(Event(
                EventType.ActuationState, {"heartbeat": True}))

    async def stop(self):
        logger.info('---ActuationMain stop---')
        await self.peer_channel.stop()
        self.event_bus.stop()
