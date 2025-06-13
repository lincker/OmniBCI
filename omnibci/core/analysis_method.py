#!/usr/bin/env python
# -*- coding: utf-8 -*-
# @Author   : lincker <haolin.d.wu@gmail.com>
# @Date     : 2025-04-25 10:53
from typing import Any
from numpy import typing as npt
from abc import ABC, abstractmethod
from .data_model import DataPacket


class AnalysisMethod(ABC):

    def __init__(self) -> None:
        super().__init__()

    # is start: bool, data: numpy array, event position: int
    def check_event(self, data_packet: DataPacket) -> (bool, int, int):
        data = data_packet.data
        event_data = data[-1, :]
        for index, event in enumerate(event_data):
            # found event trigger
            if event > 0:
                started = True
                return index, event
        # not found event trigger
        return -1, 0

    @abstractmethod
    def recognize(self, data) -> (bool, npt.NDArray[Any], int):
        pass

