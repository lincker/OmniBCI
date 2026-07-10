"""
Unit tests for Classic40SSVEP_2 — state machine, texture lifecycle, feedback parsing.

All OpenGL / filesystem dependencies are mocked:
  - _load_rgba   : returns a fixed 10×10 RGBA array
  - os.listdir   : returns deterministic file lists
  - renderer     : MagicMock with upload_texture → unique TextureHandle
  - trigger      : MagicMock
"""
from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from omnibci.infrastructure.plugins.paradigms.classic40SSVEP.ssvep import (
    Classic40SSVEP_2,
    _CUE_FRAMES,
    _RESULT_FRAMES,
    _State,
)
from omnibci.infrastructure.stimulation.ports.renderer import TextureHandle

# ---------------------------------------------------------------------------
# Constants matching the fake filesystem
# ---------------------------------------------------------------------------
_N_STIM_FRAMES = 5
_N_CUE_FRAMES  = 3
# total uploads: 1 init + _N_STIM_FRAMES stim + _N_CUE_FRAMES cue + 1 text
_TOTAL_UPLOADS = 1 + _N_STIM_FRAMES + _N_CUE_FRAMES + 1

_FAKE_RGBA = np.zeros((10, 10, 4), dtype=np.uint8)

_SSVEP_FILES = ["initial_frame.png"] + [f"{i}.png" for i in range(_N_STIM_FRAMES)]
_CUE_FILES   = [f"{i}.png" for i in range(_N_CUE_FRAMES)]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def trigger():
    return MagicMock()


@pytest.fixture()
def renderer():
    r = MagicMock()
    _id = iter(range(1, 10_000))
    r.upload_texture.side_effect = lambda _: TextureHandle(id=next(_id))
    return r


@pytest.fixture()
def paradigm(trigger):
    return Classic40SSVEP_2(trigger)


@pytest.fixture(autouse=True)
def _mock_fs():
    """Patch filesystem + image loading so setup() needs no real files."""
    def _listdir(path):
        return _CUE_FILES if "ssvep_cue" in path else _SSVEP_FILES

    with patch(
        "omnibci.infrastructure.plugins.paradigms.classic40SSVEP.ssvep._load_rgba",
        return_value=_FAKE_RGBA,
    ), patch("os.listdir", side_effect=_listdir):
        yield


# ---------------------------------------------------------------------------
# Setup / teardown
# ---------------------------------------------------------------------------

class TestSetup:
    def test_none_renderer_is_noop(self, paradigm):
        paradigm.setup(None)
        assert paradigm._renderer is None

    def test_uploads_expected_texture_count(self, paradigm, renderer):
        paradigm.setup(renderer)
        assert renderer.upload_texture.call_count == _TOTAL_UPLOADS

    def test_initial_state_is_idle(self, paradigm, renderer):
        paradigm.setup(renderer)
        assert paradigm._state == _State.IDLE

    def test_stim_textures_populated(self, paradigm, renderer):
        paradigm.setup(renderer)
        assert len(paradigm._stim_textures) == _N_STIM_FRAMES

    def test_cue_textures_populated(self, paradigm, renderer):
        paradigm.setup(renderer)
        assert len(paradigm._cue_textures) == _N_CUE_FRAMES


class TestTeardown:
    def test_releases_all_uploaded_textures(self, paradigm, renderer):
        paradigm.setup(renderer)
        n = renderer.upload_texture.call_count
        paradigm.teardown(renderer)
        assert renderer.release_texture.call_count == n

    def test_clears_texture_lists(self, paradigm, renderer):
        paradigm.setup(renderer)
        paradigm.teardown(renderer)
        assert paradigm._stim_textures == []
        assert paradigm._cue_textures  == []

    def test_clears_renderer_reference(self, paradigm, renderer):
        paradigm.setup(renderer)
        paradigm.teardown(renderer)
        assert paradigm._renderer is None


# ---------------------------------------------------------------------------
# State machine — trial_stimulus_present
# ---------------------------------------------------------------------------

class TestTrialStimulusPresent:
    def test_valid_event_id_transitions_to_cue(self, paradigm, renderer):
        paradigm.setup(renderer)
        paradigm.trial_stimulus_present(1)          # 1-based: target 1 = 'A'
        assert paradigm._state == _State.CUE

    def test_zero_is_no_cue_goes_straight_to_stim(self, paradigm, renderer):
        paradigm.setup(renderer)
        paradigm.trial_stimulus_present(0)          # 0 = baseline / no-cue
        assert paradigm._state == _State.STIM

    def test_cue_countdown_set(self, paradigm, renderer):
        paradigm.setup(renderer)
        paradigm.trial_stimulus_present(2)
        assert paradigm._cue_countdown == _CUE_FRAMES

    def test_current_event_stored(self, paradigm, renderer):
        paradigm.setup(renderer)
        paradigm.trial_stimulus_present(7)
        assert paradigm._current_event == 7

    def test_invalid_event_id_goes_straight_to_stim(self, paradigm, renderer):
        paradigm.setup(renderer)
        paradigm.trial_stimulus_present(99)
        assert paradigm._state == _State.STIM

    def test_invalid_event_id_sends_trigger_immediately(self, paradigm, renderer, trigger):
        paradigm.setup(renderer)
        paradigm.trial_stimulus_present(99)
        trigger.send.assert_called_once_with(99)


