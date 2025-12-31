'''
Author: lincker
Date: 2025-05-27 14:47:36
LastEditors: lincker haolin.d.wu@gmail.com
LastEditTime: 2025-12-25 11:08:02
FilePath: /OmniBCI/omnibci/neuroengine/processing/main.py
Description:

Copyright (c) 2025 by lincker, All Rights Reserved.
'''
#!/usr/bin/env python
# -*- coding: utf-8 -*-
# @Author   : lincker <haolin.d.wu@gmail.com>
# @Date     : 2025-04-24 09:31
import multiprocessing as mp

from omnibci.neuroengine.processing.pipeline import AnalysisController


class NeuroEngineProcess(mp.Process):
    def __init__(self, command_queue, data_manager=None, event_manager=None):
        super().__init__()
        self.data_manager = data_manager
        self.event_manager = event_manager
        self.command_queue = command_queue

    def run(self):
        self._run()

    def _run(self):
        self.analysis_controller = AnalysisController(command_queue=self.command_queue, data_manager=self.data_manager, event_manager=self.event_manager)
        self.analysis_controller.run()
