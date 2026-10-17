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
        # These names describe matrix rows after channel selection and ordering.
        # context.channel_names describes the original source instead.
        names = [channel.name for channel in context.input_signal.channels]
        if "Oz" not in names:
            raise ValueError("Oz Polarity Demo requires an Oz input channel")
        self.oz_index = names.index("Oz")
        self.sample_count = max(
            2, math.ceil(parameters["window_duration"] * context.input_signal.sample_rate),
        )

    def should_decode(self, window: NDArray) -> bool:
        # The host checks the accumulated post-trigger window before epoch filters.
        return window.shape[1] >= self.sample_count

    def decode(self, window: NDArray, latest_samples: NDArray) -> Prediction | None:
        # window is filtered post-trigger data accumulated so far, not necessarily
        # a complete trial. A closing boundary can bypass the readiness gate.
        if not self.should_decode(window):
            return None
        # latest_samples is required: the most recent mapped epoch
        # fragment, without epoch filters, or shape (channels, 0) when absent.
        # It is not all samples since the last decode. This stateless whole-window
        # decoder ignores it and never modifies either input array in place.
        mean = float(np.mean(window[self.oz_index, :self.sample_count]))
        if not math.isfinite(mean):
            return None
        # A Prediction ends collection of the current epoch.
        return Prediction(target_id=1 if mean >= 0 else 2)
