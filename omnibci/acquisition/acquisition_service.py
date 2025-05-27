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
    stop_acquisition = 'stop_acquisition'
    get_metadata = 'reader_metadata'
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
        await self.reader.disconnect()


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
            await self.reader.stop_stream()
            self.fetch_task.cancel()
            print('fetch task stopped')

            return

        if command == AcquisitionCommand.stop_acquisition:
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
            await asyncio.sleep(0.01)

        print('fetching')

    def preprocess(self, data):
        # may detach eeg data and event
        # print(f'preprocess data= {data}')
        return data


import multiprocessing as mp
class AcquisitionProcess(mp.Process):
    def __init__(self, config, command_queue, data_manager=None):
        super().__init__()
        self.reader = self.get_reader_from_config(config)
        if self.reader is None:
            print(" Error! can not get device")
        self.command_queue = command_queue
        self.data_manager = data_manager
        # self.loop: typing.Optional[asyncio.AbstractEventLoop] = None
        # self.stopped: typing.Optional[asyncio.Event] = None

    def get_reader_from_config(self, config):
        reader = None
        if config['reader'] == 'filereader':
            from .file_reader import FileReader
            reader = FileReader(config)
        elif config['reader'] == 'neuroscan45':
            from .device_adapters.neuroscan.scan45.adapter import Scan45DataReader
            reader = Scan45DataReader(config)
        return reader

    def run(self):
        self._run()

    def _run(self):
        self.acquisitor = Acquisition(reader=self.reader, command_queue=self.command_queue, data_manager=self.data_manager)
        self.acquisitor.run()
