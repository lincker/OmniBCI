#!/usr/bin/env python
# -*- coding: utf-8 -*-
# @Author   : lincker <haolin.d.wu@gmail.com>
# @Date     : 2026-01-15 13:59
from pathlib import Path
from collections.abc import Mapping
import numpy as np

from omnibci.domain.signal_info import SignalInfo
from omnibci.domain.ports.algorithm import EpochDetector, EpochBoundary


class DefaultEpochDetector(EpochDetector):
    plugin_type = 'DEFAULT_EPOCH_DETECTOR'
    def __init__(
        self, *, parameters: Mapping[str, object], resource_root: Path,
        input_signal: SignalInfo,
    ) -> None:
        pass

    def detect(self, raw_data: np.ndarray) -> tuple[EpochBoundary, ...]:
        events = raw_data[-1]
        positions = np.nonzero(events)[0]
        if not positions.size:
            return ()
        return tuple(EpochBoundary(offset=int(index), kind="start", event_id=int(events[index]))
                     for index in positions)
