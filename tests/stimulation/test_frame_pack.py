"""Verify that the bundled stimulus asset can be consumed without source PNGs."""
from __future__ import annotations

from importlib import resources

import numpy as np
import pytest

from omnibci.infrastructure.plugins.paradigms.classic40SSVEP.frame_pack import FramePack


@pytest.mark.integration
def test_bundled_frame_pack_is_complete_and_readable():
    resource = resources.files(
        "omnibci.infrastructure.plugins.paradigms.classic40SSVEP"
    ).joinpath("resource", "classic40_ssvep.ofp")

    with resources.as_file(resource) as path, FramePack(path) as pack:
        groups = (
            ((pack.idle_frame,), 1, 1),
            (pack.iter_stimulation_frames(), 240, 1),
            (pack.iter_cue_frames(), 40, 3),
            (pack.iter_result_frames(), 40, 3),
        )
        for frames, expected_count, channels in groups:
            count = 0
            for frame in frames:
                # Reading through FramePack also verifies each frame's SHA-256.
                assert frame.shape == (990, 1920, channels)
                assert frame.dtype == np.uint8
                count += 1
            assert count == expected_count


@pytest.mark.integration
def test_bundled_cues_have_red_outlines_and_preserve_idle_pixels():
    resource = resources.files(
        "omnibci.infrastructure.plugins.paradigms.classic40SSVEP"
    ).joinpath("resource", "classic40_ssvep.ofp")

    with resources.as_file(resource) as path, FramePack(path) as pack:
        idle = np.repeat(pack.idle_frame, 3, axis=2)
        for cue, result in zip(pack.iter_cue_frames(), pack.iter_result_frames(), strict=True):
            red = (result[:, :, 0] > 200) & (result[:, :, 1] < 50) & (result[:, :, 2] < 50)
            ys, xs = np.nonzero(red)
            assert len(xs) > 0
            x0, x1 = xs.min(), xs.max() + 1
            y0, y1 = ys.min(), ys.max() + 1
            # Result assets must contain a filled target, allowing for its glyph.
            assert len(xs) > 0.9 * (x1 - x0) * (y1 - y0)

            border = np.zeros(cue.shape[:2], dtype=bool)
            border[y0:y1, x0:x1] = True
            border[y0 + 4:y1 - 4, x0 + 4:x1 - 4] = False
            assert np.all(cue[border] == (255, 0, 0))
            np.testing.assert_array_equal(cue[~border], idle[~border])
