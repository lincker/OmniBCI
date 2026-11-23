"""Fixed-window FBCCA with rank-aware CCA and explicit abstention thresholds."""
from collections.abc import Mapping
import math
from numbers import Real
from pathlib import Path

import numpy as np
from numpy.typing import NDArray
from scipy.signal import cheby1, sosfiltfilt

from omnibci.sdk import Decoder, Prediction, SignalInfo
from .targets import REFRESH_RATE_HZ, STIM_FRAMES, TARGETS


_HARMONICS = 5
_BAND_LOW_HZ = (8, 16, 24)
_BAND_HIGH_HZ = 88
_WEIGHTS = np.arange(1, 4, dtype=float) ** -1.25 + .25
_WEIGHTS /= _WEIGHTS.sum()


def _number(parameters, name, default, minimum, maximum):
    value = parameters.get(name, default)
    if (isinstance(value, bool) or not isinstance(value, Real) or not math.isfinite(value)
            or not minimum <= value <= maximum):
        raise ValueError(f'{name} must be a finite number in {minimum}..{maximum}')
    return float(value)


def _reference_signals(target, sample_rate, sample_count, offset_samples):
    time = (np.arange(sample_count) + offset_samples) / sample_rate
    harmonics = np.arange(1, _HARMONICS + 1)[:, None]
    phase = harmonics * (2 * np.pi * target.frequency_hz * time + target.phase_rad)
    return np.stack((np.sin(phase), np.cos(phase)), axis=1).reshape(2 * _HARMONICS, sample_count)


def _basis(data):
    """Sample-space basis without arbitrary columns for flat/duplicate channels."""
    scale = np.max(np.abs(data), axis=1, keepdims=True)
    scaled = data / np.where(scale > 0, scale, 1)
    centered = (scaled - scaled.mean(axis=1, keepdims=True)).T
    u, singular, _ = np.linalg.svd(centered, full_matrices=False)
    tolerance = max(centered.shape) * np.finfo(float).eps * singular[0]
    return u[:, singular > tolerance]


class Command10FBCCA(Decoder):

    def __init__(self, *, parameters: Mapping[str, object], resource_root: Path,
                 input_signal: SignalInfo) -> None:
        if not isinstance(parameters, Mapping) or set(parameters) - {
            'window_duration', 'offset_time', 'min_score', 'min_margin',
        }:
            raise ValueError('Unknown Command10 FBCCA parameters')
        if not isinstance(input_signal, SignalInfo):
            raise ValueError('Command10 FBCCA requires prepared input_signal')
        if input_signal.sample_rate < 200:
            raise ValueError('Command10 FBCCA requires EEG sampling at 200 Hz or higher')
        if any(channel.signal_type != 'eeg' for channel in input_signal.channels):
            raise ValueError('Command10 FBCCA accepts mapped EEG channels only')
        self._sample_rate = input_signal.sample_rate
        self._channels = input_signal.signal_channel_count
        duration = _number(parameters, 'window_duration', 3.0, 1, 4)
        offset = _number(parameters, 'offset_time', .14, 0, 3)
        self._min_score = _number(parameters, 'min_score', .3, 0, 1)
        self._min_margin = _number(parameters, 'min_margin', .05, 0, 1)
        self._sample_count = math.ceil(duration * self._sample_rate)
        self._offset_samples = math.floor(offset * self._sample_rate)
        self._required_samples = self._offset_samples + self._sample_count
        if (offset + duration > STIM_FRAMES / REFRESH_RATE_HZ
                or self._required_samples > self._sample_rate * STIM_FRAMES // REFRESH_RATE_HZ):
            raise ValueError('Offset and decode window must fit inside the four-second stimulus')
        self._filters = self._references = None
        self._attempted = False

    def setup(self) -> None:
        if self._filters is not None:
            raise RuntimeError('Command10 FBCCA is already initialized')
        filters = tuple(cheby1(4, .5, [low, _BAND_HIGH_HZ], btype='bandpass',
                              fs=self._sample_rate, output='sos') for low in _BAND_LOW_HZ)
        references = tuple(_basis(_reference_signals(
            target, self._sample_rate, self._sample_count, self._offset_samples,
        )) for target in TARGETS)
        self._filters, self._references = filters, references
        self._attempted = False

    def _require_ready(self):
        if self._filters is None:
            raise RuntimeError('Command10 FBCCA requires successful setup')

    def begin_epoch(self) -> None:
        self._require_ready()
        self._attempted = False

    def teardown(self) -> None:
        self._filters = self._references = None
        self._attempted = False

    def _check_matrix(self, data):
        if (not isinstance(data, np.ndarray) or data.ndim != 2 or data.shape[0] != self._channels
                or data.dtype.kind not in 'fiu'):
            raise ValueError(f'Expected a real numeric matrix with {self._channels} EEG rows')

    def should_decode(self, raw_window: NDArray) -> bool:
        self._require_ready()
        self._check_matrix(raw_window)
        return not self._attempted and raw_window.shape[1] >= self._required_samples

    def decode(self, filtered_window: NDArray, *, latest_raw_segment: NDArray) -> Prediction | None:
        self._require_ready()
        self._check_matrix(filtered_window)
        self._check_matrix(latest_raw_segment)
        if not self.should_decode(filtered_window):
            return None
        # One fixed analysis window per epoch, including an abstained decision.
        self._attempted = True
        data = np.array(filtered_window[:, self._offset_samples:self._required_samples], dtype=float, copy=True)
        if not np.isfinite(data).all():
            return None
        # Normalize before filtering to avoid dependence on V/uV scale or large DC.
        scale = np.max(np.abs(data), axis=1, keepdims=True)
        data /= np.where(scale > 0, scale, 1)
        data -= data.mean(axis=1, keepdims=True)
        if not np.any(data):
            return None

        scores = np.zeros(len(TARGETS))
        try:
            for weight, coefficients in zip(_WEIGHTS, self._filters):
                band = sosfiltfilt(coefficients, data, axis=1)
                basis = _basis(band)
                if basis.shape[1] == 0:
                    continue
                for index, reference in enumerate(self._references):
                    rho = np.linalg.svd(basis.T @ reference, compute_uv=False)[0]
                    scores[index] += weight * min(float(rho), 1.0) ** 2
        except np.linalg.LinAlgError:
            return None
        if not np.isfinite(scores).all():
            return None
        ranking = np.argsort(scores)
        best, runner_up = scores[ranking[-1]], scores[ranking[-2]]
        if (best < self._min_score or best - runner_up < self._min_margin
                or np.isclose(best, runner_up, rtol=1e-9, atol=1e-12)):
            return None
        # The SDK confidence field carries similarity, not a calibrated probability.
        return Prediction(target_id=TARGETS[int(ranking[-1])].target_id, confidence=float(np.clip(best, 0, 1)))
