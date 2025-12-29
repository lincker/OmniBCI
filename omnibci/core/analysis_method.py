#!/usr/bin/env python
# -*- coding: utf-8 -*-
# @Author   : lincker <haolin.d.wu@gmail.com>
# @Date     : 2025-04-25 10:53
from abc import ABC, abstractmethod
from typing import Any

from numpy import typing as npt

from .data_model import CommonDataModel


class AnalysisMethod(ABC):

    def __init__(self) -> None:
        super().__init__()

    # # is start: bool, data: numpy array, event position: int
    # def find_event_position(self, data_packet: DataPacket) -> (bool, int, int):
    #     data = data_packet.data
    #     event_data = data[-1, :]
    #     for index, event in enumerate(event_data):
    #         # found event trigger
    #         if event > 0:
    #             started = True
    #             return index, event
    #     # not found event trigger
    #     return -1, 0

    @abstractmethod
    def recognize(self, all_data, current_packet_data, force_calc) -> (bool, npt.NDArray[Any], int):
        pass

    #return position and event value
    def trial_start_position(self, data_packet:CommonDataModel, event_set=None) -> (int, int):
        data = data_packet.data
        event_data = data[-1, :]
        for index, event in enumerate(event_data):
            # found event trigger
            if event > 0:
                started = True
                return index, event
        return -1, 0

    #return position and event value
    def trial_end_position(self, data_packet:CommonDataModel, event_set=None) -> (int, int):
        return -1, 0

    # return the index of the first event position, if not found, return -1
    # def epoch_event_position(self, data_packet:DataPacket, event_value:int=0):
    #     event_data = data_packet.data[-1, :]
    #     for index, event in enumerate(event_data):
    #         if event == event_value:
    #             return index
    #     return -1
