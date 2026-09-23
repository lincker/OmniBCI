#!/usr/bin/env python
# -*- coding: utf-8 -*-
# @Author   : lincker <haolin.d.wu@gmail.com>
"""
BaselineCorrection — pre-trigger baseline removal Filter.

Standard EEG/ERP baseline correction: subtract, per channel, the mean amplitude
of the pre-trigger ``baseline`` segment from every sample of the analysis
``window``.  This removes slow DC drift / per-trial offset so decoders compare
against a common zero.

Contract notes:
  - The pre-trigger segment is *not* part of the main data stream — it arrives
    via ``FilterContext.baseline`` (channels × n).  This filter reads it from
    there and returns the corrected window only, so downstream stages and the
    decoder keep seeing the window exactly as before, just baseline-corrected.
  - No-op (returns the window unchanged) when no baseline was captured
    (``baseline is None`` or zero-width), e.g. when ``baseline_duration == 0`` or
    on a reopened epoch whose inter-trial gap yielded no pre-trigger samples.
  - Pure transform: no per-epoch state, safe to share across epochs.
"""

from numpy import typing as npt

from omnibci.domain.ports.algorithm import Filter, FilterContext
from omnibci.infrastructure.plugins.registry import register_plugin


@register_plugin(Filter)
class BaselineCorrection(Filter):
    plugin_type = "BASELINE_CORRECTION"

    def apply(self, data: npt.NDArray, ctx: FilterContext) -> npt.NDArray:
        baseline = ctx.baseline
        if baseline is None or baseline.shape[1] == 0:
            return data
        # Per-channel mean over the baseline samples → (channels, 1), broadcast.
        baseline_mean = baseline.mean(axis=1, keepdims=True)
        return data - baseline_mean
