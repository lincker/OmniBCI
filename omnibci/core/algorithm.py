#!/usr/bin/env python
# -*- coding: utf-8 -*-
# @Author   : lincker <haolin.d.wu@gmail.com>
# @Date     : 2025-04-25 10:53
from abc import ABC, abstractmethod
from typing import Any

from numpy import typing as npt

from .data_model import StreamFrame


class BaseProcessor(ABC):
    @abstractmethod
    def process(self, data: npt.NDArray[Any]) -> npt.NDArray[Any]:
        """
        Takes data, transforms it, and returns it for the next step.
        Returns None if processing should stop (e.g., waiting for more data).
        """
        pass


class BaseRecognizer(ABC):

    def __init__(self) -> None:
        super().__init__()

    @abstractmethod
    def recognize(self, all_data, current_packet_data, force_predict) -> (bool, npt.NDArray[Any], int):
        pass

    #return position and event value
    def trial_start_position(self, data_packet:StreamFrame, event_set=None) -> (int, int):
        data = data_packet.data
        event_data = data[-1, :]
        for index, event in enumerate(event_data):
            # found event trigger
            if event > 0:
                started = True
                return index, event
        return -1, 0

    #return position and event value
    def trial_end_position(self, data_packet:StreamFrame, event_set=None) -> (int, int):
        return -1, 0

