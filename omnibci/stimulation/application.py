#!/usr/bin/env python
# -*- coding: utf-8 -*-
# @Author   : lincker <haolin.d.wu@gmail.com>
# @Date     : 2025-05-10 23:15

import multiprocessing as mp
import asyncio
from queue import Queue

from omnibci.core.event.event_manager import EventManager, Event, EventType
from .client import ClientMessageManagement
from omnibci.stimulation.stimulator import StimulatorProcess


class Stimulation:
    def __init__(self):
        self.event_manager = EventManager()
        self.event_manager.set_event_queue(queue=mp.Queue())
        self.client_message_management = ClientMessageManagement(self.event_manager)
        self.should_stop = False
        self.stim_command_queue = Queue(20)
        self.sys_command_queue = Queue(20)
        self.stimulator = StimulatorProcess()
        self.stimulator.start()


    def run(self):
        print('---StimulationMain run---')
        asyncio.run(self.serve(), debug=True)

    async def serve(self):
        await self.startup()
        await self.main_loop()
        await self.shutdown()

    async def startup(self):
        print('---StimulationMain startup---')
        # TODO: refactor, client server management will case deadlock
        await self.client_message_management.start()
        await self.event_manager.start()
        self._register_event()
        await asyncio.sleep(2)

    async def main_loop(self):
        print('---StimulationMain main_loop---')
        # counter = 1
        while not self.should_stop:
            self.execute_command_once()
            await asyncio.sleep(0.1)


    def execute_command_once(self):
        if not self.stim_command_queue.empty():
            stim_command = self.stim_command_queue.get()
            if stim_command.type == EventType.StartStimTrial:
                self.stimulator.play()
            # execute stim command
        if not self.sys_command_queue.empty():
            sys_command = self.sys_command_queue.get()
            if sys_command.type == EventType.FeedbackResult:
                result = sys_command.data
                self.stimulator.display_feedback(result)
            elif sys_command.type == EventType.StopStimulation:
                self.should_stop = True

    def _register_event(self):
        self.event_manager.register_event_listener(event_type=EventType.StartStimTrial, handler=self._put_stim_command_queue)
        self.event_manager.register_event_listener(event_type=EventType.FeedbackResult, handler=self._put_sys_command_queue)
        self.event_manager.register_event_listener(event_type=EventType.StopStimulation, handler=self._put_sys_command_queue)

    def _put_stim_command_queue(self, command: Event):
        if command.type == EventType.StartStimTrial:
            if command.data > 0:
                n_trials = command.data
                for i in range(n_trials):
                    self.stim_command_queue.put(command)

    def _put_sys_command_queue(self, command: Event):
        if command.type == EventType.FeedbackResult:
            self.sys_command_queue.put(command)
        elif command.type == EventType.StopStimulation:
            self.sys_command_queue.put(command)

    async def shutdown(self):
        print('---StimulationMain shutdown---')

        self.should_stop = True
        await self.stop()



    async def stop(self):
        # self.client_message_management.terminate()
        self.stimulator.stop()
        await self.client_message_management.stop()
        self.event_manager.stop()

