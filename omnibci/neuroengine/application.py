#!/usr/bin/env python
# -*- coding: utf-8 -*-
# @Author   : lincker <haolin.d.wu@gmail.com>
# @Date     : 2025-04-27 10:10
import multiprocessing as mp
import asyncio

from core.data_manager import DataManager
from core.event.base import EventType
from core.event.event_manager import EventManager
from acquisition.acquisition_service import AcquisitionProcess, AcquisitionCommand
from .analysis.pipeline import AnalysisPipelineProcess
from .analysis.analysis_controller import AnalysisCommand
from .message.management import ServerMessageManagement


class MainApplication:
    def __init__(self, config=None):
        self.config = config
        # TODO: put acquisitor command queue and analysis command queue into event manager
        self.event_manager = EventManager()
        self.event_manager.set_event_queue(queue=mp.Queue())
        # TODO: refactor , not process, use thread
        self.sever_message_management = ServerMessageManagement(self.event_manager)
        self.data_manager = DataManager(data_buffer=mp.Queue())
        # TODO: refactor this
        self.acquisition_command_queue = mp.Queue(20)
        self.acquisitor = AcquisitionProcess(config, self.acquisition_command_queue, self.data_manager)
        self.acquisitor.start()
        self.analysis_command_queue = mp.Queue(20)
        self.analysis_controller = AnalysisPipelineProcess(config=config,command_queue=self.analysis_command_queue, data_manager=self.data_manager, event_manager=self.event_manager)
        self.analysis_controller.start()
        self.should_stop = False

    def run(self):
        # self.serve()
        asyncio.run(self.serve(), debug=True)

    async def serve(self):
        await self.start()
        await self.main_loop()
        self.stop()

    async def start(self):
        await self.event_manager.start()
        self.sever_message_management.start()
        self.register_event_listener()

    def register_event_listener(self):
        self.event_manager.register_event_listener(event_type=EventType.ConnectDataProvider, handler=self._connect_data_provider)
        self.event_manager.register_event_listener(event_type=EventType.DisconnectDataProvider, handler=self._disconnect_data_provider)
        self.event_manager.register_event_listener(event_type=EventType.StartReceive, handler=self._reader_start_stream)
        self.event_manager.register_event_listener(event_type=EventType.StopReceive, handler=self._reader_stop_stream)
        # self.event_manager.register_event_listener(event_type=EventType.StopAquisitionDevice, handler=self._stop_acquisitor)
        self.event_manager.register_event_listener(event_type=EventType.StopAnalysis, handler=self._stop_analysis)
        self.event_manager.register_event_listener(event_type=EventType.StopProcessSystem, handler=self._stop_process_system)




    async def main_loop(self):
        # time.sleep(10)
        # self.acquisitor_command_queue.put(AcquisitorCommand.start_data_stream)
        # # TODO: time sleep must before analysis_controller run
        # # time.sleep(20)
        # self.event_manager.put_event(event=Event(EventType.StartStimTrail))
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

    def _reader_start_stream(self, event):
        self.acquisition_command_queue.put(AcquisitionCommand.start_stream)

    def _reader_stop_stream(self, event):
        self.acquisition_command_queue.put(AcquisitionCommand.stop_stream)
    def _stop_acquisition(self, event):
        self.acquisition_command_queue.put(AcquisitionCommand.stop_acquisition)

    def _stop_analysis(self, event):
        self.analysis_command_queue.put(AnalysisCommand.should_stop)

    def _stop_process_system(self, event):
        self.should_stop = True

    def stop(self):
        self.acquisitor.terminate()
        self.analysis_controller.terminate()
        self.analysis_command_queue.close()
        self.analysis_command_queue.join_thread()
        self.acquisition_command_queue.close()
        self.acquisition_command_queue.join_thread()
        self.event_manager.stop()
        self.sever_message_management.stop()


