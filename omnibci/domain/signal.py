#!/usr/bin/env python
# -*- coding: utf-8 -*-
# @Author   : lincker <haolin.d.wu@gmail.com>
# @Date     : 2025-04-18 10:41
"""
SignalChunk — Domain Value Object (moved from shared/signal.py).

A raw slice of acquired signal — the upstream stage of the
SignalChunk -> Epoch processing chain (Epoch is downstream). It carries an
acquisition-domain convention: the last channel is the trigger channel, so
``data`` is the first n-1 rows and ``triggers`` is the last row. Because this
is domain vocabulary (the EpochDetector port references it in its signature),
it belongs in domain rather than the shared/ technical area. Depends only on
numpy and is safe to transport across an mp.Queue.
"""
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
        # bool(...): np.any returns np.bool_; collapse to a Python bool
        # to match the annotation.
        return bool(np.any(self.triggers != 0))
