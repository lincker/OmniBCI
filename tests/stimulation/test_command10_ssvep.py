"""Command10's display timeline, target protocol and owned texture lifecycle."""
from pathlib import Path
from unittest.mock import MagicMock, call
import math
import weakref

import numpy as np
import pytest

from omnibci.infrastructure.plugins.paradigms.command10SSVEP.paradigm import Command10SSVEP
from omnibci.infrastructure.plugins.paradigms.command10SSVEP.targets import TARGETS
from omnibci.sdk import Rect, RenderSurface, TextureHandle, TriggerOutput


PACKAGE = Path(__file__).resolve().parents[2] / 'omnibci/infrastructure/plugins/paradigms/command10SSVEP'
EXPECTED = (
    (1, 'Up', 1, 3, 8.0, 0),
    (2, 'Down', 3, 3, 8.8, .5),
    (3, 'Left', 2, 2, 9.6, 1),
    (4, 'Right', 2, 4, 10.4, 1.5),
    (5, 'A', 1, 1, 11.2, 0),
    (6, 'X', 1, 5, 12.0, .5),
    (7, 'B', 2, 1, 12.8, 1),
    (8, 'Y', 2, 5, 13.6, 1.5),
    (9, 'C', 3, 1, 14.4, 0),
    (10, 'Z', 3, 5, 15.2, .5),
)


_CLOCKS: weakref.WeakKeyDictionary[Command10SSVEP, list[float]] = weakref.WeakKeyDictionary()


def tick(paradigm, count=1):
    state = _CLOCKS.setdefault(paradigm, [0.0])
    for _ in range(count):
        paradigm.render_frame(state[0])
        state[0] += 1 / 60


def target_rect(row, column):
    return Rect(x=((column - 3) * 360 - 100) / 960,
                y=((2 - row) * 300 - 100) / 540, w=200 / 960, h=200 / 540)


@pytest.fixture
def surface():
    renderer = MagicMock(spec=RenderSurface)
    renderer.upload_texture.side_effect = lambda image: TextureHandle()
    return renderer


@pytest.fixture
def trigger():
    return MagicMock(spec=TriggerOutput)


@pytest.fixture
def paradigm(surface, trigger):
    instance = Command10SSVEP(parameters={}, resource_root=PACKAGE)
    instance.setup(renderer=surface, trigger=trigger)
    yield instance
    instance.teardown()


def test_requires_fixed_60hz_display_mode():
    assert Command10SSVEP.required_refresh_rate_hz == 60.0


def test_target_protocol_matches_approved_layout():
    assert len(TARGETS) == 10
    for target, (identifier, label, row, column, frequency, phase_pi) in zip(TARGETS, EXPECTED):
        assert (target.target_id, target.label, target.row, target.column) == (identifier, label, row, column)
        assert target.frequency_hz == frequency
        assert target.phase_rad == pytest.approx(phase_pi * math.pi)


@pytest.mark.parametrize('index', [0, 1, 7, 59, 100, 239])
def test_all_target_luminances_follow_display_refreshes(paradigm, surface, trigger, index):
    paradigm.present_trial({'cue_target_id': 0})
    if index:
        tick(paradigm, index)
        surface.draw_rect.reset_mock()
    tick(paradigm, 1)
    rectangles = {entry.args[0]: entry.args[1] for entry in surface.draw_rect.call_args_list}
    for _, _, row, column, frequency, phase_pi in EXPECTED:
        color = rectangles[target_rect(row, column)]
        brightness = .5 + .5 * math.sin(2 * math.pi * frequency * index / 60 + phase_pi * math.pi)
        assert (color.r, color.g, color.b) == pytest.approx((brightness,) * 3)
    trigger.send.assert_called_once_with(11)


def test_cue_marker_is_requested_at_stimulus_start(paradigm, trigger):
    paradigm.present_trial({'cue_target_id': 5})
    tick(paradigm, 60)
    trigger.send.assert_not_called()
    tick(paradigm, 2)
    assert trigger.mock_calls == [call.clear(), call.send(5)]


def test_early_result_is_cached_until_fixed_stimulus_end(paradigm, surface, trigger):
    paradigm.present_trial({'cue_target_id': 0})
    tick(paradigm, 1)
    paradigm.present_result({'target_id': 8})
    tick(paradigm, 238)
    surface.reset_mock()
    tick(paradigm, 1)
    assert surface.draw_texture.call_count == 11  # Ten labels and feedback caption.
    trigger.clear.assert_called_once()
    surface.reset_mock()
    tick(paradigm, 1)
    assert surface.draw_texture.call_count == 12  # Result adds a central target label.
    assert surface.draw_texture.call_args_list[-1].args[0] == surface.draw_texture.call_args_list[7].args[0]
    assert trigger.clear.call_count == 2
    tick(paradigm, 89)
    assert paradigm.is_presenting()
    tick(paradigm, 1)
    assert not paradigm.is_presenting()


