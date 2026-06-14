#!/usr/bin/env python
# -*- coding: utf-8 -*-
# @Author   : lincker <haolin.d.wu@gmail.com>
# @Date     : 2025-05-10 23:15

import logging
import multiprocessing as mp
import asyncio
from queue import Queue

from omnibci.infrastructure.ipc.event_bus import EventBus, Event, EventType
from omnibci.infrastructure.outbound import make_stimulation_ack
from omnibci.infrastructure.neuroengine.gateway.peer_channel import PeerChannel
from omnibci.infrastructure.stimulation.engine_channel import engine_peer_config
from omnibci.infrastructure.stimulation.stimulator import Stimulator


class Stimulation:
    def __init__(self):
        self.event_bus = EventBus()
        self.event_bus.set_event_queue(queue=mp.Queue())

        from omnibci.conf import settings
        host = settings.get("NEUROENGINE.message_server_ip", "127.0.0.1")
        port = int(settings.get("NEUROENGINE.message_server_port", 8765))
        self.peer_channel = PeerChannel(engine_peer_config(host, port), self.event_bus)

        self.should_stop = False
        self.stim_command_queue = Queue()
        self.result_command_queue = Queue()
        self.stimulator = Stimulator()

    def run(self):
        print('---StimulationMain run---')
        asyncio.run(self.serve())

    async def serve(self):
        await self.start()
        # Render loop runs as a concurrent coroutine on the asyncio main thread.
        # On macOS this satisfies Cocoa's requirement that GLFW event polling
        # (nextEventMatchingMask) is called only from the main thread.
        render_task = asyncio.create_task(self.stimulator.start_async())
        # IPC command loop — exits when should_stop=True or window closes
        while not self.should_stop and not render_task.done():
            await self.execute_command()
            await asyncio.sleep(0.01)
        # Ensure render loop is stopped (sets _stop_event; idempotent if already done)
        self.stimulator.stop()
        await render_task   # wait for teardown + shutdown to finish on GL context
        await self.stop()

    async def start(self):
        print('---StimulationMain start---')
        await self.event_bus.start()
        # PeerChannel.start() is synchronous — schedules asyncio tasks internally
        self.peer_channel.start()
        self._register_event_listener()

    async def execute_command(self):
        if not self.stim_command_queue.empty():
            stim_command = self.stim_command_queue.get()
            if stim_command.type == EventType.StartStimTrial:
                # Present the trial, then send an ack back to the engine.
                # PeerChannel's outbound subscription picks up StimulationAck from
                # the bus and forwards it to the engine automatically.
                # `data` is one trial's opaque paradigm parameter (may be None).
                params = stim_command.data
                target = params if isinstance(params, int) else None
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

    async def _stop_stimulation_system(self):
        self.should_stop = True

    async def stop(self):
        print('---StimulationMain stop---')
        await self.peer_channel.stop()
        self.event_bus.stop()
