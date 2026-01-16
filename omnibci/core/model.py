#!/usr/bin/env python
# -*- coding: utf-8 -*-
# @Author   : lincker <haolin.d.wu@gmail.com>
# @Date     : 2025-04-18 10:41
from dataclasses import dataclass

import numpy as np
from numpy import typing as npt



@dataclass
class SignalChunk:
    """统一数据包格式"""
    # data include: n-1 channels data and the n channel is event trigger
    timestamp: float
    sequence_id: int
    raw_data: npt.NDArray
    trigger_indices: tuple = (-1,)

    @property
    def data(self) -> npt.NDArray:
        # 注意：为了性能，Trigger 总是固定在最后一个通道
        return self.raw_data[0:-1, :]

    @property
    def triggers(self) -> npt.NDArray:
        return self.raw_data[-1, :]

    @property
    def has_trigger_signal(self) -> bool:
        """快速检查当前块里是否有非零的 Trigger 信号"""
        # 假设 Trigger 通道平时是 0，有事发生时变高
        return np.any(self.triggers != 0)