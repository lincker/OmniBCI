"""Domain value objects and abstract contracts for rendering, paradigms, and effectors."""
from dataclasses import FrozenInstanceError
import math

import numpy as np
import pytest

from omnibci.domain.ports import renderer
from omnibci.domain.ports.effector import Effector
from omnibci.domain.ports.paradigm import Paradigm
from omnibci.domain.ports.renderer import Color, Rect
from omnibci.sdk import FrameTiming


@pytest.mark.parametrize("field", ["r", "g", "b", "a"])
@pytest.mark.parametrize("value", [-0.01, 1.01, math.nan, math.inf, -math.inf, True, "0.5", None])
def test_color_rejects_invalid_components(field, value):
    values = dict(r=0.0, g=0.5, b=1.0, a=1.0)
    values[field] = value
    with pytest.raises((TypeError, ValueError)):
        Color(**values)


@pytest.mark.parametrize("field", ["x", "y", "w", "h"])
@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf, True, "1", None])
def test_rect_rejects_nonfinite_or_nonnumeric_fields(field, value):
    values = dict(x=-1.0, y=-1.0, w=2.0, h=2.0)
    values[field] = value
    with pytest.raises((TypeError, ValueError)):
        Rect(**values)


@pytest.mark.parametrize("field", ["w", "h"])
@pytest.mark.parametrize("value", [0, -1])
def test_rect_requires_positive_size(field, value):
    values = dict(x=0.0, y=0.0, w=1.0, h=1.0)
    values[field] = value
    with pytest.raises(ValueError):
        Rect(**values)


def test_valid_values_keep_precision_defaults_and_coordinates_outside_viewport():
    color = Color(0, np.float64(0.25), 1)
    assert (color.r, color.g, color.b, color.a) == (0.0, 0.25, 1.0, 1.0)
    rect = Rect(-2, 3, np.float64(0.125), 4)
    assert (rect.x, rect.y, rect.w, rect.h) == (-2.0, 3.0, 0.125, 4.0)
    with pytest.raises((AttributeError, FrozenInstanceError)):
        color.r = 0.2
    with pytest.raises((AttributeError, FrozenInstanceError)):
        rect.w = -1


@pytest.mark.parametrize("index", [-1, True, 1.5, "1", None])
def test_refresh_index_requires_nonnegative_integer(index):
    with pytest.raises((TypeError, ValueError)):
        renderer.FrameTiming(index, 1.0, 1 / 60)


@pytest.mark.parametrize("field", ["target_time", "refresh_period"])
@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf, True, "1", None, -1])
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


class _PresentationOnlyParadigm(Paradigm):
    def setup(self, *, renderer, trigger):
        pass

    def teardown(self):
        pass

    def present_trial(self, params):
        pass

    def present_result(self, result):
        pass


@pytest.mark.parametrize("missing", ["render_frame", "is_presenting", "both"])
def test_incomplete_paradigm_cannot_be_constructed(missing):
    methods = {}
    if missing == "render_frame":
        methods["is_presenting"] = lambda self: False
    elif missing == "is_presenting":
        methods["render_frame"] = lambda self, frame: True
    implementation = type("IncompleteParadigm", (_PresentationOnlyParadigm,), methods)

    with pytest.raises(TypeError, match="abstract"):
        implementation()


def test_paradigm_with_both_required_methods_can_be_constructed():
    class CompleteParadigm(_PresentationOnlyParadigm):
        def render_frame(self, frame):
            self.frame = frame
            return True

        def is_presenting(self):
            return False

    paradigm = CompleteParadigm()
    frame = FrameTiming(15, 0.25, 1 / 60)
    assert paradigm.render_frame(frame) is True
    assert paradigm.frame is frame
    assert paradigm.is_presenting() is False


def test_complete_effector_can_be_constructed_and_executed():
    class Device(Effector):
        def setup(self): pass
        def execute(self, command): self.command = command
        def quiesce(self): pass
        def teardown(self): pass

    device = Device()
    device.execute({"target_id": 3})
    assert device.command == {"target_id": 3}


@pytest.mark.parametrize("missing", ["execute", "quiesce"])
def test_incomplete_effector_is_rejected_before_device_setup(missing):
    methods = {
        "setup": lambda self: (_ for _ in ()).throw(AssertionError("must not acquire a device")),
        "execute": lambda self, command: None,
        "quiesce": lambda self: None,
        "teardown": lambda self: None,
    }
    del methods[missing]
    implementation = type("IncompleteDevice", (Effector,), methods)

    with pytest.raises(TypeError, match=missing):
        implementation()
