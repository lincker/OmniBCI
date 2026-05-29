'''
Author: lincker
Date: 2025-05-27 14:48:34
LastEditors: lincker haolin.d.wu@gmail.com
LastEditTime: 2025-12-25 11:09:53
FilePath: /OmniBCI/omnibci/neuroengine/server/command_server.py
Description:

Copyright (c) 2025 by lincker, All Rights Reserved.
'''
#!/usr/bin/env python
# -*- coding: utf-8 -*-
# @Author   : lincker <haolin.d.wu@gmail.com>
# @Date     : 2025-03-17 09:23
from aiohttp import web
from omnibci.conf import settings

from omnibci.core.events.types import Event, EventType


class CommandServer:
    def __init__(self, event_bus):
        self.app = web.Application()
        self.app.add_routes([web.post('/command', self.command)])
        self.runner = web.AppRunner(self.app)
        self.event_bus = event_bus

    async def command(self, request):
        command = await request.json()
        print(command)
        command_type = command.get('type')
        command_data = command.get('data')
        if command_type == 'ConnectDataProvider':
            event = Event(event_type=EventType.ConnectDataProvider, data=command_data)
        elif command_type == 'DisconnectDataProvider':
            event = Event(event_type=EventType.DisconnectDataProvider, data=command_data)
        elif command_type == 'StartReceive':
            event = Event(event_type=EventType.StartStream, data=command_data)
        elif command_type == 'StopReceive':
            event = Event(event_type=EventType.StopStream, data=command_data)
        elif command_type == 'StartStimTrial':
            event = Event(event_type=EventType.StartStimTrial, data=command_data)
        elif command_type == 'StopAll':
            event = Event(event_type=EventType.StopNeuroEngineSignalProcessor, data=command_data)
        else:
            return web.json_response({'status': 'error', 'server': 'Invalid command type'})
        # check command
        self.event_bus.put_event(event)
        return web.json_response({'command':command_type, 'status': 'ok'})

    async def start(self):
        await self.runner.setup()
        server_ip = settings.get("NEUROENGINE.COMMAND_SERVER_IP")
        server_port = settings.get("NEUROENGINE.COMMAND_SERVER_PORT")
        site = web.TCPSite(self.runner, server_ip, server_port)
        await site.start()

    async def stop(self):
        await self.runner.cleanup()

