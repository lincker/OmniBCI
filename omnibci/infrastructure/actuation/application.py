"""
Actuation — main class of the actuation subsystem process.

Mirrors stimulation's application.Stimulation: an EventBus + a PeerChannel
that dials into the engine's actuation port, and an Actuator that drives the
loaded Effector plugin. Differences from stimulation:

  - the command stream is an *intent* stream: a one-slot latest-value mailbox
    retains only the newest pending command (last-write-wins) and immediately
    acks each replaced command with status="superseded" — a backlog must never
    make the device replay stale intents in order;
  - play() runs off the event loop (asyncio.to_thread), so a slow device
    action cannot starve the heartbeat and trip the engine watchdog into a
    false LINK_LOST; commands stay serial (one thread at a time);
  - a heartbeat task publishes ActuationState every watchdog_timeout_s / 2 —
    the engine's link watchdog treats inbound silence past the timeout as
    LINK_LOST, so a dialled-in peer must keep talking;
  - one failed command → ActuationFault + keep serving; only a failed
    setup() exits the process (with a Fault as its last word). Stopping is
    process-level (ActuationProcessManager stop event / Ctrl-C) — there is no
    StopActuation wire value in v1.
"""

import asyncio
import logging
import multiprocessing as mp
import time
from queue import Empty, Full, Queue
from typing import Any

from omnibci.shared.events import Event, EventType
from omnibci.infrastructure.ipc.event_bus import EventBus
from omnibci.infrastructure.outbound import make_actuation_ack, make_actuation_fault
from omnibci.infrastructure.neuroengine.gateway.peer_channel import PeerChannel
from omnibci.infrastructure.actuation.engine_channel import engine_peer_config
from omnibci.infrastructure.actuation.actuator import Actuator
from omnibci.infrastructure.actuation.contracts import (
    InvalidActuationCommand,
    ValidatedActuationCommand,
    validate_actuation_command,
)

logger = logging.getLogger(__name__)


