#!/usr/bin/env python
# -*- coding: utf-8 -*-
# @Author   : lincker <haolin.d.wu@gmail.com>
# @Date     : 2023-07-24 09:52
import json
import logging
import multiprocessing as mp
import asyncio
from omnibci.core.event.base import Event
from omnibci.core.event.event_manager import EventManager
from .frame import Frame


class NetworkServer:
    def __init__(self, event_manager, host, port):
        self.reader = None
        self.writer = None
        self.event_manager = event_manager
        self.network_server = None
        self.host = host
        self.port = port
        self.status = 'INIT'
        self.server_task = None

    async def start(self):
        await self._start_server()

    async def _start_server(self):
        self. server = await asyncio.start_server(self.handle_client, host=self.host, port=self.port)
        await self.server.serve_forever()

    async def handle_client(self, reader, writer):
        addr = writer.get_extra_info('peername')
        self.reader = reader
        self.writer = writer

        while True:
            header_bytes = await self.reader.readexactly(Frame.Header.HEADER_SIZE)
            header = Frame.Header.from_bytes(header_bytes)
            if header.body_size == 0:
                print(f"Received body data is 0.")
            body = await self.reader.readexactly(header.body_size)
            json_data = body.decode().strip()
            print(f"Received json message from {addr}: {json_data}")
            jtype = json_data.get('type')
            jdata = json_data.get('data')
            e = Event(type=jtype, data=jdata)
            self.event_manager.put_event(e)

    async def stop(self):
        if self.server:
            self.server.close()
        self.status = 'STOP'

    async def send_event(self, event: Event):
        event_json = event.to_json()
        json_msg = json.dumps(event_json)
        await self.send(json_msg)

    async def send(self, message):
        print(f'Send message = {message}')
        if self.writer is None:
            print(f'but no client connected')
            return
        frame_bytes = Frame(message).to_bytes()
        self.writer.write(frame_bytes)
        await self.writer.drain()


class NetworkClient:
    def __init__(self,event_manager: EventManager, server_ip='127.0.0.1', server_port=8888):
        self.event_manager = event_manager
        self.server_ip = server_ip
        self.server_port = server_port
        self.reader = None
        self.writer = None
        self.status = 'INIT'

    async def start(self):
        # while self.status != 'CONNECTED':
        #     # TODO: asyncio.timeout need python 3.11
        #     async with asyncio.timeout(0.5):
        #         print(f"Connecting to server {self.server_ip}:{self.server_port}")
        #         await self._connect()
        #     await asyncio.sleep(3)
        #TODO: refactor reconnection. Now the client must wait for the server to be ready.
        while self.status != 'CONNECTED':
            async with asyncio.timeout(0.5):
                logging.info(f"Connecting to server {self.server_ip}:{self.server_port}")
                await self._connect()
            await asyncio.sleep(3)



        # self.reader, self.writer = await asyncio.open_connection(self.server_ip, self.server_port)
        # print(f"Connected to server {self.server_ip}:{self.server_port}")
        # self.status = 'CONNECTED'
        while True:
            # async with asyncio.timeout(2):
            # must use real program to test
            await self.receive()
            # await asyncio.sleep(0.1)

    async def _connect(self):
        try:
            self.reader, self.writer = await asyncio.open_connection(self.server_ip, self.server_port)
            logging.info(f"Connected to server {self.server_ip}:{self.server_port}")
            self.status = 'CONNECTED'
        except ConnectionRefusedError:
            logging.info(f"Connection to server {self.server_ip}:{self.server_port} refused.")
            self.status = 'DISCONNECTED'
        except asyncio.TimeoutError:
            logging.info(f"Connection to server {self.server_ip}:{self.server_port} timeout.")
            self.status = 'DISCONNECTED'
        except Exception as e:
            logging.info(f"Connection to server {self.server_ip}:{self.server_port} error: {e}")
            self.status = 'DISCONNECTED'

    async  def send_event(self, event):
        json_data = json.dumps(event.to_dict())
        await self.send(json_data)

    async def send(self, message):
        logging.info(f"Sending message {message}")
        frame_bytes = Frame(message).to_bytes()
        self.writer.write(frame_bytes)
        await self.writer.drain()

    async def receive(self):
        try:
            header_bytes = await self.reader.readexactly(Frame.Header.HEADER_SIZE)
            logging.info(f"Received header bytes data: {header_bytes}")
            header = Frame.Header.from_bytes(header_bytes)
            if header.body_size == 0:
                logging.info(f"Received body data is 0.")
            body = await self.reader.readexactly(header.body_size)
            logging.info(f"Received body data: {body}")
            json_data = json.loads(body)
            jtype = json_data.get('type')
            jdata = json_data.get('data')
            e = Event(type=jtype, data=jdata)
            self.event_manager.put_event(e)
        except asyncio.IncompleteReadError:
            logging.info("Incomplete read error")
            self.status = 'STOP'



    async def stop(self):
        if self.writer:
            self.writer.close()
            await self.writer.wait_closed()
        self.status = 'STOP'
