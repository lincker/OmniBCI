"""Actuator: plugin loading via registry/factory, lifecycle, error propagation."""
import pytest

from omnibci.domain.ports.effector import Effector
from omnibci.infrastructure.actuation.actuator import Actuator


class _FakeEffector(Effector):
    plugin_type = "fake_for_actuator_test"

    def __init__(self):
        self.calls = []

    def setup(self):
        self.calls.append("setup")

    def teardown(self):
        self.calls.append("teardown")

    def play(self, command):
        self.calls.append(("play", dict(command)))


class _ExplodingEffector(Effector):
    plugin_type = "exploding_for_actuator_test"

    def setup(self):
        pass

    def teardown(self):
        pass

    def play(self, command):
        raise RuntimeError("device jam")


def _actuator_with(effector) -> Actuator:
    a = Actuator()
    a.effector = effector
    return a


class TestExecute:
    def test_execute_passes_command_through(self):
        eff = _FakeEffector()
        a = _actuator_with(eff)
        a.execute({"target_id": 3, "event_id": 7, "confidence": 0.9})
        assert ("play", {"target_id": 3, "event_id": 7, "confidence": 0.9}) in eff.calls

    def test_execute_before_setup_raises(self):
        with pytest.raises(RuntimeError):
            Actuator().execute({"target_id": 1})

    def test_play_exception_propagates(self):
        a = _actuator_with(_ExplodingEffector())
        with pytest.raises(RuntimeError, match="device jam"):
            a.execute({"target_id": 1})


class TestLifecycle:
    def test_setup_without_configured_effector_raises(self, monkeypatch):
        a = Actuator()
        monkeypatch.setattr(a, "_load_effector", lambda: None)
        with pytest.raises(RuntimeError, match="No Effector plugin"):
            a.setup()

    def test_setup_calls_effector_setup(self, monkeypatch):
        eff = _FakeEffector()
        a = Actuator()
        monkeypatch.setattr(a, "_load_effector", lambda: eff)
        a.setup()
        assert eff.calls == ["setup"]

    def test_failed_setup_rolls_back_and_clears_effector(self, monkeypatch):
        class _PartiallyInitialisedEffector(_FakeEffector):
            plugin_type = "partial_setup_for_actuator_test"

            def setup(self):
                self.calls.append("setup")
                raise RuntimeError("device acquisition failed")

        eff = _PartiallyInitialisedEffector()
        a = Actuator()
        monkeypatch.setattr(a, "_load_effector", lambda: eff)

        with pytest.raises(RuntimeError, match="device acquisition failed"):
            a.setup()

        assert eff.calls == ["setup", "teardown"]
        assert a.effector is None

    def test_failed_setup_preserves_original_error_when_rollback_fails(
        self, monkeypatch
    ):
        class _BrokenRollbackEffector(_FakeEffector):
            plugin_type = "broken_setup_rollback_for_actuator_test"

            def setup(self):
                raise RuntimeError("original setup failure")

            def teardown(self):
                raise ValueError("secondary teardown failure")

        a = Actuator()
        monkeypatch.setattr(a, "_load_effector", _BrokenRollbackEffector)

        with pytest.raises(RuntimeError, match="original setup failure"):
            a.setup()

        assert a.effector is None

    def test_teardown_releases_and_clears(self):
        eff = _FakeEffector()
        a = _actuator_with(eff)
        a.teardown()
        assert "teardown" in eff.calls and a.effector is None

    def test_teardown_swallows_effector_error(self):
        class _BadTeardown(_FakeEffector):
            plugin_type = "bad_teardown_for_actuator_test"
            def teardown(self):
                raise RuntimeError("stuck valve")
        a = _actuator_with(_BadTeardown())
        a.teardown()               # must not raise
        assert a.effector is None

    def test_registry_loading_via_factory(self):
        # Register the fake in the real registry, then load through the same
        # factory path Actuator uses (ObjectFactory.create by plugin_type).
        from omnibci.infrastructure.plugins.registry import register_plugin
        from omnibci.infrastructure.plugins.factory import ObjectFactory
        register_plugin(Effector)(_FakeEffector)
        instance = ObjectFactory.create(Effector, "fake_for_actuator_test")
        assert isinstance(instance, _FakeEffector)
