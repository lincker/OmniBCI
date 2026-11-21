"""Timing values cannot invent a valid refresh from malformed clock data."""
from dataclasses import FrozenInstanceError
import math

import pytest

from omnibci.domain.ports import renderer


@pytest.mark.parametrize('index', [-1, True, 1.5, '1', None])
def test_refresh_index_requires_nonnegative_integer(index):
    with pytest.raises((TypeError, ValueError)):
        renderer.FrameTiming(index, 1.0, 1 / 60)


@pytest.mark.parametrize('field', ['target_time', 'refresh_period'])
@pytest.mark.parametrize('value', [math.nan, math.inf, -math.inf, True, '1', None, -1])
def test_invalid_clock_values_are_rejected(field, value):
    values = dict(refresh_index=0, target_time=1.0, refresh_period=1 / 60)
    values[field] = value
    with pytest.raises((TypeError, ValueError)):
        renderer.FrameTiming(**values)


def test_zero_period_is_invalid_but_session_origin_is_valid():
    with pytest.raises(ValueError):
        renderer.FrameTiming(0, 0, 0)
    frame = renderer.FrameTiming(0, 0, 1 / 60)
    assert frame.target_time == 0.0
    with pytest.raises(FrozenInstanceError):
        frame.refresh_index = 1
