#!/usr/bin/env python
# -*- coding: utf-8 -*-
# @Author   : lincker <haolin.d.wu@gmail.com>
# @Date     : 2025-04-27 10:10
import asyncio
import multiprocessing as mp

from omnibci.acquisition.acquisition_service import (AcquisitionCommand,
                                                     AcquisitionProcess)
from core.data_manager import DataManager
from omnibci.core.event.base import EventType
from omnibci.core.event.event_manager import EventManager

from .processing.pipeline import PipelineCommand
from .processing.main import PipelineProcess
from .server.management import ServerMessageManagement


class NeuroEngine:
    def __init__(self, config=None):
        self.config = config
        # TODO: put acquisitor command queue and processing command queue into event manager
        self.event_manager = EventManager()
        self.event_manager.set_event_queue(queue=mp.Queue())
        # TODO: refactor , not process, use thread
        self.sever_message_management = ServerMessageManagement(self.event_manager)
        self.data_manager = DataManager(buffer=mp.Queue())
        # TODO: refactor this
        self.acquisition_command_queue = mp.Queue(20)
        self.acquisitor = AcquisitionProcess(self.acquisition_command_queue, self.data_manager)
        self.acquisitor.start()
        self.pipeline_command_queue = mp.Queue(20)
        self.pipeline = PipelineProcess(command_queue=self.pipeline_command_queue, data_manager=self.data_manager, event_manager=self.event_manager)
        self.pipeline.start()
        self.should_stop = False

    def run(self):
        # self.serve()
        asyncio.run(self.serve(), debug=True)

    async def serve(self):
        await self.start()
        await self.main_loop()
        await self.stop()

    async def start(self):
        await self.event_manager.start()
        self.sever_message_management.start()
        self.register_event_listener()

    def register_event_listener(self):
        self.event_manager.register_event_listener(event_type=EventType.OpenDataProvider, handler=self._connect_data_provider)
        self.event_manager.register_event_listener(event_type=EventType.DisconnectDataProvider, handler=self._disconnect_data_provider)
        self.event_manager.register_event_listener(event_type=EventType.StartRead, handler=self._reader_start_read)
        self.event_manager.register_event_listener(event_type=EventType.StopRead, handler=self._reader_stop_read)
        # self.event_manager.register_event_listener(event_type=EventType.StopAquisitionDevice, handler=self._stop_acquisitor)
        self.event_manager.register_event_listener(event_type=EventType.StopNeuroEnginePipeline, handler=self._stop_pipeline)
        self.event_manager.register_event_listener(event_type=EventType.CloseAllNeuroEngineProcessSystem, handler=self._stop_neuro_engine_system)




    async def main_loop(self):
        # time.sleep(10)
        # self.acquisitor_command_queue.put(AcquisitorCommand.start_data_stream)
        # # TODO: time sleep must before analysis_controller run
        # # time.sleep(20)
        # self.event_manager.put_event(event=Event(EventType.StartStimTrial))
        # time.sleep(20)
        # self.acquisitor_command_queue.put(AcquisitorCommand.stop_data_stream)
        # time.sleep(5)
        # self.acquisitor_command_queue.put(AcquisitorCommand.should_exit)
        # time.sleep(10)
        # self.analysis_command_queue.put(AnalysisCommand.should_stop)

        while not self.should_stop:
            # if not self.command_queue.empty():
            #     self.execute(self.command_queue.get())
            await asyncio.sleep(0.1)
            # counter += 1
            # if counter > 50:
            #     self.should_stop = True

    def _connect_data_provider(self, event):
        self.acquisition_command_queue.put(AcquisitionCommand.connect)

    def _disconnect_data_provider(self, event):
        self.acquisition_command_queue.put(AcquisitionCommand.disconnect)

    def _reader_start_read(self, event):
        self.acquisition_command_queue.put(AcquisitionCommand.start_read)

    def _reader_stop_read(self, event):
        self.acquisition_command_queue.put(AcquisitionCommand.stop_read)
    def _stop_acquisition(self, event):
        self.acquisition_command_queue.put(AcquisitionCommand.stop_acquisition)

    def _stop_pipeline(self, event):
        self.pipeline_command_queue.put(PipelineCommand.should_stop)

    def _stop_neuro_engine_system(self, event):
        self.should_stop = True

    async def stop(self):
        self.acquisitor.terminate()
        self.pipeline.terminate()
        self.pipeline_command_queue.close()
        self.pipeline_command_queue.join_thread()
        self.acquisition_command_queue.close()
        self.acquisition_command_queue.join_thread()
        self.event_manager.stop()
        await self.sever_message_management.stop()


