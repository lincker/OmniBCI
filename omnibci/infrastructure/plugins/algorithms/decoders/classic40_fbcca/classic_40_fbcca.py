#!/usr/bin/env python
# -*- coding: utf-8 -*-
# @Author   : lincker <haolin.d.wu@gmail.com>
# @Date     : 2025-11-12 10:47
from pathlib import Path
from collections.abc import Mapping
import math
from typing import Optional

import numpy as np
from numpy import typing as npt
from scipy.signal import cheby1, filtfilt

from omnibci.sdk import Decoder, Prediction, SignalInfo


class Classic40FBCCA(Decoder):
    def __init__(
        self, *, parameters: Mapping[str, object], resource_root: Path,
        input_signal: SignalInfo,
    ) -> None:
        super().__init__()
        self.FREQUENCY_SET = [8.0, 9.0, 10.0, 11.0, 12.0, 13.0, 14.0, 15.0,
                              8.2, 9.2, 10.2, 11.2, 12.2, 13.2, 14.2, 15.2,
                              8.4, 9.4, 10.4, 11.4, 12.4, 13.4, 14.4, 15.4,
                              8.6, 9.6, 10.6, 11.6, 12.6, 13.6, 14.6, 15.6,
                              8.8, 9.8, 10.8, 11.8, 12.8, 13.8, 14.8, 15.8]
        if not isinstance(input_signal, SignalInfo):
            raise ValueError("Classic40FBCCA requires input_signal.sample_rate")
        self.SAMPLE_RATE = input_signal.sample_rate
        window_duration = parameters.get('window_duration', 3.0)
        offset_time = parameters.get('offset_time', 0.14)
        self.SAMPLE_COUNT = int(window_duration * self.SAMPLE_RATE)
        if self.SAMPLE_COUNT < 2:
            raise ValueError("Decode window must contain at least two samples")
        self.OFFSET_LENGTH = math.floor(offset_time * self.SAMPLE_RATE)
        self.num_harmonics = 5
        self.num_bands = 3
        self.weights = self._filter_bank = None

    def setup(self) -> None:
        # FBCCA weights: w_n = n^(-a) + b, with a=1.25 and b=0.25.
        a = 1.25
        b = 0.25
        # Precompute the filter-bank weights.
        self.weights = np.array([(n + 1) ** (-a) + b for n in range(self.num_bands)])
        self._filter_bank = self.get_filter_bank()

    def begin_epoch(self) -> None:
        # Whole-window classification has no accumulated trial state.
        pass

    def teardown(self) -> None:
        self.weights = self._filter_bank = None

    def get_reference_signals(self, data_len, target_freq):
        """Generate sine/cosine reference signals."""
        t = np.arange(data_len) / self.SAMPLE_RATE
        reference_signals = []
        for i in range(1, self.num_harmonics + 1):
            reference_signals.append(np.sin(2 * np.pi * i * target_freq * t))
            reference_signals.append(np.cos(2 * np.pi * i * target_freq * t))
        return np.array(reference_signals)  # Shape: (2*harmonics, samples)

    def get_filter_bank(self):
        """
        Generate filter-bank coefficients.
        The filter-bank passbands are:
        Band 1: 8-88 Hz
        Band 2: 16-88 Hz
        Band 3: 24-88 Hz
        ...
        """
        nyq = 0.5 * self.SAMPLE_RATE
        filters = []

        # Keep the high cutoff below Nyquist and at most 88 Hz.
        high_cut = min(88, nyq - 1)

        for i in range(self.num_bands):
            low_cut = 8 * (i + 1)  # 8Hz, 16Hz, 24Hz...
            if low_cut >= high_cut:
                break

            # Use a fourth-order Chebyshev I filter with 0.5 dB ripple.
            b, a = cheby1(N=4, rp=0.5, Wn=[low_cut / nyq, high_cut / nyq], btype='bandpass')
            filters.append((b, a))

        return filters

    def cca_coef(self, X, Y):
        """
        Compute CCA correlation using QR decomposition.
        X: EEG data (Channels, Samples)
        Y: Reference signals (2*Harmonics, Samples)
        """
        # Transpose to (samples, features).
        X = X.T
        Y = Y.T

        # Center the data.
        X = X - X.mean(axis=0)
        Y = Y - Y.mean(axis=0)

        # QR decomposition.
        Q_x, R_x = np.linalg.qr(X, mode='reduced')
        Q_y, R_y = np.linalg.qr(Y, mode='reduced')

        # SVD of Q_x.T @ Q_y
        U, S, V = np.linalg.svd(Q_x.T @ Q_y)

        # Return the largest canonical correlation.
        return S[0]

    def should_decode(self, window: npt.NDArray) -> bool:
        return window.shape[1] > self.SAMPLE_COUNT + self.OFFSET_LENGTH

    def decode(
        self,
        filtered_window: npt.NDArray,
        *, latest_raw_segment: npt.NDArray,
    ) -> Optional[Prediction]:
        calc_data = filtered_window[
            :,
            self.OFFSET_LENGTH:int(self.SAMPLE_COUNT + self.OFFSET_LENGTH),
        ]
        result = self.classify(calc_data, self.FREQUENCY_SET)
        if result is None:
            return None
        predict_index, _, _ = result
        return Prediction(target_id=int(predict_index))

    def classify(self, eeg_data, freq_list):
        """Return the target, frequency and scores, or None when filtering needs more samples."""
        n_samples = eeg_data.shape[1]
        filters = self._filter_bank

        # Store the final score for each frequency.
        scores = np.zeros(len(freq_list))

        # Split EEG into filter-bank bands.
        filtered_data = []
        for b, a in filters:
            padlen = 3 * max(len(a), len(b))
            if n_samples <= padlen:
                return None
            # Zero-phase filtering keeps the signal aligned with its references.
            filtered_data.append(filtfilt(b, a, eeg_data, axis=1, padlen=padlen))

        # Evaluate each target frequency.
        for freq_idx, freq in enumerate(freq_list):
            # Generate the reference signals for this frequency.
            Y = self.get_reference_signals(n_samples, freq)

            rho_sum = 0

            # Accumulate the weighted correlation from each band.
            for band_idx in range(len(filters)):
                X_sb = filtered_data[band_idx]

                # Calculate the canonical correlation coefficient.
                rho = self.cca_coef(X_sb, Y)

                # Weighted sum: sum(w_n * rho_n^2).
                rho_sum += self.weights[band_idx] * (rho ** 2)

            scores[freq_idx] = rho_sum

        # Select the highest-scoring frequency.
        result = np.argmax(scores)
        pred_idx = result + 1
        return pred_idx, freq_list[result], scores
