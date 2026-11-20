#!/usr/bin/env python
# -*- coding: utf-8 -*-
# @Author   : lincker <haolin.d.wu@gmail.com>
# @Date     : 2026-01-15 13:59
import numpy as np

from omnibci.domain.ports.algorithm import EpochDetector, EpochBoundary
from omnibci.sdk import PluginContext, PluginParameters


class DefaultEpochDetector(EpochDetector):
    plugin_type = 'DEFAULT_EPOCH_DETECTOR'
    def __init__(
        self, *, parameters: PluginParameters | None = None,
        context: PluginContext | None = None,
    ):
        pass

    def detect(self, raw_data: np.ndarray) -> tuple[EpochBoundary, ...]:
        events = raw_data[-1]
        positions = np.nonzero(events)[0]
        if not positions.size:
            return ()
        return tuple(EpochBoundary(offset=int(index), kind="start", event_id=int(events[index]))
                     for index in positions)
