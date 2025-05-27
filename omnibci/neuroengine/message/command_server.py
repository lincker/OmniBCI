#!/usr/bin/env python
# -*- coding: utf-8 -*-
# @Author   : lincker <haolin.d.wu@gmail.com>
# @Date     : 2025-03-17 09:23
from aiohttp import web
from core.event.base import Event, EventType
from conf import settings


class CommandServer:
    def __init__(self, event_manager):
        self.app = web.Application()
        self.app.add_routes([web.post('/command', self.command)])
        self.runner = web.AppRunner(self.app)
        self.event_manager = event_manager

    async def command(self, request):
        command = await request.json()
        print(command)
        command_type = command.get('type')
        command_data = command.get('data')
        if command_type == 'ConnectDataProvider':
            event = Event(type=EventType.ConnectDataProvider, data=command_data)
        elif command_type == 'DisconnectDataProvider':
            event = Event(type=EventType.DisconnectDataProvider, data=command_data)
        elif command_type == 'StartReceive':
            event = Event(type=EventType.StartReceive, data=command_data)
        elif command_type == 'StopReceive':
            event = Event(type=EventType.StopReceive, data=command_data)
        elif command_type == 'StartStimTrail':
            event = Event(type=EventType.StartStimTrail, data=command_data)
        elif command_type == 'StopAll':
            event = Event(type=EventType.StopProcessSystem, data=command_data)
        else:
            return web.json_response({'status': 'error', 'message': 'Invalid command type'})
        # check command
        self.event_manager.put_event(event)
        return web.json_response({'command':command_type, 'status': 'ok'})

    async def start(self):
        await self.runner.setup()
        server_ip = settings.COMMAND_SERVER_IP
        server_port = settings.COMMAND_SERVER_PORT
        site = web.TCPSite(self.runner, server_ip, server_port)
        await site.start()

    async def stop(self):
        await self.runner.cleanup()

