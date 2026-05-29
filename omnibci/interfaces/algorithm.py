#!/usr/bin/env python
# -*- coding: utf-8 -*-
# @Author   : lincker <haolin.d.wu@gmail.com>
# @Date     : 2025-04-25 10:53
from abc import ABC, abstractmethod
from typing import Dict, Type, Tuple, ClassVar


from omnibci.core.data.types import SignalChunk
from omnibci.core.utils.registry import Registerable

# class FilterBase(Registerable, ABC):
#     @abstractmethod
#     def process(self, data: npt.NDArray[Any]) -> npt.NDArray[Any]:
#         """
#         Takes data, transforms it, and returns it for the next step.
#         Returns None if processing should stop (e.g., waiting for more data).
#         """
#         pass


class DecoderBase(Registerable, ABC):
    _target_registry: ClassVar[Dict[str, Type['DecoderBase']]] = {}


    def __init__(self):
        super().__init__()

    @abstractmethod
    # return value: is recognized, events ID
    def analyse(self, all_data, current_packet_data, force_predict) -> Tuple[bool, int]:
        pass


class EpochDetectorBase(Registerable, ABC):
    """detect epoch start and end."""
    _target_registry: ClassVar[Dict[str, Type['EpochDetectorBase']]] = {}

    """return（index, event_id). If not found, return（-1，0）"""
    @abstractmethod
    def detect_start(self, chunk: SignalChunk, start_event_set=None) -> Tuple[int, int]:
        pass

    """return（index, event_id). If not found, return（-1，0）"""
    @abstractmethod
    def detect_end(self, chunk: SignalChunk, end_event_set=None) -> Tuple[int, int]:
        pass
