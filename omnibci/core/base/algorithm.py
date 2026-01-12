#!/usr/bin/env python
# -*- coding: utf-8 -*-
# @Author   : lincker <haolin.d.wu@gmail.com>
# @Date     : 2025-04-25 10:53
from abc import ABC, abstractmethod
from typing import Any, Dict, Type

from numpy import typing as npt

from omnibci.core.data_model import BCIStreamFrame
from omnibci.core.base.registry import Registerable

RECOGNIZER_REGISTRY: Dict[str, Type['RecognizerBase']] = {}

class ProcessorBase(Registerable, ABC):
    @abstractmethod
    def process(self, data: npt.NDArray[Any]) -> npt.NDArray[Any]:
        """
        Takes data, transforms it, and returns it for the next step.
        Returns None if processing should stop (e.g., waiting for more data).
        """
        pass


class RecognizerBase(Registerable, ABC):
    _target_registry = RECOGNIZER_REGISTRY

    def __init__(self):
        super().__init__()

    @abstractmethod
    # return value: is recognized, event ID
    def recognize(self, all_data, current_packet_data, force_predict) -> tuple[bool, int]:
        pass

    def trial_start_position(self, data_packet:BCIStreamFrame, event_set=None) -> tuple[int, int]:
        data = data_packet.data
        event_data = data[-1, :]
        for index, event in enumerate(event_data):
            # found event trigger
            if event > 0:
                started = True
                return index, event
        return -1, 0

    #return position and event value, if return -1 means not found
    def trial_end_position(self, data_packet:BCIStreamFrame, event_set=None) -> tuple[int, int]:
        return -1, 0

