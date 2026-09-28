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
from copy import deepcopy
import multiprocessing as mp
import time
from queue import Empty, Full, Queue
from typing import Any

from omnibci.application.events import CommandFailureKind, CommandRequest, CommandResult
from omnibci.shared.events import Event, EventType
from omnibci.infrastructure.ipc.event_bus import EventBus
from omnibci.infrastructure.ipc.peer_identity import PeerIdentity
from omnibci.infrastructure.outbound import make_actuation_ack, make_actuation_fault
from omnibci.infrastructure.neuroengine.gateway.peer_channel import PeerChannel
from omnibci.infrastructure.actuation.engine_channel import engine_peer_config
from omnibci.infrastructure.actuation.actuator import Actuator
from omnibci.infrastructure.actuation.contracts import (
    InvalidActuationCommand,
    ValidatedActuationCommand,
    validate_actuation_command,
)
from omnibci.infrastructure.process_lifecycle import StartupReporter

logger = logging.getLogger(__name__)

_MANAGED_CONNECT_TIMEOUT_S = 5.0
_EXTERNAL_CONNECT_TIMEOUT_S = 10.0


class Actuation:
    def __init__(
        self,
        stop_event: Any = None,
        startup_reporter: StartupReporter | None = None,
        *,
        settings_snapshot: dict | None = None,
    ):
        self._event_queue = mp.Queue()
        self._event_queue_closed = False
        self.event_bus = EventBus(self._event_queue)

        from omnibci.conf import settings
        self._runtime_snapshot = (deepcopy(settings_snapshot) if settings_snapshot is not None
                                  else settings.snapshot())
        source = self._runtime_snapshot
        host = source.get("ACTUATION.server_ip", "127.0.0.1")
        port = int(source.get("ACTUATION.server_port", 8001))
        self._heartbeat_interval = float(
            source.get("ACTUATION.watchdog_timeout_s", 1.0)) / 2.0
        # Age gate for the slow-trickle case (old commands arriving one at a
        # time on a live-but-slow link, where drain-to-latest cannot help).
        # 0 = disabled; requires engine/device clocks roughly in sync to use.
        self._max_command_age = float(
            source.get("ACTUATION.max_command_age_s", 0.0))
        self._external_node = source.get("CONFIG.node_role") == "actuation"
        self._reconnect_requested = False
        identity = None
        if self._external_node:
            identity = PeerIdentity("actuation", source["CONFIG.revision"], source["CONFIG.generation"])
        self.peer_channel = PeerChannel(engine_peer_config(
            host, port, identity=identity, reconnect=not self._external_node,
            on_disconnect=self._connection_lost if self._external_node else None,
        ), self.event_bus)

        self.should_stop = False
        self._stop_event = stop_event
        self._startup_reporter = startup_reporter
        # Pending intents are a latest-value mailbox, not a FIFO backlog.  A
        # command already executing remains serial; among commands waiting for
        # it, only the newest valid intent is retained.
        self.command_queue = Queue(maxsize=1)
        self.actuator = Actuator(settings_snapshot=self._runtime_snapshot)
        self._heartbeat_task = None
        self._paused = False
        self._execution_task = None
        self._settlement_tasks = set()
        self._settlement_lock = asyncio.Lock()

    def run(self):
        logger.info('---ActuationMain run---')
        asyncio.run(self.serve())
        return self._reconnect_requested

    def _connection_lost(self) -> None:
        self._reconnect_requested = True
        self._paused = True
        self.should_stop = True

    async def serve(self):
        startup_complete = False
        try:
            if self._stop_requested():
                self._report_startup_failed(
                    "preflight",
                    RuntimeError("stop requested before Actuation startup"),
                )
                return
            await self.start()
            self._heartbeat_task = asyncio.create_task(self._heartbeat())
            await self._await_managed_connection()
            if getattr(self, "_reconnect_requested", False):
                return
            reporter = getattr(self, "_startup_reporter", None)
            if reporter is not None:
                reporter.ready()
            startup_complete = True

            while not self._stop_requested():
                await self.execute_command()
                await asyncio.sleep(0.01)
        except asyncio.CancelledError:
            if not startup_complete:
                self._report_startup_failed(
                    "initialization",
                    asyncio.CancelledError("Actuation startup cancelled"),
                )
            raise
        except Exception as exc:
            if not startup_complete:
                # Without a working effector/link the subsystem cannot serve.
                # Report the original cause synchronously to the parent, then
                # leave a wire Fault as a best-effort last word.
                self._report_startup_failed("initialization", exc)
                logger.error("Actuation setup failed: %s", exc)
                try:
                    self.event_bus.put_event(Event(
                        EventType.ActuationFault,
                        make_actuation_fault("SETUP_FAILED", message=str(exc)),
                    ))
                    await asyncio.sleep(0.5)   # give the channel a beat to flush
                except Exception:
                    logger.exception("Could not publish Actuation setup fault")
            raise
        finally:
            try:
                heartbeat = getattr(self, "_heartbeat_task", None)
                if heartbeat is not None:
                    heartbeat.cancel()
                    try:
                        await heartbeat
                    except asyncio.CancelledError:
                        pass
            finally:
                try:
                    pending = tuple(getattr(self, "_settlement_tasks", ()))
                    if pending:
                        await asyncio.gather(*pending, return_exceptions=True)
                    if getattr(self, "_reconnect_requested", False):
                        # A completed play() does not confirm settled outputs.
                        # Refetch only after the driver verifies a safe state.
                        quiescence = asyncio.create_task(
                            asyncio.to_thread(self.actuator.quiesce),
                        )
                        try:
                            await asyncio.shield(quiescence)
                        except asyncio.CancelledError:
                            # Cancellation cannot stop physical work in a thread.
                            await quiescence
                            raise
                finally:
                    try:
                        self.actuator.teardown()
                    finally:
                        # Plugin teardown and heartbeat failures must not skip
                        # the IPC ownership boundary.
                        await self.stop()

    async def start(self):
        logger.info('---ActuationMain start---')
        # Install the route before a newly connected peer can deliver its first
        # command.  The command remains queued until setup completes.
        self.event_bus.register_event_listener(
            event_type=EventType.ActuationCommand, handler=self._enqueue_command)
        self.event_bus.register_event_listener(
            event_type=EventType.SettleActuation, handler=self._request_settlement)
        await self.event_bus.start()
        if getattr(self, "_external_node", False):
            # The engine must not recognize this node until its device is ready.
            self.actuator.setup()
            await self.peer_channel.start()
        else:
            await self.peer_channel.start()
            self.actuator.setup()

    async def _await_managed_connection(self) -> None:
        """Wait for the verified connection before commands can run."""
        if (getattr(self, "_startup_reporter", None) is None
                and not getattr(self, "_external_node", False)):
            return
        try:
            timeout = (_EXTERNAL_CONNECT_TIMEOUT_S if getattr(self, "_external_node", False)
                       else _MANAGED_CONNECT_TIMEOUT_S)
            await self.peer_channel.wait_until_connected(timeout)
        except (RuntimeError, TimeoutError):
            if not getattr(self, "_reconnect_requested", False):
                raise

    def _report_startup_failed(self, stage: str, error: BaseException) -> None:
        reporter = getattr(self, "_startup_reporter", None)
        if reporter is not None:
            reporter.failed(stage, error)

    async def execute_command(self):
        if getattr(self, "_external_node", False) and not self.peer_channel.is_connected:
            self._connection_lost()
            return
        if getattr(self, "_paused", False):
            return
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
            self._execution_task = asyncio.create_task(
                asyncio.to_thread(self.actuator.execute, payload),
            )
            try:
                await asyncio.shield(self._execution_task)
            except asyncio.CancelledError:
                # Cancelling an await cannot cancel physical work in a thread.
                # Join before teardown can touch the same device.
                await self._execution_task
                raise
            reply = Event(EventType.ActuationAck,
                          make_actuation_ack(target_id=target, seq=seq))
        except Exception as exc:
            logger.error("Effector command failed (target_id=%s): %s", target, exc)
            reply = Event(EventType.ActuationFault,
                          make_actuation_fault("COMMAND_FAILED",
                                               target_id=target,
                                               message=str(exc), seq=seq))
        finally:
            self._execution_task = None
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

        if getattr(self, "_paused", False):
            self._publish_cancelled(validated)
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

    def _publish_cancelled(self, command: ValidatedActuationCommand) -> None:
        self.event_bus.put_event(Event(
            EventType.ActuationAck,
            make_actuation_ack(
                target_id=command.target_id, status="cancelled", seq=command.seq,
                message="Actuation paused for configuration; command was not executed",
            ),
        ))

    def _request_settlement(self, event: Event) -> None:
        """Leave the EventBus free to forward heartbeats while a driver finishes."""
        if CommandRequest.from_value(event.data) is None:
            return
        self._paused = True
        task = asyncio.create_task(self._settle_actuation(event))
        self._settlement_tasks.add(task)
        task.add_done_callback(self._settlement_tasks.discard)

    async def _settle_actuation(self, event: Event) -> None:
        """Cancel queued intents, join active execution, then verify device quiescence."""
        request = CommandRequest.from_value(event.data)
        if request is None:
            return
        self._paused = True
        cancelled = 0
        while True:
            try:
                pending = self.command_queue.get_nowait()
            except Empty:
                break
            self._publish_cancelled(pending)
            cancelled += 1
        try:
            async with self._settlement_lock:
                execution = getattr(self, "_execution_task", None)
                if execution is not None:
                    await asyncio.shield(execution)
                # A driver must explicitly confirm safe device state; play()
                # returning or process exit is insufficient evidence.
                await asyncio.to_thread(self.actuator.quiesce)
            snapshot = getattr(self, "_runtime_snapshot", {})
            result = CommandResult(
                request.command_id, EventType.SettleActuation.name, ok=True,
                data={
                    "paused": True, "pending_cancelled": cancelled,
                    "execution_complete": True, "outputs_settled": True,
                    "generation": snapshot.get("CONFIG.generation"),
                    "config_revision": snapshot.get("CONFIG.revision"),
                },
            )
        except Exception as exc:
            result = CommandResult(
                request.command_id, EventType.SettleActuation.name, ok=False,
                error_code="ACTUATION_SETTLE_FAILED", message=str(exc),
                failure_kind=CommandFailureKind.EXECUTION_FAILED,
            )
        self.event_bus.put_event(Event(EventType.CommandResult, result))

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
