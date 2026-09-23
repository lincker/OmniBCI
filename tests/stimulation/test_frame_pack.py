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
        )
        for frames, expected_count, channels in groups:
            count = 0
            for frame in frames:
                # Reading through FramePack also verifies each frame's SHA-256.
                assert frame.shape == (990, 1920, channels)
                assert frame.dtype == np.uint8
                count += 1
            assert count == expected_count
