"""
Characterization tests — Stimulation 限界上下文.

刺激上下文的渲染路径依赖 OpenGL/GLFW（需窗口/显卡），不适合在 CI/无头
环境断言；这里把可稳定固化的 **范式配置 (SSVEPConfig)** 作为接缝，锁定
classic40SSVEP 范式的关键参数与字符布局。

重构（尤其把 stimulation/ 迁入 infrastructure/）后，只要本文件全绿，即可
确信刺激范式的对外契约（频率表、相位表、字符排布、目标几何）未漂移。

注意：omnibci/stimulation/creator/ssvep_target_rect.py 当前含一处可疑的
裸导入 `from stimulation.ui.view import View`（缺少 omnibci. 前缀），在多数
环境下无法直接导入该模块。这正是特征测试网应当在重构时暴露的问题之一，
已在 README 中记录；此处刻意不依赖该模块。
"""
from __future__ import annotations

import math

import numpy as np

from omnibci.infrastructure.stimulation.creator.stim_config import SSVEPConfig


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
    """
    刺激亮度的数学契约（与 StimTargetRect.cal_brightness 一致）：
        brightness(frame) = 0.5 + 0.5 * sin(2π·f/fs·frame + phase)
    由于 ssvep_target_rect 模块当前存在裸导入问题（见模块 docstring），
    这里以独立实现固化该不变量，作为重构修复该导入后比对真实实现的基准。
    """

    @staticmethod
    def _brightness(frame_no, frequency, fs, start_phase):
        return 0.5 + 0.5 * math.sin(2 * math.pi * frequency / fs * frame_no + start_phase)

    def test_brightness_bounds(self):
        for frame in range(0, 120):
            b = self._brightness(frame, frequency=10.0, fs=60, start_phase=0.0)
            assert 0.0 <= b <= 1.0

    def test_brightness_at_frame_zero_with_zero_phase(self):
        assert math.isclose(self._brightness(0, 10.0, 60, 0.0), 0.5, abs_tol=1e-12)

    def test_brightness_phase_shift(self):
        # phase = π/2 → sin starts at 1 → brightness = 1.0 at frame 0
        assert math.isclose(
            self._brightness(0, 10.0, 60, math.pi / 2), 1.0, abs_tol=1e-12
        )
