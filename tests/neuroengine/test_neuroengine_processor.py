#!/usr/bin/env python
# -*- coding: utf-8 -*-
# @Author   : lincker <haolin.d.wu@gmail.com>
# @Date     : 2025-04-26 16:09
import pytest
import multiprocessing as mp
import time
from omnibci.infrastructure.ipc.data_manager import DataManager
from omnibci.infrastructure.acquisition.service import AcquisitionWorker, AcquisitionCommand
from omnibci.infrastructure.neuroengine.processing.worker import SignalProcessingWorker
from omnibci.infrastructure.neuroengine.processing.signal_processor import SignalProcessorCommand
from omnibci.infrastructure.ipc.event_bus import EventBus


# file_path = os.path.abspath(__file__)
# filename = 'train.cnt'
# file_path = os.path.dirname(file_path)
# file_path = os.path.join(file_path,'..', 'fixture', filename)
# settings.set('ACQUISITION.data_file_path', file_path)
# settings.set('ACQUISITION.signal_source', 'file_reader')


class TestNeuroEngineProcessor:
    def test_processor(self):
        acquisition_command_queue = mp.Queue(20)
        processor_command_queue = mp.Queue(20)
        data_queue = mp.Queue()
        data_manager = DataManager(buffer=data_queue)
        event_bus = EventBus()

        p_pp = SignalProcessingWorker(command_queue=processor_command_queue, data_manager=data_manager,
                                      event_bus=event_bus)
        p_pp.start()
        p_ap = AcquisitionWorker(command_queue=acquisition_command_queue, data_manager=data_manager)
        p_ap.start()

        acquisition_command_queue.put(AcquisitionCommand.connect)
        time.sleep(5)
        acquisition_command_queue.put(AcquisitionCommand.start_stream)

        time.sleep(20)
        acquisition_command_queue.put(AcquisitionCommand.stop_stream)
        time.sleep(10)
        acquisition_command_queue.put(AcquisitionCommand.stop_acquisition)
        # time.sleep(100) # to be removed
        # if queue/data manager's queue is not empty, the process will not join
        p_ap.join(timeout=1)
        if p_ap.is_alive():
            p_ap.terminate()
        processor_command_queue.put(SignalProcessorCommand.should_stop)
        p_pp.join(timeout=1)
        if p_pp.is_alive():
            p_pp.terminate()
        data_queue.close()
        data_queue.join_thread()
        acquisition_command_queue.close()
        acquisition_command_queue.join_thread()
        processor_command_queue.close()
        processor_command_queue.join_thread()


if __name__ == '__main__':
    pytest.main(['-s', '-v', './tests/neuroengine/test_neuroengine_processor.py'])