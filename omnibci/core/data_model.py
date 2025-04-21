#!/usr/bin/env python
# -*- coding: utf-8 -*-
# @Author   : lincker <haolin.d.wu@gmail.com>
# @Date     : 2025-04-18 10:41
from dataclasses import dataclass
from numpy import typing as npt
from enum import Enum


class DataQualityFlag(Enum):
    OK = 0b00000000
    SIGNAL_SATURATION = 0b00000001
    HIGH_NOISE = 0b00000010
    CHANNEL_DISCONNECTED = 0b00000100
    IMPEDANCE_WARNING = 0b00001000
    TIMESTAMP_JITTER = 0b00010000
    CRC_ERROR = 0b00100000
    CUSTOM_FLAG_1 = 0b01000000
    CUSTOM_FLAG_2 = 0b10000000

    @classmethod
    def describe(cls, flag: int) -> str:
        descriptions = [
            member.name.replace('_', ' ').lower()
            for member in cls
            if flag & member.value and member != cls.OK
        ]
        return '|'.join(descriptions) if descriptions else 'ok'


@dataclass
class DataPacket:
    """统一数据包格式"""
    # data include: n-1 channels data and the n channel is event
    timestamp: float
    data: npt.NDArray
    sequence_id: int
    flags: int = DataQualityFlag.OK.value