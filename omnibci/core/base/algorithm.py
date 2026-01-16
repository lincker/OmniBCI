#!/usr/bin/env python
# -*- coding: utf-8 -*-
# @Author   : lincker <haolin.d.wu@gmail.com>
# @Date     : 2025-04-25 10:53
from abc import ABC, abstractmethod
from typing import Dict, Type, Tuple


from core.model import SignalChunk
from omnibci.core.base.registry import Registerable

ALGORITHM_REGISTRY: Dict[str, Type['AlgorithmBase']] = {}
EPOCH_STRATEGY_REGISTRY: Dict[str, Type['EpochStrategyBase']] = {}

# class ProcessingUnitBase(Registerable, ABC):
#     @abstractmethod
#     def process(self, data: npt.NDArray[Any]) -> npt.NDArray[Any]:
#         """
#         Takes data, transforms it, and returns it for the next step.
#         Returns None if processing should stop (e.g., waiting for more data).
#         """
#         pass


class AlgorithmBase(Registerable, ABC):
    _target_registry = ALGORITHM_REGISTRY

    def __init__(self):
        super().__init__()

    @abstractmethod
    # return value: is recognized, event ID
    def analyse(self, all_data, current_packet_data, force_predict) -> Tuple[bool, int]:
        pass


class EpochStrategyBase(Registerable, ABC):
    """detect epoch start and end"""
    _target_registry = EPOCH_STRATEGY_REGISTRY

    """return（index, event_id). If not found, return（-1，0）"""
    @abstractmethod
    def detect_start(self, chunk: SignalChunk, start_event_set=None) -> Tuple[int, int]:
        pass

    """return（index, event_id). If not found, return（-1，0）"""
    @abstractmethod
    def detect_end(self, chunk: SignalChunk, end_event_set=None) -> Tuple[int, int]:
        pass

    @abstractmethod
    def allowed_algorithm_analyse(self) -> bool:
        pass