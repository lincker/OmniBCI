"""Exercise display opportunities without timers or a graphics context."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest


def make_clock():
    from omnibci.infrastructure.stimulation.frame_clock import FrameClock
    source = SimpleNamespace(read=Mock(return_value=None), close=Mock(), now=Mock(return_value=.5))
    return FrameClock(source), source


def frame(index, target=1):
    from omnibci.domain.ports.renderer import FrameTiming
    return FrameTiming(index, target, 1 / 60)


def test_pending_sample_is_consumed_without_waiting_for_another_notification():
    clock, source = make_clock()
    source.read.return_value = frame(10)
    first = clock.begin_frame()
    assert first.refresh_index == 10
    assert clock.end_frame(first, submit=True)
    assert clock.begin_frame() is None
    source.read.return_value = frame(14, 1 + 4 / 60)
    assert clock.begin_frame().refresh_index == 14
    assert clock.skipped_refreshes == 3


def test_expired_sample_is_not_delivered_and_is_never_replayed():
    clock, source = make_clock()
    source.read.return_value = frame(10)
    source.now.return_value = 1.01
    assert clock.begin_frame() is None
    source.read.return_value = frame(11, 1 + 1 / 60)
    assert clock.begin_frame().refresh_index == 11


@pytest.mark.parametrize('expired_by', ['time', 'new_refresh', 'unavailable'])
def test_late_or_unavailable_draw_cannot_be_submitted(expired_by):
    clock, source = make_clock()
    source.read.return_value = frame(10)
    pending = clock.begin_frame()
    if expired_by == 'time':
        source.now.return_value = 1.0
    if expired_by == 'new_refresh':
        source.read.return_value = frame(11, 1 + 1 / 60)
    assert clock.end_frame(pending, submit=expired_by != 'unavailable') is False
    assert clock.discarded_frames == 1
    with pytest.raises(RuntimeError, match='pending'):
        clock.end_frame(pending, submit=True)


def test_clock_loss_fails_instead_of_generating_timer_frames():
    clock, source = make_clock()
    source.now.return_value = 2.0
    with pytest.raises(RuntimeError, match='clock'):
        clock.begin_frame()


@pytest.mark.parametrize('next_frame', [(9, 1.1, 1 / 60), (11, .9, 1 / 60), (11, 1.1, 1 / 120)])
def test_clock_reset_time_regression_or_mode_change_fails(next_frame):
    from omnibci.domain.ports.renderer import FrameTiming
    clock, source = make_clock()
    source.read.return_value = frame(10)
    clock.end_frame(clock.begin_frame(), submit=True)
    source.read.return_value = FrameTiming(*next_frame)
    with pytest.raises(RuntimeError, match='clock|refresh'):
        clock.begin_frame()


def test_close_releases_source_once_and_rejects_more_frames():
    clock, source = make_clock()
    clock.close()
    clock.close()
    source.close.assert_called_once()
    with pytest.raises(RuntimeError, match='closed'):
        clock.begin_frame()
