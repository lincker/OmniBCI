'''
Author: lincker
Date: 2026-01-08 14:52:39
LastEditors: lincker haolin.d.wu@gmail.com
LastEditTime: 2026-04-23 11:36:41
FilePath: /OmniBCI/omnibci/interfaces/acquisition.py
Description:

Copyright (c) 2026 by lincker, All Rights Reserved.
'''
#!/usr/bin/env python
# -*- coding: utf-8 -*-
# @Author   : lincker <haolin.d.wu@gmail.com>
# @Date     : 2025-12-31 10:34
from abc import ABC, abstractmethod
from typing import Any, ClassVar, Dict, Type

from numpy import typing as npt

from omnibci.core.utils.registry import Registerable


class DataSourceBase(Registerable, ABC):
    """
    Abstract utils class for data source.

    This class defines the interface for data source, which are responsible for
    reading data from a source and providing it in a standardized format.
    """
    _target_registry: ClassVar[Dict[str, Type['DataSourceBase']]] = {}
    def __init__(self):
        super().__init__()
        self._is_connected = False
        self._is_streaming = False


    @abstractmethod
    async def get_metadata(self):
        """获取设备元数据"""

    @abstractmethod
    async def connect(self) -> bool:
        """建立设备/文件连接"""

    @abstractmethod
    async def start_stream(self) -> bool:
        """启动读取数据流"""

    @abstractmethod
    async def read_chunk(self)-> npt.NDArray[Any]:
        """从文件读取一个固定尺寸的数据，或从网络读取一个数据包"""

    @abstractmethod
    async def stop_stream(self) -> bool:
        """停止读取数据流"""

    @abstractmethod
    async def disconnect(self) -> bool:
        """断开设备/文件连接"""


class TriggerBase(Registerable, ABC):
    _target_registry: ClassVar[Dict[str, Type['TriggerBase']]] = {}
    @abstractmethod
    def send(self, event_id: int) -> bool:
        """发送trigger/event"""

    @abstractmethod
    def clear(self) -> None:
        """清除触发器状态"""
