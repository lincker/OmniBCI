"""Executable plugin tutorial; the sign of an EEG mean is not a BCI classifier."""
from __future__ import annotations

import math

import numpy as np
from numpy.typing import NDArray

from omnibci.sdk import Decoder, PluginContext, PluginParameters, Prediction


class OzPolarityDemo(Decoder):
    """Stateless whole-window demonstration of named, mapped channel input."""

    def __init__(self, *, parameters: PluginParameters, context: PluginContext) -> None:
        if context.input_signal is None:
            raise ValueError("Oz Polarity Demo requires prepared input metadata")
        # These names describe matrix rows after mapping and re-referencing.
        # context.channel_names describes the original source instead.
        names = [channel.name for channel in context.input_signal.channels]
        if "Oz" not in names:
            raise ValueError("Oz Polarity Demo requires an Oz input channel")
        self.oz_index = names.index("Oz")
        self.sample_count = max(
            2, math.ceil(parameters["window_duration"] * context.input_signal.sample_rate),
        )

    def should_decode(self, window: NDArray) -> bool:
        # Called before the ordered filters; keep this readiness check cheap.
        return window.shape[1] >= self.sample_count

    def decode(self, window: NDArray, latest_samples: NDArray) -> Prediction | None:
        # A forced end-of-epoch decode can bypass should_decode().
        if not self.should_decode(window):
            return None
        # Rows exclude trigger; whole-window decoders can ignore latest_samples.
        mean = float(np.mean(window[self.oz_index, :self.sample_count]))
        if not math.isfinite(mean):
            return None
        return Prediction(target_id=1 if mean >= 0 else 2)
