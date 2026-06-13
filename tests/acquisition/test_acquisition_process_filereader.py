#!/usr/bin/env python
# -*- coding: utf-8 -*-
# @Author   : lincker <haolin.d.wu@gmail.com>
# @Date     : 2026-01-04 15:37
import asyncio
import multiprocessing as mp
import pytest
import time

from omnibci.infrastructure.ipc.data_manager import DataManager
from omnibci.infrastructure.acquisition.service import AcquisitionWorker, AcquisitionCommand


class TestAcquisitionWorkerFileReader:

    def test_acquisition_process_filereader(self):
        command_queue = mp.Queue(20)
        data_manager = DataManager(asyncio.queues.Queue())
        acquisition = AcquisitionWorker(command_queue=command_queue, data_manager=data_manager)
        acquisition.start()
        command_queue.put(AcquisitionCommand.connect)
        command_queue.put(AcquisitionCommand.start_stream)
        time.sleep(10)
        command_queue.put(AcquisitionCommand.stop_stream)
        command_queue.put(AcquisitionCommand.disconnect)
        command_queue.put(AcquisitionCommand.stop_acquisition)
        time.sleep(3)
        acquisition.join()

if __name__ == '__main__':
    pytest.main(['-s', '-v', './tests/acquisition/test_acquisition_filereader.py'])