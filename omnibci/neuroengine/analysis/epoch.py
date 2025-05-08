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
        self.analysis_method = analysis_method

    def head_recognition(self, data_packet) -> bool:
        started, index, event = self.analysis_method.is_epoch_head(data_packet)
        if started:
            self._head_event = event
            self._event_index = index

            return True
        else:
            return False

    def set_epoch_head_data(self, data_packet):
        if self._event_index is not None:
            new_data = data_packet.data[0:-1, self._event_index:]  # Slice rows
            if new_data.ndim == 1:
                new_data = new_data.reshape(-1, 1)  # Reshape to (n_samples, 1)

            # self._eegdata = data_packet.data[0:data_packet.data.shape[0] - 1, self._event_index:]
            self._eegdata = new_data
        else:
            raise ValueError("Epoch head index is not set.")

    def append_epoch_tail_data(self, data_packet):
        if self._event_index is not None:
            new_data = data_packet.data[0:-1, 0:self._event_index]  # Slice rows
            if new_data.ndim == 1:
                new_data = new_data.reshape(-1, 1)  # Reshape to (n_samples, 1)
            self._eegdata = np.append(self._eegdata, new_data, axis=1)
            # self._eegdata = np.append(self._eegdata, data_packet.data[0:data_packet.data.shape[0] - 1, 0:self._event_index], axis=1)
        else:
            raise ValueError("Epoch head index is not set.")

    def append_data(self, data_packet):
        new_data = data_packet.data[0:-1, :]  # Slice rows
        if new_data.ndim == 1:
            new_data = new_data.reshape(-1, 1)  # Reshape to (n_samples, 1)
        self._eegdata = np.append(self._eegdata, new_data, axis=1)
        # self._eegdata = np.append(self._eegdata, data_packet.data[0:data_packet.data.shape[0] - 1, :], axis=1)


    def analysis(self, force_calc=False) -> (bool, npt.NDArray):
        return self.analysis_method.recognize(self._eegdata,force_calc)

    # def clear(self):
    #     self._eegdata = np.empty((0, 0))
    #     self._head_event = None
    #     self._event_index = None
