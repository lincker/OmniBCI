#!/usr/bin/env python
# -*- coding: utf-8 -*-
# @Author   : lincker <haolin.d.wu@gmail.com>
"""
BaselineCorrection — per-channel baseline offset correction Filter.

Standard EEG/ERP baseline correction: subtract, per channel, the mean amplitude
of all supplied pre-trigger reference samples from every sample of the analysis
``window``. This removes a constant per-channel offset relative to that
reference interval; it does not remove time-varying drift.

Contract notes:
  - The pre-trigger segment is *not* part of the main data stream — it arrives
    via ``pre_trigger_data`` (channels × n).  This filter reads it from
    there and returns the corrected window only, so downstream stages and the
    decoder keep seeing the window exactly as before, just baseline-corrected.
  - No-op (returns the window unchanged) when no pre-trigger data was captured
    (``pre_trigger_data is None`` or zero-width), e.g. when ``baseline_duration == 0`` or
    when the stream has not yet supplied any history. Captured history is
    continuous and may include samples from the previous trial.
  - Pure transform: no per-epoch state, safe to share across epochs.
"""

from pathlib import Path
from collections.abc import Mapping
from numpy import typing as npt

from omnibci.domain.signal_info import SignalInfo
from omnibci.domain.ports.algorithm import Filter


class BaselineCorrection(Filter):
    plugin_type = "BASELINE_CORRECTION"

    def __init__(
        self, *, parameters: Mapping[str, object], resource_root: Path,
        input_signal: SignalInfo,
    ) -> None:
        pass

    def apply(self, data: npt.NDArray, *, pre_trigger_data: npt.NDArray | None) -> npt.NDArray:
        if pre_trigger_data is None or pre_trigger_data.shape[1] == 0:
            return data
        # Use all supplied reference samples to estimate the per-channel baseline.
        baseline_mean = pre_trigger_data.mean(axis=1, keepdims=True)
        return data - baseline_mean
