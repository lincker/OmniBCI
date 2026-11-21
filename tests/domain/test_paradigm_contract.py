"""A loadable paradigm must implement frame rendering and trial readiness."""

import pytest

from omnibci.domain.ports.paradigm import Paradigm
from omnibci.sdk import FrameTiming


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
    frame = FrameTiming(15, .25, 1 / 60)
    assert paradigm.render_frame(frame) is True
    assert paradigm.frame is frame
    assert paradigm.is_presenting() is False
