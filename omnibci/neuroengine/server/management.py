'''
Author: lincker
Date: 2025-05-27 14:48:34
LastEditors: lincker haolin.d.wu@gmail.com
LastEditTime: 2025-12-25 11:09:12
FilePath: /OmniBCI/omnibci/neuroengine/server/management.py
Description:

Copyright (c) 2025 by lincker, All Rights Reserved.
'''
#!/usr/bin/env python
# -*- coding: utf-8 -*-
# @Author   : lincker <haolin.d.wu@gmail.com>
# @Date     : 2024-03-25 10:35
import asyncio

from omnibci.conf import settings
from omnibci.core.events.types import EventType

from omnibci.core.network.socket import NetworkServer

from .command_server import CommandServer


class ServerMessageManagement:
    def __init__(self, event_bus):
        self.event_bus = event_bus
        self.server_ip = settings.get("NEUROENGINE.message_server_ip")
        self.server_port = settings.get("NEUROENGINE.message_server_port")
        self.server = NetworkServer(event_bus=event_bus, host=self.server_ip, port=self.server_port)
        self.command_server = CommandServer(event_bus=event_bus)
        self.register_events()

    def start(self) -> None:
        asyncio.create_task(self._start())
        # asyncio.run(self._start(), debug=True)

    async def _start(self):
        # self.events_task = await asyncio.create_task(self.event_bus.start())
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

    # async def send_exchange_message(self, server):
    #     await self.server.send(server.encode('utf-8'))
    #     # print('send '+ server +'!')

    def register_events(self):
        self.event_bus.register_event_listener(EventType.RecognizedResult, self.server.send_event)
        self.event_bus.register_event_listener(EventType.StartStimTrial, self.server.send_event)
        self.event_bus.register_event_listener(EventType.StopStimulation, self.server.send_event)

