#!/usr/bin/env python
# -*- coding: utf-8 -*-
# @Author   : lincker <haolin.d.wu@gmail.com>
# @Date     : 2025-05-10 23:30
import asyncio
import logging
from typing import Optional

from omnibci.infrastructure.ipc.network import NetworkClient

logger = logging.getLogger(__name__)


class ClientMessageManagement:
    def __init__(self, event_bus, server_ip='127.0.0.1', server_port=8000):
        self.event_bus = event_bus

        self.client = NetworkClient(event_bus= self.event_bus, server_ip=server_ip, server_port=server_port)
        # Strong reference to the background client task — without it the loop
        # only keeps a weak reference and the task may be garbage-collected
        # mid-run, silently dropping the stimulation connection.
        self._client_task: Optional[asyncio.Task] = None

    async def start(self) -> None:
        self._client_task = asyncio.create_task(self._start())

    async def send_event(self, event) -> bool:
        """
        Send an event back to the engine if connected.  Returns True on success,
        False if no connection (skipped) or on send error — never raises, so an
        ack failure can't crash the stimulation loop.
        """
        if self.client is None or self.client.status != "CONNECTED":
            logger.debug("send_event skipped: stimulation client not connected")
            return False
        try:
            await self.client.send_event(event)
            return True
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("send_event failed: %s", exc)
            return False

    async def _start(self):
        # TODO: retry connection
        await asyncio.gather(self.client.start())
        # await asyncio.gather(self.client.start(), self.event_bus.start() )


    async def stop(self):
        if self.client:
            await self.client.stop()
        if self._client_task is not None and not self._client_task.done():
            self._client_task.cancel()
            try:
                await self._client_task
            except asyncio.CancelledError:
                pass
        self._client_task = None
        # self.event_bus.stop()
