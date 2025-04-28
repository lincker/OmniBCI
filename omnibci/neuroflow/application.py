#!/usr/bin/env python
# -*- coding: utf-8 -*-
# @Author   : lincker <haolin.d.wu@gmail.com>
# @Date     : 2025-04-27 10:10
import multiprocessing as mp
import asyncio

from core.data_manager import DataManager
from core.event.base import EventType
from core.event.event_manager import EventManager
from acquisition.acquisition_service import AcquisitionProcess
from .analysis.pipeline import AnalysisPipelineProcess
from .message.management import ServerMessageManagement


class MainApplication:
    def __init__(self, config=None):
        self.config = config
        # TODO: put acquisitor command queue and analysis command queue into event manager
        self.event_manager = EventManager()
        self.event_manager.set_event_queue(queue=mp.Queue())
        # TODO: refactor , not process, use thread
        self.sever_message_management = ServerMessageManagement(self.event_manager, config=config)
        self.data_manager = DataManager(data_buffer=mp.Queue())
        # TODO: refactor this
        self.acquisitor_command_queue = mp.Queue(20)
        self.acquisitor = AcquisitionProcess(config, self.acquisitor_command_queue, self.data_manager)
        self.acquisitor.start()
        self.analysis_command_queue = mp.Queue(20)
        self.analysis_controller = AnalysisPipelineProcess(config=config, data_manager=self.data_manager, event_manager=self.event_manager)
        self.analysis_controller.start()
        self.should_stop = False
        # analysis process

        # message server send and receive message between stimulator and application
        # TODO: refactor this: maybe in the event manager
        # self.message_server = None

        self.operation_operator = None


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
        self.event_manager.register_event_listener(event_type=EventType.StartReceive, handler=self._start_acquisitor_data_stream)
        self.event_manager.register_event_listener(event_type=EventType.StopReceive, handler=self._stop_acquisitor_data_stream)
        self.event_manager.register_event_listener(event_type=EventType.StopAquisionDevice, handler=self._stop_acquisitor)
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
        self.acquisitor_command_queue.put(AcquisitorCommand.setup)

    def _disconnect_data_provider(self, event):
        self.acquisitor_command_queue.put(AcquisitorCommand.teardown)

    def _start_acquisitor_data_stream(self, event):
        self.acquisitor_command_queue.put(AcquisitorCommand.start_data_stream)

    def _stop_acquisitor_data_stream(self, event):
        self.acquisitor_command_queue.put(AcquisitorCommand.stop_data_stream)

    def _stop_acquisitor(self, event):
        self.acquisitor_command_queue.put(AcquisitorCommand.should_exit)

    def _stop_analysis(self, event):
        self.analysis_command_queue.put(AnalysisCommand.should_stop)

    def _stop_process_system(self, event):
        self.should_stop = True

    def stop(self):
        self.acquisitor.terminate()
        self.acquisitor.join()
        self.analysis_controller.terminate()
        self.analysis_controller.join()
        self.sever_message_management.stop()
        # self.sever_message_management.join()
        self.is_running = False

