#!/usr/bin/env python
# -*- coding: utf-8 -*-
# @Author   : lincker <haolin.d.wu@gmail.com>
# @Date     : 2025-04-27 10:10
import asyncio
import multiprocessing as mp
import logging

from omnibci.acquisition.service import (AcquisitionCommand,
                                         AcquisitionProcess)
from core.data.manager import DataManager
from omnibci.core.events.types import EventType
from omnibci.core.events.bus import EventBus
from .processor.processor import SignalProcessorProcess, SignalProcessorCommand
from .server.management import ServerMessageManagement

logger = logging.getLogger(__name__)

class NeuroEngine:
    def __init__(self, queue_size: int = 20):
        self.event_bus = EventBus()
        self.event_bus.set_event_queue(queue=mp.Queue())
        self.server_message_management = ServerMessageManagement(self.event_bus)
        self.data_manager = DataManager(buffer=mp.Queue())
        self.acquisition_command_queue = mp.Queue(queue_size)
        self.acquisitor_process = AcquisitionProcess(self.acquisition_command_queue, self.data_manager)
        self.processor_command_queue = mp.Queue(queue_size)
        self.signal_processor_process = SignalProcessorProcess(command_queue=self.processor_command_queue, data_manager=self.data_manager, event_bus=self.event_bus)
        self.should_stop = False

    def run(self):
        asyncio.run(self.serve(), debug=True)

    async def serve(self):
        await self.start()
        await self.main_loop()
        await self.stop()

    async def start(self):
        self.acquisitor_process.start()
        self.signal_processor_process.start()

        await self.event_bus.start()
        self.server_message_management.start()
        self.register_event_listener()
        logger.info("NeuroEngine started successfully")

    def register_event_listener(self):
        self.event_bus.register_event_listener(event_type=EventType.ConnectDataProvider, handler=self._connect_data_provider)
        self.event_bus.register_event_listener(event_type=EventType.DisconnectDataProvider, handler=self._disconnect_data_provider)
        self.event_bus.register_event_listener(event_type=EventType.StartStream, handler=self._reader_start_stream)
        self.event_bus.register_event_listener(event_type=EventType.StopStream, handler=self._reader_stop_stream)
        self.event_bus.register_event_listener(event_type=EventType.StopNeuroEngineSignalProcessor, handler=self._stop_signal_processor)
        self.event_bus.register_event_listener(event_type=EventType.CloseAllNeuroEngineProcessSystem, handler=self._stop_neuro_engine_system)




    async def main_loop(self):
        while not self.should_stop:
            await asyncio.sleep(0.05)


    def _connect_data_provider(self):
        self.acquisition_command_queue.put(AcquisitionCommand.connect)

    def _disconnect_data_provider(self):
        self.acquisition_command_queue.put(AcquisitionCommand.disconnect)

    def _reader_start_stream(self):
        self.acquisition_command_queue.put(AcquisitionCommand.start_stream)

    def _reader_stop_stream(self):
        self.acquisition_command_queue.put(AcquisitionCommand.stop_stream)
    def _stop_acquisition(self):
        self.acquisition_command_queue.put(AcquisitionCommand.stop_acquisition)

    def _stop_signal_processor(self):
        self.processor_command_queue.put(SignalProcessorCommand.should_stop)

    def _stop_neuro_engine_system(self):
        self.should_stop = True

    async def stop(self):
        logger.info("Initiating NeuroEngine shutdown")
        self._stop_acquisition()
        self._stop_signal_processor()

        self.event_bus.stop()
        await self.server_message_management.stop()

        self.acquisitor_process.join(timeout=3)

        if self.acquisitor_process.is_alive():
            logger.warning("AcquisitionProcess did not exit gracefully, terminating...")
            self.acquisitor_process.terminate()

        self.signal_processor_process.join(timeout=3)
        if self.signal_processor_process.is_alive():
            logger.warning("SignalProcessorProcess did not exit gracefully, terminating...")
            self.signal_processor_process.terminate()

        self.processor_command_queue.close()
        self.processor_command_queue.join_thread()
        self.acquisition_command_queue.close()
        self.acquisition_command_queue.join_thread()

        logger.info("NeuroEngine shutdown complete")



