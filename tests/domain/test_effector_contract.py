"""Effectors declare execution and physical settlement before they can load."""
import pytest

from omnibci.domain.ports.effector import Effector


def test_complete_effector_uses_execute_without_a_play_adapter():
    class Device(Effector):
        def setup(self): pass
        def execute(self, command): self.command = command
        def quiesce(self): pass
        def teardown(self): pass

    device = Device()
    device.execute({"target_id": 3})
    assert device.command == {"target_id": 3}
    assert not hasattr(device, "play")


def test_missing_quiescence_is_rejected_before_device_setup():
    class Device(Effector):
        def setup(self): raise AssertionError("must not acquire a device")
        def execute(self, command): pass
        def play(self, command): pass
        def teardown(self): pass

    with pytest.raises(TypeError, match="quiesce"):
        Device()


def test_play_only_effector_is_rejected_without_constructor_retry():
    class Device(Effector):
        def setup(self): pass
        def play(self, command): pass
        def quiesce(self): pass
        def teardown(self): pass

    with pytest.raises(TypeError, match="execute"):
        Device()
