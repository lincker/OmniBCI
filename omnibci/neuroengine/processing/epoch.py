'''
Author: lincker
Date: 2025-06-13 10:44:23
LastEditors: lincker haolin.d.wu@gmail.com
LastEditTime: 2025-12-25 11:08:58
FilePath: /OmniBCI/omnibci/neuroengine/processing/epoch.py
Description:

Copyright (c) 2025 by lincker, All Rights Reserved.
'''
#!/usr/bin/env python
# -*- coding: utf-8 -*-
# @Author   : lincker <haolin.d.wu@gmail.com>
# @Date     : 2025-04-25 09:45
from dataclasses import dataclass

import numpy as np
from numpy import typing as npt

from omnibci.core.algorithm import BaseRecognizer
from omnibci.core.data_model import StreamFrame

# @dataclass
# class Epoch:
#     data: npt.NDArray
#     event: npt.NDArray
#
#     def clear(self):
#         self.data = None
#         self.event = None


class EpochManager:
    def __init__(self, recognizer: BaseRecognizer):


        self.epoch_data = np.empty((0, 0))
        self.current_data_packet = None
        self.start_event = None
        # self._head_event = None
        # self._event_index = None
        # self._begin = False
        self.recognizer = recognizer

    def begin_recognition(self, data_packet) -> bool:
        index, event = self.recognizer.trial_start_position(data_packet)
        if index >= 0:
            print(f"EpochManager: begin_recognition, index={index}, event={event}")
            self.start_event = event
            return True
        else:
            return False

    def end_recognition(self, data_packet) -> bool:
        index, event = self.recognizer.trial_end_position(data_packet)
        if index >= 0:
            print(f"EpochManager: end_recognition, index={index}, event={event}")
            return True
        else:
            return False


    def set_epoch_head_data(self, data_packet):
        index, event = self.recognizer.trial_start_position(data_packet)
        if index >= 0:
            new_data = data_packet.data[0:-1, index:]  # Slice rows
            self.epoch_data = new_data
        else:
            raise ValueError("Epoch head index is not set.")

    def append_epoch_tail_data(self, data_packet):
        index, event = self.recognizer.trial_end_position(data_packet)
        if index >= 0:
            new_data = data_packet.data[0:-1, 0:index]  # Slice rows
            self.epoch_data = np.append(self.epoch_data, new_data, axis=1)
        else:
            raise ValueError("Epoch tail index is not set.")

    def append_data_before_begin_event(self, data_packet):
        index, event = self.recognizer.trial_start_position(data_packet)
        if index >= 0:
            new_data = data_packet.data[0:-1, 0:index]
            self.epoch_data = np.append(self.epoch_data, new_data, axis=1)
        else:
            raise ValueError("Epoch head index is not set.")

    def append_data(self, data_packet):
        self.current_data_packet = data_packet.data[0:-1, :]  # Slice rows
        self.epoch_data = np.append(self.epoch_data, self.current_data_packet, axis=1)


    def analysis(self, force_predict=False) -> (bool, npt.NDArray):
        return self.recognizer.recognize(self.epoch_data, self.current_data_packet, force_predict)

    def clear(self):
        self.epoch_data = np.empty((0, 0))
        self.current_data_packet = None
        self.start_event = None
