#!/usr/bin/env python
# -*- coding: utf-8 -*-
# @Author   : lincker <haolin.d.wu@gmail.com>
# @Date     : 2025-05-10 23:30
import asyncio
from omnibci.core.communication.network import NetworkClient

class ClientMessageManagement:
    def __init__(self, event_manager, server_ip='local_host', server_port=8000):
        self.event_manager = event_manager

        self.client = NetworkClient(event_manager= self.event_manager, server_ip=server_ip, server_port=server_port)

    async def start(self) -> None:
        asyncio.create_task(self._start())

    async def _start(self):
        # TODO: retry connection
        await asyncio.gather(self.client.start())
        # await asyncio.gather(self.client.start(), self.event_manager.start() )


    async def stop(self):
        if self.client:
            await self.client.stop()
        # self.event_manager.stop()
