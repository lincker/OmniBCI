'''
Author: lincker
Date: 2025-04-17 06:58:56
LastEditors: lincker haolin.d.wu@gmail.com
LastEditTime: 2025-12-25 09:27:33
FilePath: /OmniBCI/tests/test_acquisition_filereader.py
Description:

Copyright (c) 2025 by lincker, All Rights Reserved.
'''
import multiprocessing as mp
import os
import time
import asyncio

#!/usr/bin/env python
# -*- coding: utf-8 -*-
# @File     : test_acquisition_filereader.py
# @Author   : lincker
# @Date     : 2025-04-17 06:58
import pytest

from omnibci.infrastructure.acquisition.service import Acquisition, AcquisitionCommand
from omnibci.infrastructure.ipc.data_manager import DataManager
from omnibci.infrastructure.plugins.acquisition.file.reader import FileReader

file_path = os.path.abspath(__file__)
filename = 'train.cnt'
file_path = os.path.dirname(file_path)
file_path = os.path.join(file_path,'..', 'fixture', filename)


class TestFileReaderAcquisition:

    def start_acquisition(self, reader, command_queue, data_manager):
        self.acquisitor = Acquisition(data_source=reader, command_queue=command_queue, data_manager=data_manager)
        self.acquisitor.run()

    def test_acquisition(self):
        reader = FileReader()
        reader.set_file_path(file_path)
        command_queue = mp.Queue(20)
        data_manager = DataManager(asyncio.queues.Queue())
        p = mp.Process(target=self.start_acquisition, args=(reader, command_queue, data_manager))
        p.start()
        command_queue.put(AcquisitionCommand.connect)
        command_queue.put(AcquisitionCommand.start_stream)
        time.sleep(10)
        command_queue.put(AcquisitionCommand.stop_stream)
        command_queue.put(AcquisitionCommand.disconnect)
        command_queue.put(AcquisitionCommand.stop_acquisition)
        time.sleep(3)
        p.join()


if __name__ == '__main__':
    pytest.main(['-s', '-v', './tests/acquisition/test_acquisition_filereader.py'])
