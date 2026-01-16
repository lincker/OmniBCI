#!/usr/bin/env python
# -*- coding: utf-8 -*-
# @File     : acquisition_service.py
# @Author   : lincker
# @Date     : 2025-04-12 01:45
import asyncio
import logging
import time
import os
from enum import Enum
from typing import Optional

from omnibci.core.base.acquisition import ReaderBase
from core.model import SignalChunk
from omnibci.core.base.factory import ReaderFactory


class AcquisitionCommand(Enum):
    start_acquisition = 'start_acquisition'
    stop_acquisition = 'stop_acquisition'
    get_metadata = 'reader_metadata'
    connect = 'connect'
    start_read = 'start_read'
    stop_read = 'stop_read'
    disconnect = 'disconnect'

class AcquisitionState(Enum):
    stop = 'stop'
    running = 'running'
    paused = 'paused'


# run acquisitor and send the command to acquisitor to control
class Acquisition:
# TODO: change data queue to data manager
    def __init__(self, reader: ReaderBase, command_queue, data_manager):
        self.reader = reader
        self.command_queue = command_queue
        self.status = AcquisitionState.stop
        self.fetch_task: Optional[asyncio.Task] = None
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
        logging.info('========================---acquisitor shut down---==============================')
        await self.reader.disconnect()

    async def startup(self):
        logging.info('=========================---acquisitor start---=================================')
        # await self.device.setup()
        # self.status = AcquisitorState.running

    async def main_loop(self):
        counter = 0
        print('---main loop---')
        while not self.should_exit:
            # await command_queue.get()
            if not self.command_queue.empty():
                await self.execute(self.command_queue.get())
            # if self.status == AcquisitorState.running:
            #     await self.fetch()
            await asyncio.sleep(0.1)
            counter += 1

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

        if self.status == AcquisitionState.running and command == AcquisitionCommand.start_read:
            # start data stream, add fetch into event loop task
            await self.reader.start_read()
            self.fetch_task = asyncio.create_task(self.fetch())
            print('fetch task started')
            return

        if command == AcquisitionCommand.stop_read:
            # TODO: check the reader state first, if in read state, then stop
            # stop data stream, cancel fetch task
            await self.reader.stop_read()
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
        logging.info('fetching')
        packets_counter = 0
        try:
            while True:
                raw_data = await self.reader.read_unit()
                data = SignalChunk(
                            timestamp=time.time_ns(),
                            raw_data=raw_data,
                            sequence_id=packets_counter,
                        )
                data = self.preprocess(data)
                self.data_manager.put_data(data)
                packets_counter += 1
        except asyncio.CancelledError:
            logging.info('Fetch task cancelled')
        except Exception as e:
            logging.error(f'Error in data streaming task: {e}')

    def preprocess(self, data):
        # may detach eeg data and event
        # print(f'preprocess data=')
        # TODO: mapping channels according to selected algorithm.
        # 如果采集的是没有trigger的数据，可以在此增加全0的trigger 通道
        return data


import multiprocessing as mp


class AcquisitionProcess(mp.Process):
    def __init__(self, command_queue, data_manager=None):
        super().__init__()
        self.reader: ReaderBase = None
        self.acquisitor = None
        self.command_queue = command_queue
        self.data_manager = data_manager

    def run(self):
        self._load_reader()
        self._run()

    def _run(self):
        self.acquisitor = Acquisition(reader=self.reader, command_queue=self.command_queue, data_manager=self.data_manager)
        self.acquisitor.run()

    def _load_reader(self):
        from omnibci.core.base.factory import load_plugins
        acquisition_plugin_path = os.path.join(os.path.dirname(__file__), '..', 'plugin', 'acquisition')
        load_plugins(acquisition_plugin_path)
        logging.info("Initializing Reader in Child Process...")
        self.reader = ReaderFactory.get_reader()
