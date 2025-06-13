#!/usr/bin/env python
# -*- coding: utf-8 -*-
# @Author   : lincker <haolin.d.wu@gmail.com>
# @Date     : 2025-04-25 09:45
from dataclasses import dataclass
from numpy import typing as npt
import numpy as np

from core.data_model import DataPacket


# @dataclass
# class Epoch:
#     data: npt.NDArray
#     event: npt.NDArray
#
#     def clear(self):
#         self.data = None
#         self.event = None


class EpochManager:
    def __init__(self, analysis_method):


        self._eegdata = np.empty((0, 0))
        self._head_event = None
        self._event_index = None
        self._begin = False
        self.analysis_method = analysis_method

    def begin_recognition(self, data_packet) -> bool:
        index, event = self.analysis_method.check_event(data_packet)
        if index >= 0:
            print(f"EpochManager: begin_recognition, index={index}, event={event}")
            return True
        else:
            return False


    def set_epoch_head_data(self, data_packet):
        index, event = self.analysis_method.check_event(data_packet)
        if index >= 0:
            new_data = data_packet.data[0:-1, index:]  # Slice rows
            self._eegdata = new_data
        else:
            raise ValueError("Epoch head index is not set.")

    def append_epoch_tail_data(self, data_packet):
        index, event = self.analysis_method.check_event(data_packet)
        if index >= 0:
            new_data = data_packet.data[0:-1, 0:index]  # Slice rows
            self._eegdata = np.append(self._eegdata, new_data, axis=1)
        else:
            raise ValueError("Epoch head index is not set.")

    def append_data(self, data_packet):
        new_data = data_packet.data[0:-1, :]  # Slice rows
        self._eegdata = np.append(self._eegdata, new_data, axis=1)


    def analysis(self, force_calc=False) -> (bool, npt.NDArray):
        return self.analysis_method.recognize(self._eegdata,force_calc)

    # def clear(self):
    #     self._eegdata = np.empty((0, 0))
    #     self._head_event = None
    #     self._event_index = None
