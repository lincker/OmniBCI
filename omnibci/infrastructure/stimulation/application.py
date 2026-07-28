#!/usr/bin/env python
# -*- coding: utf-8 -*-
# @Author   : lincker <haolin.d.wu@gmail.com>
# @Date     : 2025-05-10 23:15

import logging
import multiprocessing as mp
import asyncio
from queue import Queue

from omnibci.infrastructure.ipc.event_bus import EventBus
from omnibci.shared.events import Event, EventType
from omnibci.infrastructure.outbound import make_stimulation_ack
from omnibci.infrastructure.neuroengine.gateway.peer_channel import PeerChannel
from omnibci.infrastructure.process_lifecycle import StartupReporter
from omnibci.infrastructure.stimulation.engine_channel import engine_peer_config
from omnibci.infrastructure.stimulation.stimulator import Stimulator

_MANAGED_CONNECT_TIMEOUT_S = 5.0


class Stimulation:
    def __init__(
        self,
        stop_event=None,
        startup_reporter: StartupReporter | None = None,
    ):
        self._event_queue = mp.Queue()
        self._event_queue_closed = False
        self.event_bus = EventBus(self._event_queue)

        from omnibci.conf import settings
        host = settings.get("NEUROENGINE.message_server_ip", "127.0.0.1")
        port = int(settings.get("NEUROENGINE.message_server_port", 8765))
        self.peer_channel = PeerChannel(engine_peer_config(host, port), self.event_bus)

        self.should_stop = False
        self._stop_event = stop_event
        self._startup_reporter = startup_reporter
        self.stim_command_queue = Queue()
        self.result_command_queue = Queue()
        self.stimulator = Stimulator()
        self._render_task = None   # GLFW 渲染窗口任务;关窗后为 done,但进程/连接保持

    def run(self):
        print('---StimulationMain run---')
        asyncio.run(self.serve())

    async def serve(self):
        startup_complete = False
        try:
            try:
                # A cooperative stop can win the race before the spawned child
                # gets CPU time.  Do not open IPC/GL merely to tear it down.
                if self._stop_requested():
                    self._report_startup_failed(
                        "preflight",
                        RuntimeError("stop requested before Stimulation startup"),
                    )
                    return
                await self.start()
                if self._stop_requested():
                    self._report_startup_failed(
                        "initialization",
                        RuntimeError("stop requested during Stimulation startup"),
                    )
                    return
                # Render loop runs as a concurrent coroutine on the asyncio main
                # thread. On macOS this keeps Cocoa event polling on that thread.
                self._render_task = asyncio.create_task(
                    self.stimulator.start_async()
                )
                # Let start_async() install its RenderLoop before checking the
                # stop flag again. Without this hand-off an immediate stop can
                # make stimulator.stop() a no-op and strand the renderer.
                await asyncio.sleep(0)

                if self._render_task.done() and not self._render_task.cancelled():
                    render_error = self._render_task.exception()
                    if render_error is not None:
                        raise render_error
                    if getattr(self, "_startup_reporter", None) is not None:
                        raise RuntimeError(
                            "stimulation window closed during startup"
                        )

                await self._await_managed_readiness()
                reporter = getattr(self, "_startup_reporter", None)
                if reporter is not None:
                    reporter.ready()
                startup_complete = True

                # IPC command loop — the engine connection lives as long as this
                # loop runs. It is deliberately decoupled from a normally-closed
                # render window.
                while not self._stop_requested():
                    # A normal render-task return means "window closed" and the
                    # IPC process stays available to report explicit errors. An
                    # exception is a subsystem failure and must be observed.
                    if (
                        self._render_task.done()
                        and not self._render_task.cancelled()
                    ):
                        render_error = self._render_task.exception()
                        if render_error is not None:
                            raise render_error
                    await self.execute_command()
                    await asyncio.sleep(0.01)
            except BaseException as exc:
                if not startup_complete:
                    self._report_startup_failed("initialization", exc)
                raise
        finally:
            # Ensure GL/paradigm teardown and IPC teardown happen on every exit
            # path, including render setup failures and task cancellation.
            try:
                try:
                    self.stimulator.stop()
                except BaseException:
                    # A failed stop hook may have left the render coroutine
                    # blocked forever.  Cancel it, but continue through the
                    # outer finally so sockets and the EventBus still close.
                    if (
                        self._render_task is not None
                        and not self._render_task.done()
                    ):
                        self._render_task.cancel()
                    raise
            finally:
                try:
                    if self._render_task is not None:
                        await self._render_task
                except asyncio.CancelledError:
                    pass
                finally:
                    # Renderer teardown failures must not skip IPC cleanup.
                    await self.stop()

    async def start(self):
        print('---StimulationMain start---')
        # Install inbound routes before the reconnecting transport can receive
        # its first frame.
        self._register_event_listener()
        await self.event_bus.start()
        await self.peer_channel.start()

    async def _await_managed_readiness(self) -> None:
        """Wait for the engine link while continuing to supervise GL startup."""
        if getattr(self, "_startup_reporter", None) is None:
            return
        connection_task = asyncio.create_task(
            self.peer_channel.wait_until_connected(_MANAGED_CONNECT_TIMEOUT_S),
            name="stimulation-startup-engine-link",
        )
        render_task = self._render_task
        if render_task is None:
            connection_task.cancel()
            await asyncio.gather(connection_task, return_exceptions=True)
            raise RuntimeError("stimulation renderer was not started")
        try:
            done, _ = await asyncio.wait(
                {connection_task, render_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if render_task in done:
                if not connection_task.done():
                    connection_task.cancel()
                await asyncio.gather(connection_task, return_exceptions=True)
                if render_task.cancelled():
                    raise RuntimeError(
                        "stimulation renderer was cancelled during startup"
                    )
                render_error = render_task.exception()
                if render_error is not None:
                    raise render_error
                raise RuntimeError("stimulation window closed during startup")
            await connection_task
        except BaseException:
            if not connection_task.done():
                connection_task.cancel()
            await asyncio.gather(connection_task, return_exceptions=True)
            raise

    def _report_startup_failed(self, stage: str, error: BaseException) -> None:
        reporter = getattr(self, "_startup_reporter", None)
        if reporter is not None:
            reporter.failed(stage, error)

    async def execute_command(self):
        # Serialize trials: only pull the next StartStimTrial once the paradigm
        # has finished presenting the current one.  Without this gate the whole
        # queue is drained within a few loop iterations (~10 ms each) while a
        # single trial's cue→stim sequence runs for seconds, so N queued trials
        # collapse into one.  Paradigms that don't report busy-state fall back to
        # the old immediate behaviour (getattr default False).
        presenting = getattr(self.stimulator, "is_presenting", False)
        if not presenting and not self.stim_command_queue.empty():
            stim_command = self.stim_command_queue.get()
            if stim_command.type == EventType.StartStimTrial:
                # Present the trial, then send an ack back to the engine.
                # PeerChannel's outbound subscription picks up StimulationAck from
                # the bus and forwards it to the engine automatically.
                # `data` is one trial's opaque paradigm parameter (may be None).
                params = stim_command.data
                target = params if isinstance(params, int) else None
                if self._render_task is None or self._render_task.done():
                    # 渲染窗口已关闭:连接仍保持,但无法显示刺激 → 明确回执错误,
                    # 避免在无窗口时静默卡住(范式状态机不会再推进)。
                    ack = make_stimulation_ack(
                        "StartStimTrial", status="error", target_index=target,
                        message="stimulation window closed",
                    )
                else:
                    try:
                        self.stimulator.present_trial(params)
                        ack = make_stimulation_ack(
                            "StartStimTrial", status="ok", target_index=target
                        )
                    except Exception as exc:
                        ack = make_stimulation_ack(
                            "StartStimTrial", status="error",
                            target_index=target, message=str(exc),
                        )
                self.event_bus.put_event(Event(EventType.StimulationAck, ack))

        if not self.result_command_queue.empty():
            sys_command = self.result_command_queue.get()
            if sys_command.type == EventType.TrialCompleted:
                # The TrialCompleted payload arrives over the engine→stimulation
                # socket as a plain dict (body_to_event does not rebuild domain
                # VOs), so extract target_id from the dict shape.  Wrapped in
                # try/except: a malformed/out-of-range result must NOT crash the
                # render process and close the stimulation window.
                from omnibci.domain.classification import Classification
                data = sys_command.data
                try:
                    if isinstance(data, Classification):
                        result = data.target_id
                    elif isinstance(data, dict):
                        result = int(data.get("target_id"))
                    else:
                        result = int(data)
                    self.stimulator.present_result(result)
                except Exception as exc:
                    logging.warning("present_result skipped (bad result %r): %s", data, exc)

    def _register_event_listener(self):
        self.event_bus.register_event_listener(event_type=EventType.StartStimTrial, handler=self._put_stim_command_queue)
        self.event_bus.register_event_listener(event_type=EventType.TrialCompleted, handler=self._put_sys_command_queue)
        self.event_bus.register_event_listener(event_type=EventType.StopStimulation, handler=self._stop_stimulation_system)

    def _put_stim_command_queue(self, command: Event):
        # Generic expander: knows only the base layer (how many trials) and
        # carries each trial's paradigm-specific parameter as an opaque blob.
        # No event_id / target knowledge lives here.
        if command.type != EventType.StartStimTrial:
            return
        spec = command.data or {}
        n_trials = int(spec.get("n_trials", 1))
        params = spec.get("trial_params")
        if params is None:
            params = [None] * n_trials      # default: empty per-trial param → paradigm default
        for per_trial in params:
            # One queue item == one trial, carrying that trial's opaque params.
            self.stim_command_queue.put(Event(EventType.StartStimTrial, per_trial))

    def _put_sys_command_queue(self, command: Event):
        if command.type == EventType.TrialCompleted:
            self.result_command_queue.put(command)

    async def _stop_stimulation_system(self, _event=None):
        self.should_stop = True

    def _stop_requested(self) -> bool:
        if self.should_stop:
            return True
        stop_event = getattr(self, "_stop_event", None)
        return stop_event is not None and stop_event.is_set()

    async def stop(self):
        print('---StimulationMain stop---')
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
                    "Stimulation cleanup also failed during cancellation", errors
                )
            raise cancellation
        if errors:
            raise ExceptionGroup("Stimulation IPC cleanup failed", errors)

    def _close_event_queue(self) -> None:
        if getattr(self, "_event_queue_closed", False):
            return
        event_queue = getattr(self, "_event_queue", None)
        if event_queue is not None:
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
                raise ExceptionGroup("Stimulation event queue cleanup failed", errors)
        self._event_queue_closed = True
