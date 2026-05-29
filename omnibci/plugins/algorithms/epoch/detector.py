#!/usr/bin/env python
# -*- coding: utf-8 -*-
# @Author   : lincker <haolin.d.wu@gmail.com>
# @Date     : 2026-01-15 13:59
import numpy as np

from omnibci.core.data.types import SignalChunk
from omnibci.interfaces.algorithm import EpochDetectorBase


class EpochDetector(EpochDetectorBase):
    plugin_type = 'DEFAULT_EPOCH_DETECTOR'
    def __init__(self):
        pass

    def detect_start(self, chunk: SignalChunk, start_event_set=None) -> tuple[int, int]:
        triggers = chunk.triggers.flatten()
        nonzero = np.nonzero(triggers)[0]
        if len(nonzero) > 0:
            return int(nonzero[0]), int(triggers[nonzero[0]])
        return -1, 0


    def detect_end(self, chunk: SignalChunk, end_event_set=None) -> tuple[int, int]:
        return -1, 0

    def allowed_decoder_analyse(self) -> bool:
        return True
