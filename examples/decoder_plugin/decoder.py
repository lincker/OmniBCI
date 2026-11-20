"""Executable plugin tutorial; the sign of an EEG mean is not a BCI classifier."""
from __future__ import annotations

import math
from collections.abc import Mapping
from pathlib import Path

import numpy as np
from numpy.typing import NDArray

from omnibci.sdk import Decoder, SignalInfo, Prediction


class OzPolarityDemo(Decoder):
    """Stateless whole-window demonstration of named, mapped channel input."""

    def __init__(self, *, parameters: Mapping[str, object], resource_root: Path, input_signal: SignalInfo) -> None:
        if not isinstance(input_signal, SignalInfo):
            raise ValueError("Oz Polarity Demo requires prepared input metadata")
        # These names describe matrix rows after channel selection and ordering.
        names = [channel.name for channel in input_signal.channels]
        if "Oz" not in names:
            raise ValueError("Oz Polarity Demo requires an Oz input channel")
        self.oz_index = names.index("Oz")
        self.sample_count = max(
            2, math.ceil(parameters["window_duration"] * input_signal.sample_rate),
        )

    def setup(self) -> None:
        pass

    def begin_epoch(self) -> None:
        pass

    def teardown(self) -> None:
        pass

    def should_decode(self, window: NDArray) -> bool:
        # The host checks the accumulated post-trigger window before epoch filters.
        return window.shape[1] >= self.sample_count

    def decode(self, window: NDArray, *, latest_raw_segment: NDArray) -> Prediction | None:
        # window is filtered post-trigger data accumulated so far, not necessarily
        # a complete trial. A closing boundary can bypass the readiness gate.
        if not self.should_decode(window):
            return None
        # latest_raw_segment is required: the most recent mapped epoch
        # fragment, without epoch filters, or shape (channels, 0) when absent.
        # It is not all samples since the last decode. This stateless whole-window
        # decoder ignores it and never modifies either input array in place.
        mean = float(np.mean(window[self.oz_index, :self.sample_count]))
        if not math.isfinite(mean):
            return None
        # A Prediction ends collection of the current epoch.
        return Prediction(target_id=1 if mean >= 0 else 2)
