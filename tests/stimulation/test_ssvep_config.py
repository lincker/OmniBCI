"""Tests for SSVEP paradigm configuration and target brightness."""
from __future__ import annotations

import math

import numpy as np

from omnibci.infrastructure.stimulation.creator.stim_config import SSVEPConfig
from omnibci.infrastructure.stimulation.creator.ssvep_target_rect import (
    StimTargetRect,
)


class TestSSVEPConfigInvariants:
    def test_layout_is_5x8(self):
        assert SSVEPConfig().layout == (5, 8)

    def test_resolution_default(self):
        assert SSVEPConfig().resolution == (1920, 1080)

    def test_refresh_rate_and_stim_time(self):
        c = SSVEPConfig()
        assert c.refresh_rate == 60
        assert c.stim_time == 3

    def test_forty_frequencies_linspace_8_to_15_8(self):
        f = SSVEPConfig().frequency
        assert len(f) == 40
        assert math.isclose(float(f[0]), 8.0, abs_tol=1e-9)
        assert math.isclose(float(f[-1]), 15.8, abs_tol=1e-9)
        # uniform spacing of 0.2 Hz
        diffs = np.diff(f)
        assert np.allclose(diffs, 0.2)

    def test_phase_table_length_and_period(self):
        p = SSVEPConfig().phase
        assert len(p) == 40
        # pattern tile of [0, 0.5π, π, 1.5π] repeated 10×
        expected_first4 = np.arange(0, 2, 0.5) * math.pi
        assert np.allclose(p[:4], expected_first4)

    def test_displayed_char_count_is_40(self):
        assert len(SSVEPConfig().displayed_char) == 40

    def test_displayed_char_starts_with_alphabet(self):
        assert SSVEPConfig().displayed_char[:3] == ["A", "B", "C"]

    def test_displayed_char_contains_control_keys(self):
        chars = SSVEPConfig().displayed_char
        assert "Del" in chars
        assert "." in chars

    def test_target_geometry_defaults(self):
        c = SSVEPConfig()
        assert c.target_width == 140
        assert c.target_height == 140
        assert c.interval == 50
        assert (c.init_target_x, c.init_target_y) == (225, 90)

    def test_paradigm_tag(self):
        assert SSVEPConfig().paradigm == "ssvep"


class TestSSVEPBrightnessContract:
    """Exercise the production brightness calculation directly."""

    @staticmethod
    def _target(frequency=10.0, fs=60, start_phase=0.0):
        return StimTargetRect(
            rectINX=0,
            site_point=(0, 0),
            rect_size=(140, 140),
            fs=fs,
            frequency=frequency,
            start_phase=start_phase,
            max_amplitude=1.0,
        )

    def test_brightness_bounds(self):
        target = self._target()
        for frame in range(0, 120):
            b = target.cal_brightness(frame)
            assert 0.0 <= b <= 1.0

    def test_brightness_at_frame_zero_with_zero_phase(self):
        assert math.isclose(
            self._target().cal_brightness(0), 0.5, abs_tol=1e-12
        )

    def test_brightness_phase_shift(self):
        # phase = π/2 → sin starts at 1 → brightness = 1.0 at frame 0
        assert math.isclose(
            self._target(start_phase=math.pi / 2).cal_brightness(0),
            1.0,
            abs_tol=1e-12,
        )
