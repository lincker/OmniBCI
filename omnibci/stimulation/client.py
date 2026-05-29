#!/usr/bin/env python
# -*- coding: utf-8 -*-
# @Author   : lincker <haolin.d.wu@gmail.com>
# @Date     : 2025-05-10 23:30
import asyncio
from omnibci.core.network.socket import NetworkClient

class ClientMessageManagement:
    def __init__(self, event_bus, server_ip='127.0.0.1', server_port=8000):
        self.event_bus = event_bus

        self.client = NetworkClient(event_bus= self.event_bus, server_ip=server_ip, server_port=server_port)

    async def start(self) -> None:
        asyncio.create_task(self._start())

    async def _start(self):
        # TODO: retry connection
        await asyncio.gather(self.client.start())
        # await asyncio.gather(self.client.start(), self.event_bus.start() )


    async def stop(self):
        if self.client:
            await self.client.stop()
        # self.event_bus.stop()
