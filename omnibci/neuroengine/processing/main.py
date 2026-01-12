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
import os
import multiprocessing as mp
import logging

from omnibci.core.base.algorithm import RecognizerBase
from omnibci.core.base.factory import RecognizerFactory
from omnibci.neuroengine.processing.pipeline import Pipeline


class PipelineProcess(mp.Process):
    def __init__(self, command_queue, data_manager=None, event_manager=None):
        super().__init__()
        self.data_manager = data_manager
        self.event_manager = event_manager
        self.command_queue = command_queue
        self.recognizer: RecognizerBase = None
        self.pipeline = None

    def run(self):
        self._load_recognizer()
        self._run()

    def _run(self):
        self.pipeline = Pipeline(recognizer=self.recognizer, command_queue=self.command_queue, data_manager=self.data_manager, event_manager=self.event_manager)
        self.pipeline.run()

    def _load_recognizer(self):
        from omnibci.core.base.factory import load_plugins
        algorithm_plugin_path = os.path.join(os.path.dirname(__file__), '..', '..', 'plugin', 'algorithm')
        load_plugins(algorithm_plugin_path)
        logging.info("Initializing algorithm in Child Process...")
        self.recognizer = RecognizerFactory.get_recognizer()
