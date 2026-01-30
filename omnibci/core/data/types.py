#!/usr/bin/env python
# -*- coding: utf-8 -*-
# @Author   : lincker <haolin.d.wu@gmail.com>
# @Date     : 2025-04-18 10:41
from dataclasses import dataclass

import numpy as np
from numpy import typing as npt



@dataclass
class SignalChunk:
    """unified data structure for signal chunks"""
    # raw data include: n-1 channels data and the n channel is events trigger
    timestamp: float
    sequence_id: int
    raw_data: npt.NDArray
    trigger_indices: tuple = (-1,)

    @property
    def data(self) -> npt.NDArray:
        # fix trigger at the last channel
        return self.raw_data[0:-1, :]

    @property
    def triggers(self) -> npt.NDArray:
        return self.raw_data[-1, :]

    @property
    def has_trigger_signal(self) -> bool:
        """fast check non zero trigger"""
        return np.any(self.triggers != 0)