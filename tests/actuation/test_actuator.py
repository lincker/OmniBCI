"""Actuator descriptor loading, lifecycle and error propagation."""
from textwrap import dedent
from pathlib import Path
import json

import pytest

from omnibci.domain.ports.effector import Effector
from omnibci.infrastructure.actuation.actuator import Actuator


COMMAND_SCHEMA = json.loads((Path(__file__).parents[2] /
    "omnibci/infrastructure/plugins/effectors/logger/command.schema.json").read_text())


class _FakeEffector(Effector):
    plugin_type = "fake_for_actuator_test"

    def __init__(self):
        self.calls = []

    def setup(self):
        self.calls.append("setup")

    def teardown(self):
        self.calls.append("teardown")

    def execute(self, command):
        self.calls.append(("execute", dict(command)))

    def quiesce(self):
        self.calls.append("quiesce")


class _ExplodingEffector(Effector):
    plugin_type = "exploding_for_actuator_test"

    def setup(self):
        pass

    def teardown(self):
        pass

    def execute(self, command):
        raise RuntimeError("device jam")

    def quiesce(self):
        pass


def _actuator_with(effector) -> Actuator:
    a = Actuator(settings_snapshot={})
    a.effector = effector
    a._command_schema = COMMAND_SCHEMA
    return a


@pytest.mark.parametrize("operation", ["setup", "execute", "quiesce", "teardown"])
def test_void_lifecycle_results_do_not_accept_false_as_success(monkeypatch, operation):
    effector = _FakeEffector()
    monkeypatch.setattr(effector, operation, lambda *args: False)
    actuator = Actuator(settings_snapshot={})
    actuator._command_schema = COMMAND_SCHEMA
    monkeypatch.setattr(actuator, "_load_effector", lambda: effector)
    if operation != "setup":
        actuator.setup()
    arguments = ({"target_id": 1},) if operation == "execute" else ()
    with pytest.raises(TypeError, match=operation):
        getattr(actuator, operation)(*arguments)


class TestExecute:
    def test_execute_passes_command_through(self):
        eff = _FakeEffector()
        a = _actuator_with(eff)
        a.execute({"target_id": 3, "event_id": 7, "confidence": 0.9})
        assert ("execute", {"target_id": 3, "event_id": 7, "confidence": 0.9}) in eff.calls

    def test_execute_before_setup_raises(self):
        with pytest.raises(RuntimeError):
            Actuator(settings_snapshot={}).execute({"target_id": 1})

    def test_execute_exception_propagates(self):
        a = _actuator_with(_ExplodingEffector())
        with pytest.raises(RuntimeError, match="device jam"):
            a.execute({"target_id": 1})


class TestLifecycle:
    def test_user_effector_descriptor_drives_full_lifecycle(self, monkeypatch, tmp_path):
        plugin_root = tmp_path / "plugins"
        package_dir = tmp_path / "user-device"
        package_dir.mkdir(parents=True)
        (package_dir / "manifest.toml").write_text(
            dedent(
                """
                manifest_version = 1
                [package]
                id = "user-device"
                publisher = "alice"
                version = "1.0.0"
                domain = "actuation"
                api_version = "1"
                [[contributions]]
                id = "default"
                kind = "effector"
                plugin_type = "user_device"
                entrypoint = "driver:UserDevice"
                display_name = "User Device"
                [contributions.payload_schemas]
                command = "command.schema.json"
                [contributions.configuration]
                schema = "schema.json"
                schema_version = 1
                """
            ),
            encoding="utf-8",
        )
        (package_dir / "command.schema.json").write_text('{"type":"object","properties":{"target_id":{"type":"integer"}},"required":["target_id"],"additionalProperties":false}')
        (package_dir / "schema.json").write_text('{"type":"object","properties":{},"additionalProperties":false}')
        (package_dir / "driver.py").write_text(
            dedent(
                """
                from omnibci.domain.ports.effector import Effector

                class UserDevice(Effector):
                    plugin_type = "user_device"

                    def __init__(self, *, parameters, resource_root):
                        self.calls = []

                    def setup(self):
                        self.calls.append("setup")

                    def execute(self, command):
                        self.calls.append(("execute", dict(command)))

                    def quiesce(self):
                        self.calls.append("quiesce")

                    def teardown(self):
                        self.calls.append("teardown")
                """
            ),
            encoding="utf-8",
        )
        monkeypatch.setenv("OMNIBCI_PLUGIN_DIR", str(plugin_root))
        from tests.plugin_configuration_helpers import install_package_root, prepared_snapshot
        install_package_root(package_dir)
        from omnibci.application.configuration.models import ProfileBinding
        from omnibci.infrastructure.plugin_management.catalog import PluginCatalog
        descriptor = next(item for item in PluginCatalog().list_contributions("effector") if item.plugin_type == "user_device")
        binding = ProfileBinding("actuation.effector", "effector", None, descriptor.selection, {})
        actuator = Actuator(settings_snapshot=prepared_snapshot({"PLUGINS.bindings": [binding.to_dict()]}))
        actuator.setup()
        loaded = actuator.effector
        actuator.execute({"target_id": 7})
        actuator.teardown()

        assert loaded is not None
        assert loaded.calls == ["setup", ("execute", {"target_id": 7}), "teardown"]
        assert actuator.effector is None

    def test_setup_without_configured_effector_raises(self, monkeypatch):
        a = Actuator(settings_snapshot={})
        monkeypatch.setattr(a, "_load_effector", lambda: None)
        with pytest.raises(RuntimeError, match="No Effector plugin"):
            a.setup()

    def test_setup_calls_effector_setup(self, monkeypatch):
        eff = _FakeEffector()
        a = Actuator(settings_snapshot={})
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
        a = Actuator(settings_snapshot={})
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

        a = Actuator(settings_snapshot={})
        effector = _BrokenRollbackEffector()
        monkeypatch.setattr(a, "_load_effector", lambda: effector)

        with pytest.raises(RuntimeError, match="original setup failure") as raised:
            a.setup()

        assert isinstance(raised.value.__cause__, ValueError)
        assert a.effector is effector

    def test_teardown_releases_and_clears(self):
        eff = _FakeEffector()
        a = _actuator_with(eff)
        a.teardown()
        assert "teardown" in eff.calls and a.effector is None

    def test_failed_teardown_retains_ownership_until_success(self):
        class _BadTeardown(_FakeEffector):
            plugin_type = "bad_teardown_for_actuator_test"

            def teardown(self):
                self.calls.append("teardown")
                if len(self.calls) == 1:
                    raise RuntimeError("stuck valve")

        effector = _BadTeardown()
        a = _actuator_with(effector)
        with pytest.raises(RuntimeError, match="stuck valve"):
            a.teardown()
        assert a.effector is effector
        with pytest.raises(RuntimeError, match="still owns resources"):
            a.setup()
        a.teardown()
        assert a.effector is None