# ---------------------------------------------------------------------------
# State machine — render_frame transitions
# ---------------------------------------------------------------------------

class TestRenderFrameTransitions:
    def _tick(self, paradigm, n=1):
        for _ in range(n):
            paradigm.render_frame(0.0)

    def test_cue_transitions_to_stim_after_countdown(self, paradigm, renderer, trigger):
        paradigm.setup(renderer)
        paradigm.trial_stimulus_present(1)
        self._tick(paradigm, _CUE_FRAMES)
        assert paradigm._state == _State.STIM
        trigger.send.assert_called_once_with(1)

    def test_stim_plays_all_frames(self, paradigm, renderer):
        paradigm.setup(renderer)
        paradigm.trial_stimulus_present(1)
        self._tick(paradigm, _CUE_FRAMES)          # CUE → STIM
        self._tick(paradigm, _N_STIM_FRAMES)       # play every frame
        assert paradigm._stim_frame_idx == _N_STIM_FRAMES

    def test_stim_transitions_to_idle_when_frames_exhausted(self, paradigm, renderer):
        paradigm.setup(renderer)
        paradigm.trial_stimulus_present(1)
        self._tick(paradigm, _CUE_FRAMES + _N_STIM_FRAMES + 1)
        assert paradigm._state == _State.IDLE

    def test_stim_clears_trigger_on_finish(self, paradigm, renderer, trigger):
        paradigm.setup(renderer)
        paradigm.trial_stimulus_present(1)
        self._tick(paradigm, _CUE_FRAMES + _N_STIM_FRAMES + 1)
        trigger.clear.assert_called()

    def test_result_transitions_to_idle_after_countdown(self, paradigm, renderer):
        paradigm.setup(renderer)
        paradigm.trial_result_present(1)            # 1-based: target 1 = 'A'
        assert paradigm._state == _State.RESULT
        self._tick(paradigm, _RESULT_FRAMES)
        assert paradigm._state == _State.IDLE


# ---------------------------------------------------------------------------
# trial_result_present
# ---------------------------------------------------------------------------

class TestTrialResultPresent:
    def test_transitions_to_result_state(self, paradigm, renderer):
        paradigm.setup(renderer)
        paradigm.trial_result_present(1)
        assert paradigm._state == _State.RESULT

    def test_result_countdown_set(self, paradigm, renderer):
        paradigm.setup(renderer)
        paradigm.trial_result_present(1)
        assert paradigm._result_countdown == _RESULT_FRAMES

    def test_add_text_accumulates(self, paradigm, renderer):
        paradigm.setup(renderer)
        paradigm.trial_result_present(1)   # 'A'
        paradigm.trial_result_present(2)   # 'B'
        assert paradigm._text_view._content == "AB"

    def test_del_removes_last_char(self, paradigm, renderer):
        paradigm.setup(renderer)
        paradigm.trial_result_present(1)   # 'A'
        paradigm.trial_result_present(40)  # 'Del'
        assert paradigm._text_view._content == ""


# ---------------------------------------------------------------------------
# _parse_feedback
# ---------------------------------------------------------------------------

class TestParseFeedback:
    def test_first_char_is_A(self, paradigm):
        assert paradigm._parse_feedback(1) == "A"        # 1-based: target 1 = 'A'

    def test_last_letter_is_Z(self, paradigm):
        assert paradigm._parse_feedback(26) == "Z"

    def test_first_digit_is_0(self, paradigm):
        assert paradigm._parse_feedback(27) == "0"

    def test_last_digit_is_9(self, paradigm):
        assert paradigm._parse_feedback(36) == "9"

    def test_del_is_last(self, paradigm):
        assert paradigm._parse_feedback(40) == "Del"     # target 40 = 'Del'

    def test_zero_raises(self, paradigm):
        # 0 is the baseline / no-target sentinel, not a valid feedback id.
        with pytest.raises(IndexError):
            paradigm._parse_feedback(0)

    def test_negative_raises(self, paradigm):
        with pytest.raises(IndexError):
            paradigm._parse_feedback(-1)

    def test_out_of_range_raises(self, paradigm):
        with pytest.raises(IndexError):
            paradigm._parse_feedback(41)


# ---------------------------------------------------------------------------
# render_frame — drawing calls
# ---------------------------------------------------------------------------

class TestRenderFrameDrawing:
    def test_idle_calls_clear_and_draw_texture(self, paradigm, renderer):
        paradigm.setup(renderer)
        renderer.reset_mock()
        paradigm.render_frame(0.0)
        renderer.clear.assert_called_once()
        renderer.draw_texture.assert_called()

    def test_render_frame_noop_without_renderer(self, paradigm):
        # should not raise
        paradigm.render_frame(0.0)
