'''
Author: lincker
Date: 2025-04-18 10:41:35
LastEditors: lincker haolin.d.wu@gmail.com
LastEditTime: 2025-12-25 10:54:43
FilePath: /OmniBCI/omnibci/core/data_model.py
Description:

Copyright (c) 2025 by lincker, All Rights Reserved.
'''
#!/usr/bin/env python
# -*- coding: utf-8 -*-
# @Author   : lincker <haolin.d.wu@gmail.com>
# @Date     : 2025-04-18 10:41
from dataclasses import dataclass
from enum import Enum

from numpy import typing as npt


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
class BCIStreamFrame:
    """统一数据包格式"""
    # data include: n-1 channels data and the n channel is event
    timestamp: float
    data: npt.NDArray
    sequence_id: int
    flags: int = DataQualityFlag.OK.value