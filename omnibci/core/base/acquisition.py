#!/usr/bin/env python
# -*- coding: utf-8 -*-
# @Author   : lincker <haolin.d.wu@gmail.com>
# @Date     : 2025-12-31 10:34
from abc import ABC, abstractmethod
from typing import Any, Dict, Type

from numpy import typing as npt

from .registry import Registerable


READER_REGISTRY: Dict[str, Type['ReaderBase']] = {}
TRIGGER_REGISTRY: Dict[str, Type['TriggerBase']] = {}

class ReaderBase(Registerable,ABC):
    """
    Abstract base class for data readers.

    This class defines the interface for data readers, which are responsible for
    reading data from a source and providing it in a standardized format.
    """
    _target_registry = READER_REGISTRY
    def __init__(self):
        super().__init__()
        self._is_open = False
        self._is_streaming = False


    @abstractmethod
    async def get_metadata(self):
        """获取设备元数据"""

    @abstractmethod
    async def connect(self) -> bool:
        """建立设备/文件连接"""

    @abstractmethod
    async def start_read(self) -> bool:
        """启动读取数据流"""

    @abstractmethod
    async def read_unit(self)-> npt.NDArray[Any]:
        """从文件读取一个固定尺寸的数据，或从网络读取一个数据包"""

    @abstractmethod
    async def stop_read(self) -> bool:
        """停止读取数据流"""

    @abstractmethod
    async def disconnect(self) -> bool:
        """断开设备/文件连接"""


class TriggerBase(Registerable, ABC):
    _target_registry = TRIGGER_REGISTRY
    @abstractmethod
    def send(self, event_id) -> bool:
        """发送trigger/event"""

    @abstractmethod
    def clear(self) -> None:
        """清除触发器状态"""