def test_no_result_finishes_after_feedback_interval(paradigm, trigger):
    paradigm.present_trial({'cue_target_id': 0})
    tick(paradigm, 241)
    assert paradigm.is_presenting()
    tick(paradigm, 90)
    assert not paradigm.is_presenting()
    assert trigger.mock_calls == [call.clear(), call.send(11), call.clear()]


def test_result_can_arrive_during_feedback(paradigm, surface):
    paradigm.present_trial({'cue_target_id': 0})
    tick(paradigm, 241)
    paradigm.present_result({'target_id': 9})
    surface.reset_mock()
    tick(paradigm, 1)
    assert surface.draw_texture.call_count == 12
    with pytest.raises(ValueError, match='already'):
        paradigm.present_result({'target_id': 9})


def test_repeated_cued_trials_reset_marker_and_phase(paradigm, surface, trigger):
    for _ in range(2):
        paradigm.present_trial({'cue_target_id': 3})
        tick(paradigm, 60)
        surface.draw_rect.reset_mock()
        tick(paradigm, 1)
        rectangles = {entry.args[0]: entry.args[1] for entry in surface.draw_rect.call_args_list}
        assert rectangles[target_rect(1, 3)].r == pytest.approx(.5)
        assert rectangles[target_rect(2, 4)].r == pytest.approx(0)
        tick(paradigm, 330)
        assert not paradigm.is_presenting()
    assert trigger.mock_calls == [call.clear(), call.send(3), call.clear()] * 2


@pytest.mark.parametrize('payload', [{}, {'event_id': 1}, {'cue_target_id': -1}, {'cue_target_id': 11},
                                   {'cue_target_id': True}, {'cue_target_id': 1.0}, {'cue_target_id': '1'},
                                   {'cue_target_id': 1, 'extra': 2}, None])
def test_invalid_trial_cannot_send_or_change_state(paradigm, trigger, payload):
    with pytest.raises(ValueError):
        paradigm.present_trial(payload)
    assert not paradigm.is_presenting()
    trigger.clear.assert_not_called()
    trigger.send.assert_not_called()


@pytest.mark.parametrize('payload', [{}, {'target_id': 0}, {'target_id': 11}, {'target_id': True},
                                   {'target_id': 1.0}, {'target_id': '1'}, {'target_id': 1, 'extra': 2}])
def test_invalid_result_keeps_trial_active(paradigm, payload):
    paradigm.present_trial({'cue_target_id': 0})
    paradigm.render_frame(0.0)
    with pytest.raises(ValueError):
        paradigm.present_result(payload)
    assert paradigm.is_presenting()


def test_result_requires_an_active_started_trial(paradigm):
    with pytest.raises(ValueError, match='active'):
        paradigm.present_result({'target_id': 1})
    paradigm.present_trial({'cue_target_id': 1})
    with pytest.raises(ValueError, match='active'):
        paradigm.present_result({'target_id': 1})


def test_busy_trial_cannot_be_overwritten(paradigm, trigger):
    paradigm.present_trial({'cue_target_id': 1})
    with pytest.raises(RuntimeError, match='active'):
        paradigm.present_trial({'cue_target_id': 2})
    trigger.clear.assert_called_once()


def test_failed_baseline_clear_does_not_start_trial(paradigm, trigger):
    trigger.clear.side_effect = RuntimeError('driver failed')
    with pytest.raises(RuntimeError, match='driver failed'):
        paradigm.present_trial({'cue_target_id': 0})
    assert not paradigm.is_presenting()


def test_assets_upload_once_without_runtime_font_lookup(paradigm, surface):
    assert surface.upload_texture.call_count == 11
    for upload in surface.upload_texture.call_args_list:
        pixels = upload.args[0]
        assert pixels.dtype == np.uint8
        assert pixels.shape[2] == 4
        assert pixels.flags.c_contiguous
        assert pixels[:, :, 3].max() > 0
        assert pixels[:, :, 3].min() == 0
    surface.upload_texture.reset_mock()
    paradigm.present_trial({'cue_target_id': 0})
    tick(paradigm, 5)
    surface.upload_texture.assert_not_called()


def test_texture_cleanup_is_complete_and_repeatable(surface, trigger):
    instance = Command10SSVEP(parameters={}, resource_root=PACKAGE)
    instance.setup(renderer=surface, trigger=trigger)
    instance.teardown()
    assert surface.release_texture.call_count == 11
    assert len({entry.args[0] for entry in surface.release_texture.call_args_list}) == 11
    instance.teardown()
    assert surface.release_texture.call_count == 11
    assert instance.render_frame(0.0) is None


