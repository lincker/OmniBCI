#!/usr/bin/env python
# -*- coding: utf-8 -*-
# @File     : acquisition_service.py
# @Author   : lincker
# @Date     : 2025-04-12 01:45
import asyncio
from enum import Enum
from acquisition.base import BaseReader
from core.data_manager import DataManager


class AcquisitionCommand(Enum):
    start_acquisition = 'start_acquisition'
    should_exit = 'acquisition_exit'
    get_metadata = 'device_metadata'
    connect = 'connect'
    start_stream = 'start_stream'
    stop_stream = 'stop_stream'
    disconnect = 'disconnect'

class AcquisitionState(Enum):
    stop = 'stop'
    running = 'running'
    paused = 'paused'


# run acquisitor and send the command to acquisitor to control
class Acquisition:
# TODO: change data queue to data manager
    def __init__(self, reader: BaseReader, command_queue, data_manager=None):
        self.reader = reader
        self.command_queue = command_queue
        self.status = AcquisitionState.stop
        self.fetch_task = None
        if data_manager is None:
            self.data_manager = DataManager()
        else:
            self.data_manager = data_manager
        self.should_exit = False

    def run(self):
        print('---run---')
        asyncio.run(self.serve())

    async def serve(self):
        await self.startup()
        # if self.should_exit:
        #     return
        await self.main_loop()
        await self.shutdown()

    async def shutdown(self):
        print('========================---acquisitor shut down---==============================')
        # TODO: if self.is_connected, need to teardown
        await self.reader.disconnect()
        if not self.should_exit:
            self.should_exit = True
            return

    async def startup(self):
        print('=========================---acquisitor start---=================================')
        # await self.device.setup()
        # self.status = AcquisitorState.running

    async def main_loop(self):
        counter = 0
        print('---main loop---')
        print(f'counter = {counter}')
        while not self.should_exit:
            # await command_queue.get()
            if not self.command_queue.empty():
                await self.execute(self.command_queue.get())
            # if self.status == AcquisitorState.running:
            #     await self.fetch()
            await asyncio.sleep(0.1)
            counter += 1
            print(f'counter = {counter}')

    async def execute(self, command):
        # TODO: refactor to use command pattern
        print(f'received execute command = {command}')

        if command == AcquisitionCommand.connect:
            await self.reader.connect()
            self.status = AcquisitionState.running
            return

        if command == AcquisitionCommand.disconnect:
            await self.reader.disconnect()
            self.status = AcquisitionState.stop
            return

        if self.status == AcquisitionState.running and command == AcquisitionCommand.start_stream:
            # start data stream, add fetch into event loop task
            self.fetch_task = asyncio.create_task(self.fetch())
            print('fetch task started')
            return

        if command == AcquisitionCommand.stop_stream:
            # stop data stream, cancel fetch task
            self.fetch_task.cancel()
            print('fetch task stopped')
            await self.reader.stop_stream()
            return

        if command == AcquisitionCommand.should_exit:
            self.should_exit = True
            return
        elif command == AcquisitionCommand.get_metadata:
            info = await self.reader.get_metadata()
            print(f'device info = {info}')
        else:
            print(f'unknown command = {command}')

    async def fetch(self):

        async for packet in self.reader.start_stream():
            packet = self.preprocess(packet)
            self.data_manager.add_packet(packet)
            await asyncio.sleep(0.1)

        print('fetching')

    def preprocess(self, data):
        # may detach eeg data and event
        print(f'preprocess data= {data}')
        return data
