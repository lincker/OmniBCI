#!/usr/bin/env python
# -*- coding: utf-8 -*-
# @Author   : lincker <haolin.d.wu@gmail.com>
# @Date     : 2024-03-25 10:35
import asyncio

from core.communication.network import NetworkServer
from core.event.base import EventType
from conf import settings
from .command_server import CommandServer



class ServerMessageManagement:
    def __init__(self, event_manager):
        self.event_manager = event_manager
        self.server_ip = settings.MESSAGE_SERVER_IP
        self.server_port = settings.MESSAGE_SERVER_PORT
        self.server = NetworkServer(event_manager=event_manager, host=self.server_ip, port=self.server_port)
        self.command_server = CommandServer(event_manager=event_manager)
        self.register_events()

    def start(self) -> None:
        asyncio.create_task(self._start())
        # asyncio.run(self._start(), debug=True)

    async def _start(self):
        # self.events_task = await asyncio.create_task(self.event_manager.start())
        #self.server_task = await asyncio.create_task(self.server.start())

        # refactor to task group
        await asyncio.gather(self.server.start(), self.command_server.start())

        # try:
        #     async with TaskGroup() as group:
        #         # spawn some tasks
        #         group.create_task(job(1, 0.5))
        #         group.create_task(job(2, 1.5))
        #         # sleep for 1 second
        #         await asyncio.sleep(1)
        #         # add an exception-raising task to force the group to terminate
        #         group.create_task(force_terminate_task_group())
        # except TerminateTaskGroup:
        #     pass

    async def stop(self):
        await self.server.stop()

    # async def send_exchange_message(self, message):
    #     await self.server.send(message.encode('utf-8'))
    #     # print('send '+ message +'!')

    def register_events(self):
        self.event_manager.register_event_listener(EventType.FeedbackResult, self.server.send_event)
        self.event_manager.register_event_listener(EventType.StartStimTrail, self.server.send_event)
        self.event_manager.register_event_listener(EventType.StopStimulation, self.server.send_event)

