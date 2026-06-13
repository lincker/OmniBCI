#!/usr/bin/env python
# -*- coding: utf-8 -*-
# @Author   : lincker <haolin.d.wu@gmail.com>
# @Date     : 2025-05-10 23:15

import multiprocessing as mp
import asyncio
from queue import Queue

from omnibci.infrastructure.ipc.event_bus import EventBus, Event, EventType
from omnibci.infrastructure.outbound import make_stimulation_ack
from .client import ClientMessageManagement
from omnibci.infrastructure.stimulation.stimulator import Stimulator


class Stimulation:
    def __init__(self):
        self.event_bus = EventBus()
        self.event_bus.set_event_queue(queue=mp.Queue())
        self.client_message_management = ClientMessageManagement(self.event_bus)
        self.should_stop = False
        self.stim_command_queue = Queue()
        self.result_command_queue = Queue()
        self.stimulator = Stimulator()
        # stimulator.start_async() is called from serve() on the asyncio main thread

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
        await self.client_message_management.start()
        self._register_event_listener()

    async def execute_command(self):
        if not self.stim_command_queue.empty():
            stim_command = self.stim_command_queue.get()
            if stim_command.type == EventType.StartStimTrial:
                # Present the trial, then send an ack back to the engine so the
                # operator gets a receipt of whether it actually ran.
                try:
                    self.stimulator.present_trial(event_id=stim_command.data)
                    ack = make_stimulation_ack(
                        "StartStimTrial", status="ok", target_index=stim_command.data
                    )
                except Exception as exc:
                    ack = make_stimulation_ack(
                        "StartStimTrial", status="error",
                        target_index=stim_command.data, message=str(exc),
                    )
                await self.client_message_management.send_event(
                    Event(EventType.StimulationAck, ack)
                )
        if not self.result_command_queue.empty():
            sys_command = self.result_command_queue.get()
            if sys_command.type == EventType.TrialCompleted:
                # payload is a Classification VO; extract target_id for display
                from omnibci.domain.classification import Classification
                data = sys_command.data
                result = data.target_id if isinstance(data, Classification) else int(data)
                self.stimulator.present_result(result)


    def _register_event_listener(self):
        self.event_bus.register_event_listener(event_type=EventType.StartStimTrial, handler=self._put_stim_command_queue)
        self.event_bus.register_event_listener(event_type=EventType.TrialCompleted, handler=self._put_sys_command_queue)
        self.event_bus.register_event_listener(event_type=EventType.StopStimulation, handler=self._stop_stimulation_system)

    def _put_stim_command_queue(self, command: Event):
        if command.type == EventType.StartStimTrial:
            if command.data > 0:
                n_trials = command.data
                for i in range(n_trials):
                    self.stim_command_queue.put(command)
            # The ack is sent from execute_command(), after the renderer actually
            # presents the trial (success → status="ok", failure → "error").

    def _put_sys_command_queue(self, command: Event):
        if command.type == EventType.TrialCompleted:
            self.result_command_queue.put(command)

    async def _stop_stimulation_system(self):
        self.should_stop = True

    async def stop(self):
        print('---StimulationMain stop---')
        # stimulator already stopped in serve() before this is called
        await self.client_message_management.stop()
        self.event_bus.stop()

