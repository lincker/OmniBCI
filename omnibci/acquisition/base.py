#!/usr/bin/env python
# -*- coding: utf-8 -*-
# @Author   : lincker <haolin.d.wu@gmail.com>
# @Date     : 2025-04-18 10:40
from abc import ABC, abstractmethod
from datetime import datetime
from numpy import typing as npt
from typing import Any, TypedDict, NotRequired


# --------------------------
# 设备元数据定义
# --------------------------
class ChannelInfo(TypedDict):
    """单个通道的元数据"""
    name: str
    type: str
    unit: str
    sampling_rate: float
    range: tuple[float, float]
    position: NotRequired[tuple[float, float, float]]

class DeviceMeta(TypedDict):
    """设备级元数据（兼容动态字段）"""
    device_id: str
    manufacturer: str
    model: str
    firmware_version: str
    hardware_version: str
    channels: list[ChannelInfo]
    nominal_sampling_rate: float
    timestamp_reference: datetime
    calibration_data: NotRequired[npt.NDArray]
    extensions: NotRequired[dict]



class BaseReader(ABC):
    """
    Abstract base class for data readers.

    This class defines the interface for data readers, which are responsible for
    reading data from a source and providing it in a standardized format.
    """

    def __init__(self):
        super().__init__()
        self._is_connected = False
        self._is_streaming = False


    @abstractmethod
    async def get_metadata(self) -> DeviceMeta:
        """获取设备元数据"""

    @abstractmethod
    async def connect(self) -> bool:
        """建立设备/文件连接"""

    @abstractmethod
    async def start_stream(self) -> bool:
        """启动数据流"""

    @abstractmethod
    async def read_data(self)-> npt.NDArray[Any]:
        """读取单个数据包"""

    @abstractmethod
    async def stop_stream(self) -> bool:
        """停止数据流"""

    @abstractmethod
    async def disconnect(self) -> bool:
        """断开设备/文件连接"""


class BaseTrigger(ABC):
    pass