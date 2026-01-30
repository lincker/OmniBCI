#!/usr/bin/env python
# -*- coding: utf-8 -*-
# @File     : service.py
# @Author   : lincker
# @Date     : 2025-04-12 01:45
import asyncio
import logging
import time
import os
from enum import Enum
from typing import Optional

from interfaces.acquisition import DataSourceBase
from core.data.types import SignalChunk

logger = logging.getLogger(__name__)


class AcquisitionCommand(Enum):
    start_acquisition = 'start acquisition'
    stop_acquisition = 'stop acquisition'
    get_metadata = 'get metadata'
    connect = 'connect'
    start_stream = 'start stream'
    stop_stream = 'stop stream'
    disconnect = 'disconnect'

class AcquisitionState(Enum):
    stop = 'stop'
    running = 'running'
    paused = 'paused'


# run acquisitor and send the command to acquisitor to control
class Acquisition:
    def __init__(self, data_source: DataSourceBase, command_queue, data_manager):
        self.data_source = data_source
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
        await self.main_loop()
        await self.shutdown()

    async def shutdown(self):
        logger.info('========================---acquisitor shut down---==============================')
        if self.data_source._is_connected:
            await self.data_source.disconnect()

    async def startup(self):
        logger.info('=========================---acquisitor start---=================================')
        # await self.device.setup()
        # self.status = AcquisitorState.running

    async def main_loop(self):
        counter = 0
        print('---main loop---')
        while not self.should_exit:
            if not self.command_queue.empty():
                await self.execute(self.command_queue.get())
            await asyncio.sleep(0.01)
            counter += 1

    async def execute(self, command):
        # TODO: refactor to use command pattern
        print(f'received execute command = {command}')

        if command == AcquisitionCommand.connect:
            await self.data_source.connect()
            self.status = AcquisitionState.running
            return

        if command == AcquisitionCommand.disconnect:
            await self.data_source.disconnect()
            self.status = AcquisitionState.stop
            return

        if self.status == AcquisitionState.running and command == AcquisitionCommand.start_stream:
            # start data stream, add fetch into events loop task
            await self.data_source.start_stream()
            self.fetch_task = asyncio.create_task(self.fetch())
            print('fetch task started')
            return

        if command == AcquisitionCommand.stop_stream:
            # TODO: check the data_source state first, if in stream state, then stop
            # stop data stream, cancel fetch task
            await self.data_source.stop_stream()
            self.fetch_task.cancel()
            print('fetch task stopped')
            return

        if command == AcquisitionCommand.stop_acquisition:
            self.should_exit = True
            return
        elif command == AcquisitionCommand.get_metadata:
            info = await self.data_source.get_metadata()
            print(f'device info = {info}')
        else:
            print(f'unknown command = {command}')

    async def fetch(self):
        logger.info('fetching')
        packets_counter = 0
        try:
            while True:
                raw_data = await self.data_source.read_chunk()
                data = SignalChunk(
                            timestamp=time.time_ns(),
                            raw_data=raw_data,
                            sequence_id=packets_counter,
                        )
                data = self.preprocess(data)
                self.data_manager.put_data(data)
                packets_counter += 1
        except asyncio.CancelledError:
            logger.info('Fetch task cancelled')
        except Exception as e:
            logger.error(f'Error in data streaming task: {e}')

    def preprocess(self, data):
        # may detach eeg data and events
        # print(f'preprocess data=')
        # TODO: mapping channels according to selected algorithm.
        # 如果采集的是没有trigger的数据，可以在此增加全0的trigger 通道
        return data


import multiprocessing as mp


class AcquisitionProcess(mp.Process):
    def __init__(self, command_queue, data_manager=None):
        super().__init__()
        self.data_source: DataSourceBase
        self.acquisitor = None
        self.command_queue = command_queue
        self.data_manager = data_manager

    def run(self):
        self._load_data_source()
        self._run()

    def _run(self):
        self.acquisitor = Acquisition(data_source=self.data_source, command_queue=self.command_queue, data_manager=self.data_manager)
        self.acquisitor.run()

    def _load_data_source(self):
        acquisition_plugin_path = os.path.join(os.path.dirname(__file__), '..', 'plugins', 'acquisition')
        from core.utils.loader import PluginLoader
        from core.factory import ObjectFactory
        PluginLoader.load_from_dir(acquisition_plugin_path, base_package='omnibci.plugins.acquisition')
        self.data_source = ObjectFactory.create(DataSourceBase, config_key='ACQUISITION.data_source')
