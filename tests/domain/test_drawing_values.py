"""Drawing values reject invalid coordinates and colors before GPU calls."""
from dataclasses import FrozenInstanceError
import math

import numpy as np
import pytest

from omnibci.domain.ports.renderer import Color, Rect


@pytest.mark.parametrize('field', ['r', 'g', 'b', 'a'])
@pytest.mark.parametrize('value', [-.01, 1.01, math.nan, math.inf, -math.inf, True, '0.5', None])
def test_color_rejects_invalid_components(field, value):
    values = dict(r=0., g=.5, b=1., a=1.)
    values[field] = value
    with pytest.raises((TypeError, ValueError)):
        Color(**values)


@pytest.mark.parametrize('field', ['x', 'y', 'w', 'h'])
@pytest.mark.parametrize('value', [math.nan, math.inf, -math.inf, True, '1', None])
def test_rect_rejects_nonfinite_or_nonnumeric_fields(field, value):
    values = dict(x=-1., y=-1., w=2., h=2.)
    values[field] = value
    with pytest.raises((TypeError, ValueError)):
        Rect(**values)


@pytest.mark.parametrize('field', ['w', 'h'])
@pytest.mark.parametrize('value', [0, -1])
def test_rect_requires_positive_size(field, value):
    values = dict(x=0., y=0., w=1., h=1.)
    values[field] = value
    with pytest.raises(ValueError):
        Rect(**values)


def test_valid_values_keep_precision_defaults_and_coordinates_outside_viewport():
    color = Color(0, np.float64(.25), 1)
    assert (color.r, color.g, color.b, color.a) == (0., .25, 1., 1.)
    rect = Rect(-2, 3, np.float64(.125), 4)
    assert (rect.x, rect.y, rect.w, rect.h) == (-2., 3., .125, 4.)
    with pytest.raises((AttributeError, FrozenInstanceError)):
        color.r = .2
    with pytest.raises((AttributeError, FrozenInstanceError)):
        rect.w = -1
