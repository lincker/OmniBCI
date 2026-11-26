"""
Unit tests for Classic40SSVEP_2 — state machine, texture lifecycle, feedback parsing.

All OpenGL / filesystem dependencies are mocked:
  - FramePack   : yields deterministic grayscale and RGB frames
  - pack path   : provides a path without accessing package resources
  - renderer     : MagicMock with upload_texture → unique TextureHandle
  - trigger      : MagicMock
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, call, patch

import numpy as np
import pytest

from omnibci.infrastructure.plugins.paradigms.classic40SSVEP.ssvep import (
    Classic40SSVEP_2,
    _CUE_FRAMES,
    _RESULT_FRAMES,
    _STIM_RECT,
    _State,
)
from omnibci.sdk import TextureHandle

# ---------------------------------------------------------------------------
# Constants matching the fake frame pack
# ---------------------------------------------------------------------------
_N_STIM_FRAMES = 5
_N_CUE_FRAMES  = 40
_N_RESULT_FRAMES = 40
# total uploads: 1 init + stimulation + cue + result + 1 text
_TOTAL_UPLOADS = 1 + _N_STIM_FRAMES + _N_CUE_FRAMES + _N_RESULT_FRAMES + 1

_IDLE_FRAME = np.zeros((10, 10, 1), dtype=np.uint8)
_STIM_FRAMES = [
    np.full((10, 10, 1), i + 1, dtype=np.uint8)
    for i in range(_N_STIM_FRAMES)
]
_CUE_FRAME_DATA = [
    np.full((10, 10, 3), i + 1, dtype=np.uint8)
    for i in range(_N_CUE_FRAMES)
]
_RESULT_FRAME_DATA = [
    np.full((10, 10, 3), i + 101, dtype=np.uint8)
    for i in range(_N_RESULT_FRAMES)
]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def trigger():
    return MagicMock()


@pytest.fixture()
def renderer():
    r = MagicMock()
    r.upload_texture.side_effect = lambda _: TextureHandle()
    return r


@pytest.fixture()
def paradigm(trigger):
    return Classic40SSVEP_2(resource_root=Path(__file__).parent, parameters={})


@pytest.fixture(autouse=True)
def _mock_frame_pack():
    """Provide small frames without opening the bundled frame pack."""
    with patch(
        "omnibci.infrastructure.plugins.paradigms.classic40SSVEP.ssvep._frame_pack_path",
        side_effect=lambda root: "classic40_ssvep.ofp",
    ), patch(
        "omnibci.infrastructure.plugins.paradigms.classic40SSVEP.ssvep.FramePack",
        autospec=True,
    ) as frame_pack:
        pack = frame_pack.return_value
        pack.__enter__.return_value = pack
        pack.idle_frame = _IDLE_FRAME
        pack.iter_stimulation_frames.side_effect = lambda: iter(_STIM_FRAMES)
        pack.iter_cue_frames.side_effect = lambda: iter(_CUE_FRAME_DATA)
        pack.iter_result_frames.side_effect = lambda: iter(_RESULT_FRAME_DATA)
        yield pack


# ---------------------------------------------------------------------------
# Setup / teardown
# ---------------------------------------------------------------------------

class TestSetup:
    def test_setup_requires_both_capabilities(self, paradigm):
        with pytest.raises(TypeError):
            paradigm.setup()

    def test_uploads_expected_texture_count(self, paradigm, renderer, trigger):
        paradigm.setup(renderer=renderer, trigger=trigger)
        assert renderer.upload_texture.call_count == _TOTAL_UPLOADS

    def test_uploads_frame_pack_in_order(self, paradigm, renderer, trigger):
        paradigm.setup(renderer=renderer, trigger=trigger)
        frames = [_IDLE_FRAME, *_STIM_FRAMES, *_CUE_FRAME_DATA, *_RESULT_FRAME_DATA]
        assert renderer.upload_texture.call_count == len(frames) + 1
        for upload, frame in zip(renderer.upload_texture.call_args_list, frames):
            assert upload.args[0] is frame

    def test_closes_frame_pack_after_upload(self, paradigm, renderer, _mock_frame_pack, trigger):
        paradigm.setup(renderer=renderer, trigger=trigger)
        _mock_frame_pack.__exit__.assert_called_once_with(None, None, None)

    def test_closes_frame_pack_when_upload_fails(self, paradigm, renderer, _mock_frame_pack, trigger):
        error = RuntimeError("Texture upload failed")
        renderer.upload_texture.side_effect = error

        with pytest.raises(RuntimeError, match="Texture upload failed"):
            paradigm.setup(renderer=renderer, trigger=trigger)

        _mock_frame_pack.__exit__.assert_called_once()
        assert _mock_frame_pack.__exit__.call_args.args[:2] == (RuntimeError, error)

    def test_initial_state_is_idle(self, paradigm, renderer, trigger):
        paradigm.setup(renderer=renderer, trigger=trigger)
        assert paradigm._state == _State.IDLE
        assert paradigm._result_target_id == 0

    def test_stim_textures_populated(self, paradigm, renderer, trigger):
        paradigm.setup(renderer=renderer, trigger=trigger)
        assert len(paradigm._stim_textures) == _N_STIM_FRAMES

    def test_cue_textures_populated(self, paradigm, renderer, trigger):
        paradigm.setup(renderer=renderer, trigger=trigger)
        assert len(paradigm._cue_textures) == _N_CUE_FRAMES

    def test_result_textures_populated(self, paradigm, renderer, trigger):
        paradigm.setup(renderer=renderer, trigger=trigger)
        assert len(paradigm._result_textures) == _N_RESULT_FRAMES


class TestTeardown:
    def test_partial_upload_retains_every_completed_texture(self, paradigm, renderer, trigger):
        handles = [TextureHandle() for _ in range(3)]
        renderer.upload_texture.side_effect = [*handles, RuntimeError("upload failed")]
        with pytest.raises(RuntimeError, match="upload failed"):
            paradigm.setup(renderer=renderer, trigger=trigger)
        paradigm.teardown()
        assert {call.args[0] for call in renderer.release_texture.call_args_list} == set(handles)

    def test_one_release_failure_does_not_skip_other_textures(self, paradigm, renderer, trigger):
        paradigm.setup(renderer=renderer, trigger=trigger)
        total = renderer.upload_texture.call_count
        renderer.release_texture.side_effect = [RuntimeError("release failed")] + [None] * (total - 1)
        with pytest.raises(ExceptionGroup, match="texture cleanup failed"):
            paradigm.teardown()
        assert renderer.release_texture.call_count == total

    def test_releases_all_uploaded_textures(self, paradigm, renderer, trigger):
        paradigm.setup(renderer=renderer, trigger=trigger)
        n = renderer.upload_texture.call_count
        paradigm.teardown()
        assert renderer.release_texture.call_count == n

    def test_clears_texture_lists(self, paradigm, renderer, trigger):
        paradigm.setup(renderer=renderer, trigger=trigger)
        paradigm.teardown()
        assert paradigm._stim_textures == []
        assert paradigm._cue_textures == []
        assert paradigm._result_textures == []

    def test_clears_renderer_reference(self, paradigm, renderer, trigger):
        paradigm.setup(renderer=renderer, trigger=trigger)
        paradigm.teardown()
        assert paradigm._renderer is None


# ---------------------------------------------------------------------------
# State machine — present_trial
# ---------------------------------------------------------------------------

class TestTrialStimulusPresent:
    def test_valid_event_id_transitions_to_cue(self, paradigm, renderer, trigger):
        paradigm.setup(renderer=renderer, trigger=trigger)
        paradigm.present_trial({"event_id": 1})          # 1-based: target 1 = 'A'
        assert paradigm._state == _State.CUE

    def test_zero_is_no_cue_goes_straight_to_stim(self, paradigm, renderer, trigger):
        paradigm.setup(renderer=renderer, trigger=trigger)
        paradigm.present_trial({"event_id": 0})          # 0 = baseline / no-cue
        assert paradigm._state == _State.STIM

    def test_requires_fixed_60hz_display_mode(self):
        assert Classic40SSVEP_2.required_refresh_rate_hz == 60.0

    def test_cue_timeline_starts_at_first_frame(self, paradigm, renderer, trigger):
        paradigm.setup(renderer=renderer, trigger=trigger)
        paradigm.present_trial({"event_id": 2})
        assert paradigm._phase_start_time is None

    def test_current_event_stored(self, paradigm, renderer, trigger):
        paradigm.setup(renderer=renderer, trigger=trigger)
        paradigm.present_trial({"event_id": 7})
        assert paradigm._current_event == 7

    def test_invalid_event_id_preserves_idle(self, paradigm, renderer, trigger):
        paradigm.setup(renderer=renderer, trigger=trigger)
        with pytest.raises(ValueError):
            paradigm.present_trial({"event_id": 99})
        assert paradigm._state == _State.IDLE

    def test_invalid_event_id_does_not_send_trigger(self, paradigm, renderer, trigger):
        paradigm.setup(renderer=renderer, trigger=trigger)
        with pytest.raises(ValueError):
            paradigm.present_trial({"event_id": 99})
        trigger.send.assert_not_called()


# ---------------------------------------------------------------------------
# State machine — render_frame transitions
# ---------------------------------------------------------------------------

class TestRenderFrameTransitions:
    def _tick(self, paradigm, n=1):
        t = getattr(self, "_t", 0.0)
        for _ in range(n):
            paradigm.render_frame(t)
            t += 1 / 60
        self._t = t

    def test_cue_transitions_to_stim_after_countdown(self, paradigm, renderer, trigger):
        paradigm.setup(renderer=renderer, trigger=trigger)
        paradigm.present_trial({"event_id": 1})
        self._tick(paradigm, _CUE_FRAMES - 1)
        assert paradigm._state == _State.CUE
        trigger.send.assert_not_called()
        renderer.reset_mock()
        self._tick(paradigm)
        assert renderer.draw_texture.call_args_list[0] == call(
            paradigm._cue_textures[0], _STIM_RECT
        )
        renderer.draw_rect.assert_not_called()
        assert paradigm._state == _State.STIM
        trigger.send.assert_called_once_with(1)
        renderer.reset_mock()
        self._tick(paradigm)
        renderer.draw_rect.assert_not_called()
        assert renderer.draw_texture.call_args_list[0] == call(
            paradigm._stim_textures[0], _STIM_RECT
        )

    def test_stim_plays_all_frames(self, paradigm, renderer, trigger):
        paradigm.setup(renderer=renderer, trigger=trigger)
        paradigm.present_trial({"event_id": 1})
        self._tick(paradigm, _CUE_FRAMES)          # CUE → STIM
        renderer.reset_mock()
        self._tick(paradigm, _N_STIM_FRAMES)       # play every frame
        assert [c.args[0] for c in renderer.draw_texture.call_args_list[::2]] == paradigm._stim_textures

    def test_stim_transitions_to_idle_when_frames_exhausted(self, paradigm, renderer, trigger):
        paradigm.setup(renderer=renderer, trigger=trigger)
        paradigm.present_trial({"event_id": 1})
        self._tick(paradigm, _CUE_FRAMES + _N_STIM_FRAMES + 1)
        assert paradigm._state == _State.IDLE

    def test_stim_clears_trigger_on_finish(self, paradigm, renderer, trigger):
        paradigm.setup(renderer=renderer, trigger=trigger)
        paradigm.present_trial({"event_id": 1})
        self._tick(paradigm, _CUE_FRAMES + _N_STIM_FRAMES + 1)
        trigger.clear.assert_called()

    def test_result_transitions_to_idle_after_countdown(self, paradigm, renderer, trigger):
        paradigm.setup(renderer=renderer, trigger=trigger)
        paradigm.present_result({"target_id": 1})            # 1-based: target 1 = 'A'
        assert paradigm._state == _State.RESULT
        self._tick(paradigm, _RESULT_FRAMES - 1)
        assert paradigm._state == _State.RESULT
        assert paradigm._result_target_id == 1
        renderer.reset_mock()
        self._tick(paradigm)
        assert renderer.draw_texture.call_args_list[0] == call(
            paradigm._result_textures[0], _STIM_RECT
        )
        assert paradigm._state == _State.IDLE
        assert paradigm._result_target_id == 0
        renderer.reset_mock()
        self._tick(paradigm)
        assert renderer.draw_texture.call_args_list[0] == call(
            paradigm._init_tex, _STIM_RECT
        )
        renderer.draw_rect.assert_not_called()


# ---------------------------------------------------------------------------
# present_result
# ---------------------------------------------------------------------------

class TestTrialResultPresent:
    def test_transitions_to_result_state(self, paradigm, renderer, trigger):
        paradigm.setup(renderer=renderer, trigger=trigger)
        paradigm.present_result({"target_id": 1})
        assert paradigm._state == _State.RESULT

    def test_result_timeline_starts_at_first_frame(self, paradigm, renderer, trigger):
        paradigm.setup(renderer=renderer, trigger=trigger)
        paradigm.present_result({"target_id": 1})
        assert paradigm._phase_start_time is None

    def test_add_text_accumulates(self, paradigm, renderer, trigger):
        paradigm.setup(renderer=renderer, trigger=trigger)
        paradigm.present_result({"target_id": 1})   # 'A'
        paradigm.present_result({"target_id": 2})   # 'B'
        assert paradigm._text_view._content == "AB"

    def test_del_removes_last_char(self, paradigm, renderer, trigger):
        paradigm.setup(renderer=renderer, trigger=trigger)
        paradigm.present_result({"target_id": 1})   # 'A'
        paradigm.present_result({"target_id": 40})  # 'Del'
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
    def test_idle_calls_clear_and_draw_texture(self, paradigm, renderer, trigger):
        paradigm.setup(renderer=renderer, trigger=trigger)
        renderer.reset_mock()
        paradigm.render_frame(0.0)
        renderer.clear.assert_called_once()
        renderer.draw_texture.assert_called()

    @pytest.mark.parametrize("target_id", [1, 8, 9, 40])
    def test_cue_draws_target_frame_from_pack(self, paradigm, renderer, target_id, trigger):
        paradigm.setup(renderer=renderer, trigger=trigger)
        paradigm.present_trial({"event_id": target_id})
        renderer.reset_mock()
        paradigm.render_frame()

        assert renderer.draw_texture.call_args_list == [
            call(paradigm._cue_textures[target_id - 1], _STIM_RECT),
            call(paradigm._text_view._tex, paradigm._text_view._rect),
        ]
        renderer.draw_rect.assert_not_called()

    @pytest.mark.parametrize(("result_id", "text"), [(8, "AH"), (40, "")])
    def test_result_highlights_recognized_target_instead_of_cue(
        self, paradigm, renderer, result_id, text, trigger
    ):
        paradigm.setup(renderer=renderer, trigger=trigger)
        paradigm.present_result({"target_id": 1})
        paradigm.present_trial({"event_id": 1})
        paradigm.render_frame(0.0)
        paradigm.present_result({"target_id": result_id})
        renderer.reset_mock()
        paradigm.render_frame(1 / 60)

        assert paradigm._current_event == 1
        assert paradigm._result_target_id == result_id
        assert paradigm._text_view._content == text
        assert renderer.draw_texture.call_args_list == [
            call(paradigm._result_textures[result_id - 1], _STIM_RECT),
            call(paradigm._text_view._tex, paradigm._text_view._rect),
        ]
        renderer.draw_rect.assert_not_called()

    def test_new_trial_clears_result_and_moves_cue(self, paradigm, renderer, trigger):
        paradigm.setup(renderer=renderer, trigger=trigger)
        paradigm.present_trial({"event_id": 1})
        paradigm.render_frame(0.0)
        paradigm.present_result({"target_id": 40})
        paradigm.render_frame(1 / 60)
        paradigm.present_trial({"event_id": 9})
        renderer.reset_mock()
        paradigm.render_frame(2 / 60)

        assert paradigm._result_target_id == 0
        assert renderer.draw_texture.call_args_list[0] == call(
            paradigm._cue_textures[8], _STIM_RECT
        )
        renderer.draw_rect.assert_not_called()

    @pytest.mark.parametrize("target_id", [0])
    def test_no_cue_mode_draws_stimulation_without_border(self, paradigm, renderer, target_id, trigger):
        paradigm.setup(renderer=renderer, trigger=trigger)
        paradigm.present_result({"target_id": 1})
        paradigm.present_trial({"event_id": target_id})
        renderer.reset_mock()
        paradigm.render_frame()

        assert paradigm._result_target_id == 0
        assert renderer.draw_texture.call_args_list[0] == call(
            paradigm._stim_textures[0], _STIM_RECT
        )
        renderer.draw_rect.assert_not_called()

    def test_render_frame_noop_without_renderer(self, paradigm):
        # should not raise
        paradigm.render_frame(0.0)


class TestTimelineAndMissedFrames:
    def test_short_gap_skips_expired_images_without_replay(self, paradigm, renderer, trigger):
        paradigm.setup(renderer=renderer, trigger=trigger)
        paradigm.present_trial({"event_id": 0})
        paradigm.render_frame(10.0)
        renderer.reset_mock()
        paradigm.render_frame(10.0 + 3 / 60)
        assert renderer.draw_texture.call_args_list[0] == call(paradigm._stim_textures[3], _STIM_RECT)
        assert paradigm.missed_frames == 2

    def test_one_second_gap_ends_240_frame_stimulus_on_deadline(self, tmp_path, renderer, trigger):
        frames_240 = [np.full((10, 10, 1), (i % 200) + 1, dtype=np.uint8) for i in range(240)]
        pack = MagicMock()
        pack.idle_frame = _IDLE_FRAME
        pack.iter_stimulation_frames.return_value = iter(frames_240)
        pack.iter_cue_frames.return_value = iter(_CUE_FRAME_DATA)
        pack.iter_result_frames.return_value = iter(_RESULT_FRAME_DATA)
        with (
            patch(
                "omnibci.infrastructure.plugins.paradigms.classic40SSVEP.ssvep._frame_pack_path",
                return_value=Path("fake.ofp"),
            ),
            patch(
                "omnibci.infrastructure.plugins.paradigms.classic40SSVEP.ssvep.FramePack",
                return_value=MagicMock(
                    __enter__=MagicMock(return_value=pack),
                    __exit__=MagicMock(return_value=False),
                ),
            ),
        ):
            p240 = Classic40SSVEP_2(parameters={}, resource_root=tmp_path)
            p240.setup(renderer=renderer, trigger=trigger)
            p240.present_trial({"event_id": 0})
            for i in range(60):
                p240.render_frame(i / 60)
            for i in range(120, 240):
                p240.render_frame(i / 60)
            assert p240.is_presenting()
            p240.render_frame(4.0)
            assert not p240.is_presenting()
            assert p240.missed_frames == 60
            trigger.clear.assert_called()

    def test_gap_across_cue_boundary_emits_single_delayed_marker(self, paradigm, renderer, trigger):
        paradigm.setup(renderer=renderer, trigger=trigger)
        paradigm.present_trial({"event_id": 7})
        paradigm.render_frame(0.0)
        renderer.reset_mock()
        paradigm.render_frame((_CUE_FRAMES + 1) / 60)
        assert renderer.draw_texture.call_args_list[0] == call(paradigm._stim_textures[1], _STIM_RECT)
        paradigm.render_frame((_CUE_FRAMES + 3) / 60)
        trigger.send.assert_called_once_with(7)

    def test_gap_past_entire_stimulus_omits_expired_start_marker(self, paradigm, renderer, trigger):
        paradigm.setup(renderer=renderer, trigger=trigger)
        paradigm.present_trial({"event_id": 7})
        paradigm.render_frame(0.0)
        paradigm.render_frame((_CUE_FRAMES + _N_STIM_FRAMES) / 60)
        assert not paradigm.is_presenting()
        trigger.send.assert_not_called()

    def test_result_interval_ends_on_time_after_gap(self, paradigm, renderer, trigger):
        paradigm.setup(renderer=renderer, trigger=trigger)
        paradigm.present_result({"target_id": 7})
        paradigm.render_frame(0.0)
        paradigm.render_frame(_RESULT_FRAMES / 60)
        assert not paradigm.is_presenting()

    def test_duplicate_or_regressed_timestamp_redraws_without_advancing(self, paradigm, renderer, trigger):
        paradigm.setup(renderer=renderer, trigger=trigger)
        paradigm.present_trial({"event_id": 0})
        paradigm.render_frame(1.0)
        renderer.reset_mock()
        paradigm.render_frame(1.0)
        paradigm.render_frame(0.9)
        assert renderer.clear.call_count == 2
        drawn_stim = [c.args[0] for c in renderer.draw_texture.call_args_list[::2]]
        assert drawn_stim == [paradigm._stim_textures[0], paradigm._stim_textures[0]]
        assert paradigm.missed_frames == 0

    def test_double_buffer_duplicate_regressed_and_boundary_slots_never_replay_stale_frame(
        self, paradigm, trigger, monkeypatch
    ):
        import asyncio
        from omnibci.infrastructure.stimulation.render_loop import RenderLoop

        class _DoubleBufferedRenderer:
            def __init__(self) -> None:
                self.front: list[object] = ["UNINITIALIZED_FRONT"]
                self.back: list[object] = ["UNINITIALIZED_BACK"]
                self.presented: list[list[object]] = []
                self._remaining = 0

            def run_times(self, times: list[float]) -> None:
                self._remaining = len(times)
                samples = iter([0.0, *times])
                monkeypatch.setattr(
                    "omnibci.infrastructure.stimulation.render_loop.time.perf_counter",
                    lambda: next(samples),
                )
                asyncio.run(RenderLoop(self, paradigm).run_async())

            def should_close(self) -> bool:
                if self._remaining <= 0:
                    return True
                self._remaining -= 1
                return False

            def poll_events(self) -> None:
                pass

            def refresh_rate_hz(self) -> float:
                return 60.0

            def upload_texture(self, image) -> TextureHandle:
                return TextureHandle()

            def release_texture(self, handle: TextureHandle) -> None:
                pass

            def clear(self, color=None) -> None:
                self.back = []

            def draw_rect(self, rect, color) -> None:
                self.back.append(("rect", rect, color))

            def draw_texture(self, handle, rect) -> None:
                self.back.append(("tex", handle, rect))

            def end_frame(self) -> None:
                self.front, self.back = self.back, self.front
                self.presented.append(list(self.front))

        db = _DoubleBufferedRenderer()
        paradigm.setup(renderer=db, trigger=trigger)

        # Free-run duplicate slot + regressed timestamp: slot 0, slot 1, duplicate slot 1, regressed slot 0.
        paradigm.present_trial({"event_id": 0})
        db.run_times([0.0, 1 / 60, 0.020, 0.005])
        assert all("UNINITIALIZED_FRONT" not in f and "UNINITIALIZED_BACK" not in f for f in db.presented)
        presented_tex = [f[0][1] for f in db.presented]
        assert presented_tex == [
            paradigm._stim_textures[0],
            paradigm._stim_textures[1],
            paradigm._stim_textures[1],
            paradigm._stim_textures[1],
        ]

        # Cued trial boundary: last cue frame (slot 59) + duplicate slot 59 (even though _state is now STIM!),
        # then first stim frame (slot 60) + duplicate slot 60, then stimulus end (slot 65) + duplicate.
        db.presented.clear()
        trigger.reset_mock()
        paradigm.present_trial({"event_id": 7})
        db.run_times([
            0.0,
            59 / 60,
            59 / 60 + 0.002,
            60 / 60,
            60 / 60 + 0.002,
            (_CUE_FRAMES + _N_STIM_FRAMES) / 60,
            (_CUE_FRAMES + _N_STIM_FRAMES) / 60 + 0.002,
        ])
        assert all("UNINITIALIZED_FRONT" not in f and "UNINITIALIZED_BACK" not in f for f in db.presented)
        boundary_tex = [f[0][1] for f in db.presented]
        assert boundary_tex == [
            paradigm._cue_textures[6],
            paradigm._cue_textures[6],
            paradigm._cue_textures[6],
            paradigm._stim_textures[0],
            paradigm._stim_textures[0],
            paradigm._init_tex,
            paradigm._init_tex,
        ]
        assert trigger.mock_calls == [call.clear(), call.send(7), call.clear()]


from unittest.mock import Mock


@pytest.fixture
def payload_paradigm(tmp_path):
    instance = Classic40SSVEP_2(parameters={}, resource_root=tmp_path)
    instance.trigger = Mock()
    instance.trigger.clear.return_value = instance.trigger.send.return_value = None
    instance._text_view = Mock()
    return instance


@pytest.mark.parametrize('event_id', [0, 1, 40])
def test_explicit_trial_preserves_cue_or_free_run_timing(payload_paradigm, event_id):
    assert payload_paradigm.present_trial({'event_id': event_id}) is None
    assert payload_paradigm.is_presenting()
    if event_id == 0:
        payload_paradigm.trigger.clear.assert_called_once_with()
        payload_paradigm.trigger.send.assert_called_once_with(0)
    else:
        assert not payload_paradigm.trigger.mock_calls


@pytest.mark.parametrize('payload', [None, 1, {}, {'event_id': True}, {'event_id': -1},
                                     {'event_id': 41}, {'event_id': 1.0}, {'event_id': '1'},
                                     {'event_id': 0, 'unknown': True}])
def test_invalid_trial_never_falls_back_to_free_run(payload_paradigm, payload):
    with pytest.raises(ValueError):
        payload_paradigm.present_trial(payload)
    assert not payload_paradigm.is_presenting()
    assert not payload_paradigm.trigger.mock_calls


@pytest.mark.parametrize('payload', [None, 1, {}, {'target_id': 0}, {'target_id': True},
                                     {'target_id': 41}, {'target_id': 1.0}, {'target_id': '1'}])
def test_invalid_result_changes_neither_activity_nor_feedback(payload_paradigm, payload):
    with pytest.raises(ValueError):
        payload_paradigm.present_result(payload)
    assert not payload_paradigm.is_presenting()
    assert not payload_paradigm._text_view.mock_calls


def test_result_object_preserves_character_and_delete_feedback(payload_paradigm):
    assert payload_paradigm.present_result({'target_id': 1}) is None
    assert payload_paradigm.is_presenting()
    payload_paradigm._text_view.add_text.assert_called_once()
    assert payload_paradigm.present_result({'target_id': 40}) is None
    payload_paradigm._text_view.remove_last.assert_called_once_with()