class Actuation:
    def __init__(self, stop_event: Any = None):
        self._event_queue = mp.Queue()
        self._event_queue_closed = False
        self.event_bus = EventBus(self._event_queue)

        from omnibci.conf import settings
        host = settings.get("ACTUATION.server_ip", "127.0.0.1")
        port = int(settings.get("ACTUATION.server_port", 8001))
        self._heartbeat_interval = float(
            settings.get("ACTUATION.watchdog_timeout_s", 1.0)) / 2.0
        # Age gate for the slow-trickle case (old commands arriving one at a
        # time on a live-but-slow link, where drain-to-latest cannot help).
        # 0 = disabled; requires engine/device clocks roughly in sync to use.
        self._max_command_age = float(
            settings.get("ACTUATION.max_command_age_s", 0.0))
        self.peer_channel = PeerChannel(engine_peer_config(host, port), self.event_bus)

        self.should_stop = False
        self._stop_event = stop_event
        # Pending intents are a latest-value mailbox, not a FIFO backlog.  A
        # command already executing remains serial; among commands waiting for
        # it, only the newest valid intent is retained.
        self.command_queue = Queue(maxsize=1)
        self.actuator = Actuator()
        self._heartbeat_task = None

    def run(self):
        logger.info('---ActuationMain run---')
        asyncio.run(self.serve())

    async def serve(self):
        try:
            await self.start()
        except asyncio.CancelledError:
            # start() may already have started the EventBus or allocated a
            # transport before cancellation reaches it.
            await self.stop()
            raise
        except Exception as exc:
            # Without a working effector the subsystem cannot serve; leave a
            # Fault as the last word (best effort — the channel may not have
            # connected yet) and exit.
            logger.error("Actuation setup failed: %s", exc)
            try:
                self.event_bus.put_event(Event(
                    EventType.ActuationFault,
                    make_actuation_fault("SETUP_FAILED", message=str(exc)),
                ))
                await asyncio.sleep(0.5)   # give the channel a beat to flush
            finally:
                # Even a closed/broken EventBus must not strand the socket.
                await self.stop()
            return

        self._heartbeat_task = asyncio.create_task(self._heartbeat())
        try:
            while not self._stop_requested():
                await self.execute_command()
                await asyncio.sleep(0.01)
        finally:
            try:
                self._heartbeat_task.cancel()
                try:
                    await self._heartbeat_task
                except asyncio.CancelledError:
                    pass
            finally:
                try:
                    self.actuator.teardown()
                finally:
                    # Plugin teardown and heartbeat failures must not skip the
                    # IPC ownership boundary.
                    await self.stop()

    async def start(self):
        logger.info('---ActuationMain start---')
        # Install the route before a newly connected peer can deliver its first
        # command.  The command remains queued until setup completes.
        self.event_bus.register_event_listener(
            event_type=EventType.ActuationCommand, handler=self._enqueue_command)
        await self.event_bus.start()
        await self.peer_channel.start()
        self.actuator.setup()

    async def execute_command(self):
        try:
            command: ValidatedActuationCommand = self.command_queue.get_nowait()
        except Empty:
            return

        payload = command.payload
        target = command.target_id
        seq = command.seq
        # Age gate: an accounting outcome (like superseded), not a device
        # fault. Commands without issued_at (older senders) are never gated.
        issued_at = command.issued_at
        if (self._max_command_age > 0 and issued_at is not None
                and time.time() - issued_at > self._max_command_age):
            logger.warning("Command seq=%s target_id=%s dropped as stale "
                           "(age > %.3fs)", seq, target, self._max_command_age)
            self.event_bus.put_event(Event(
                EventType.ActuationAck,
                make_actuation_ack(target_id=target, status="stale", seq=seq),
            ))
            return
        try:
            # Off the event loop: play() may legitimately block for the whole
            # device action (serial writes, motion completion) and the
            # heartbeat must keep beating meanwhile. asyncio.to_thread keeps
            # commands serial — one execution at a time.
            await asyncio.to_thread(self.actuator.execute, payload)
            reply = Event(EventType.ActuationAck,
                          make_actuation_ack(target_id=target, seq=seq))
        except Exception as exc:
            logger.error("Effector command failed (target_id=%s): %s", target, exc)
            reply = Event(EventType.ActuationFault,
                          make_actuation_fault("COMMAND_FAILED",
                                               target_id=target,
                                               message=str(exc), seq=seq))
        # PeerChannel's outbound subscription forwards these to the engine.
        self.event_bus.put_event(reply)

    def _enqueue_command(self, command: Event):
        if command.type != EventType.ActuationCommand:
            return

        try:
            validated = validate_actuation_command(command.data)
        except InvalidActuationCommand as exc:
            logger.warning("Invalid actuation command dropped: %s", exc)
            self.event_bus.put_event(Event(
                EventType.ActuationFault,
                make_actuation_fault(
                    "INVALID_COMMAND",
                    target_id=exc.target_id,
                    message=str(exc),
                    seq=exc.seq,
                ),
            ))
            return

        # Replacing happens in the EventBus callback, so every superseded
        # command is accounted for immediately instead of waiting for the
        # device command loop to wake up.  Invalid input never displaces a
        # previously validated intent.
        while True:
            try:
                superseded: ValidatedActuationCommand = self.command_queue.get_nowait()
            except Empty:
                break
            self._publish_superseded(superseded)

        # Producer and consumer normally share one event loop, making the slot
        # empty here.  Retrying on Full keeps the latest-value invariant if a
        # test or future adapter calls the listener from another thread.
        while True:
            try:
                self.command_queue.put_nowait(validated)
                break
            except Full:
                try:
                    superseded = self.command_queue.get_nowait()
                except Empty:
                    continue
                self._publish_superseded(superseded)

    def _publish_superseded(self, command: ValidatedActuationCommand) -> None:
        self.event_bus.put_event(Event(
            EventType.ActuationAck,
            make_actuation_ack(
                target_id=command.target_id,
                status="superseded",
                seq=command.seq,
            ),
        ))

    def _stop_requested(self) -> bool:
        if self.should_stop:
            return True
        stop_event = getattr(self, "_stop_event", None)
        return stop_event is not None and stop_event.is_set()

    async def _heartbeat(self):
        """Keep the engine's link watchdog quiet while we are alive."""
        while True:
            await asyncio.sleep(self._heartbeat_interval)
            self.event_bus.put_event(Event(
                EventType.ActuationState, {"heartbeat": True}))

    async def stop(self):
        logger.info('---ActuationMain stop---')
        errors: list[Exception] = []
        cancellation: asyncio.CancelledError | None = None
        try:
            await self.peer_channel.stop()
        except asyncio.CancelledError as exc:
            cancellation = exc
        except Exception as exc:
            errors.append(exc)
        try:
            await self.event_bus.stop()
        except asyncio.CancelledError as exc:
            cancellation = cancellation or exc
        except Exception as exc:
            errors.append(exc)
        try:
            self._close_event_queue()
        except Exception as exc:
            errors.append(exc)

        if cancellation is not None:
            if errors:
                raise cancellation from ExceptionGroup(
                    "Actuation cleanup also failed during cancellation", errors
                )
            raise cancellation
        if errors:
            raise ExceptionGroup("Actuation IPC cleanup failed", errors)

    def _close_event_queue(self) -> None:
        if getattr(self, "_event_queue_closed", False):
            return
        event_queue = getattr(self, "_event_queue", None)
        if event_queue is not None:
            # No consumer remains after EventBus.stop().  Do not wait forever
            # for a feeder to flush discarded events into a full pipe.
            errors = []
            cancel_join = getattr(event_queue, "cancel_join_thread", None)
            if callable(cancel_join):
                try:
                    cancel_join()
                except Exception as exc:
                    errors.append(exc)
            try:
                event_queue.close()
            except Exception as exc:
                errors.append(exc)
            if errors:
                raise ExceptionGroup("Actuation event queue cleanup failed", errors)
        self._event_queue_closed = True