def test_partial_setup_and_failed_release_can_be_cleaned_up(surface, trigger):
    handles = [TextureHandle(), TextureHandle()]
    surface.upload_texture.side_effect = [*handles, RuntimeError('upload failed')]
    instance = Command10SSVEP(parameters={}, resource_root=PACKAGE)
    with pytest.raises(RuntimeError, match='upload failed'):
        instance.setup(renderer=surface, trigger=trigger)
    surface.release_texture.side_effect = [RuntimeError('release failed'), None]
    with pytest.raises(ExceptionGroup, match='cleanup'):
        instance.teardown()
    assert surface.release_texture.call_args_list == [call(handle) for handle in handles]
    surface.release_texture.side_effect = None
    instance.teardown()
    assert surface.release_texture.call_args == call(handles[0])


def test_unconfigured_or_unready_usage_is_rejected(surface, trigger):
    with pytest.raises(ValueError):
        Command10SSVEP(parameters={'refresh_rate': 120}, resource_root=PACKAGE)
    instance = Command10SSVEP(parameters={}, resource_root=PACKAGE)
    with pytest.raises(RuntimeError, match='setup'):
        instance.present_trial({'cue_target_id': 0})
    instance.setup(renderer=surface, trigger=trigger)
    with pytest.raises(RuntimeError, match='initialized'):
        instance.setup(renderer=surface, trigger=trigger)
    instance.teardown()


def test_short_gap_preserves_sinusoidal_phase_and_records_misses(paradigm, surface, trigger):
    paradigm.present_trial({'cue_target_id': 0})
    paradigm.render_frame(0.0)
    surface.draw_rect.reset_mock()
    paradigm.render_frame(3 / 60)
    rectangles = {entry.args[0]: entry.args[1] for entry in surface.draw_rect.call_args_list}
    for _, _, row, column, frequency, phase_pi in EXPECTED:
        color = rectangles[target_rect(row, column)]
        brightness = .5 + .5 * math.sin(2 * math.pi * frequency * 3 / 60 + phase_pi * math.pi)
        assert (color.r, color.g, color.b) == pytest.approx((brightness,) * 3)
    assert paradigm.missed_frames == 2


def test_one_second_gap_ends_stimulus_at_four_seconds(paradigm, surface, trigger):
    paradigm.present_trial({'cue_target_id': 0})
    for i in range(60):
        paradigm.render_frame(i / 60)
    for i in range(120, 240):
        paradigm.render_frame(i / 60)
    assert paradigm.is_presenting()
    paradigm.render_frame(4.0)
    assert paradigm._state.name == 'RESULT'
    assert paradigm.missed_frames == 60
    assert trigger.mock_calls == [call.clear(), call.send(11), call.clear()]
    paradigm.render_frame(5.5)
    assert not paradigm.is_presenting()


def test_gap_across_cue_boundary_emits_single_delayed_marker(paradigm, surface, trigger):
    paradigm.present_trial({'cue_target_id': 5})
    paradigm.render_frame(0.0)
    surface.draw_rect.reset_mock()
    paradigm.render_frame(63 / 60)
    rectangles = {entry.args[0]: entry.args[1] for entry in surface.draw_rect.call_args_list}
    assert rectangles[target_rect(1, 3)].r == pytest.approx(.5 + .5 * math.sin(2 * math.pi * 8 * 3 / 60))
    trigger.send.assert_called_once_with(5)
    assert paradigm.missed_frames == 3


def test_gap_across_stimulus_end_clears_trigger_and_enters_result(paradigm, trigger):
    paradigm.present_trial({'cue_target_id': 0})
    paradigm.render_frame(0.0)
    paradigm.render_frame(4.25)
    assert paradigm._state.name == 'RESULT'
    assert trigger.mock_calls == [call.clear(), call.send(11), call.clear()]
    paradigm.render_frame(5.5)
    assert not paradigm.is_presenting()


def test_gap_past_entire_stimulus_omits_expired_start_marker(paradigm, trigger):
    paradigm.present_trial({'cue_target_id': 5})
    paradigm.render_frame(0.0)
    paradigm.render_frame(5.5)
    trigger.send.assert_not_called()
    with pytest.raises(ValueError, match='active'):
        paradigm.present_result({'target_id': 1})
    paradigm.render_frame(6.5)
    assert not paradigm.is_presenting()


def test_duplicate_or_regressed_timestamp_does_not_advance(paradigm, surface, trigger):
    paradigm.present_trial({'cue_target_id': 0})
    paradigm.render_frame(1.0)
    surface.reset_mock()
    paradigm.render_frame(1.0)
    paradigm.render_frame(0.5)
    surface.clear.assert_not_called()
    surface.draw_rect.assert_not_called()
    trigger.send.assert_called_once_with(11)
