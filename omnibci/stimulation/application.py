#!/usr/bin/env python
# -*- coding: utf-8 -*-
# @Author   : lincker <haolin.d.wu@gmail.com>
# @Date     : 2025-05-10 23:15

import multiprocessing as mp
import asyncio

from omnibci.core.event.event_manager import EventManager, Event, EventType
from .message import ClientMessageManagement
from omnibci.stimulation.stimulator import Stimulator


class StimulationMain:
    def __init__(self, config=None):
        self.config = config
        self.event_manager = EventManager()
        self.event_manager.set_event_queue(queue=mp.Queue())
        self.client_message_management = ClientMessageManagement(self.event_manager, config=config)
        self.should_stop = False
        self.command_queue = mp.Queue(20)
        self.stimulator = Stimulator()
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
        # TODO: refactor, client message management will case deadlock
        await self.client_message_management.start()
        await self.event_manager.start()
        self.event_manager.register_event_listener(event_type=EventType.StartStimTrail, handler=self.stimulator.run_command)
        self.event_manager.register_event_listener(event_type=EventType.FeedbackResult, handler=self.stimulator.run_command)
        self.event_manager.register_event_listener(event_type=EventType.StopStimulation, handler=self.change_stop_status)
        await asyncio.sleep(2)


        #
        # self.stimulator.start()

    async def main_loop(self):
        print('---StimulationMain main_loop---')
        # counter = 1
        while not self.should_stop:
            # if not self.command_queue.empty():
            #     self.execute(self.command_queue.get())
            await asyncio.sleep(1)
            # counter += 1
            # if counter > 50:
            #     self.should_stop = True

    async def change_stop_status(self, command: Event):
        self.should_stop = True

        # if command.type == EventType.StopStimulation:
        #     self.stimulator.stop()
        # else:
        #     self.stimulator.run_command(command)



    async def shutdown(self):
        print('---StimulationMain shutdown---')

        self.should_stop = True
        await self.stop()



    async def stop(self):
        # self.client_message_management.terminate()
        self.stimulator.stop()
        await self.client_message_management.stop()
        self.event_manager.stop()

