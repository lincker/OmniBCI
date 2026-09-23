#!/usr/bin/env python
# -*- coding: utf-8 -*-
# @Author   : lincker <haolin.d.wu@gmail.com>
# @Date     : 2026-01-15 13:59
import numpy as np

from omnibci.domain.signal import SignalChunk
from omnibci.domain.ports.algorithm import EpochDetector
from omnibci.infrastructure.plugins.registry import register_plugin
from omnibci.sdk import PluginContext, PluginParameters


@register_plugin(EpochDetector)
class DefaultEpochDetector(EpochDetector):
    plugin_type = 'DEFAULT_EPOCH_DETECTOR'
    def __init__(
        self, *, parameters: PluginParameters | None = None,
        context: PluginContext | None = None,
    ):
        pass

    def detect_start(self, chunk: SignalChunk) -> tuple[int, int]:
        triggers = chunk.triggers.flatten()
        nonzero = np.nonzero(triggers)[0]
        if len(nonzero) > 0:
            return int(nonzero[0]), int(triggers[nonzero[0]])
        return -1, 0


    def detect_end(self, chunk: SignalChunk) -> tuple[int, int]:
        return -1, 0
