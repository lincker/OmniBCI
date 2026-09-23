"""Managed child startup transports a detached snapshot before runtime imports."""

import builtins
import pickle
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import omnibci.conf
from omnibci.infrastructure.actuation import process_manager as actuation
from omnibci.infrastructure.stimulation import process_manager as stimulation
from omnibci.infrastructure.process_lifecycle import StartupReport


MANAGERS = (
    (stimulation, stimulation.StimulationProcessManager, "Stimulation", "STIMULATION.paradigm"),
    (actuation, actuation.ActuationProcessManager, "Actuation", "ACTUATION.effector"),
)


def _snapshot(key, revision="revision-1"):
    return {
        key: "selected-plugin",
        "CONFIG.revision": revision,
        "CONFIG.generation": "graph-1",
        "STIMULATION.auto_spawn": True,
        "ACTUATION.enabled": True,
        "ACTUATION.auto_spawn": True,
        "PLUGINS.bindings": [{"parameters": {"level": 1}}],
    }


def _mock_spawning(monkeypatch, module, component):
    receiver = Mock()
    receiver.poll.return_value = True
    receiver.recv.return_value = StartupReport.ready(component)
    monkeypatch.setattr(module.mp, "Pipe", Mock(return_value=(receiver, Mock())))
    monkeypatch.setattr(module.mp, "Event", Mock())

    def process(**kwargs):
        snapshot = kwargs.get("kwargs", {}).get("settings_snapshot", {})
        receiver.recv.return_value = StartupReport.ready(
            component, config_revision=snapshot.get("CONFIG.revision"),
            generation=snapshot.get("CONFIG.generation"),
        )
        child = Mock(pid=123, exitcode=0)
        child.is_alive.return_value = True
        child.join.side_effect = lambda **kwargs: setattr(child.is_alive, "return_value", False)
        return child

    factory = Mock(side_effect=process)
    monkeypatch.setattr(module.mp, "Process", factory)
    return factory


@pytest.mark.parametrize(("module", "manager_class", "component", "key"), MANAGERS)
def test_snapshot_transport_and_revision_restart(monkeypatch, module, manager_class, component, key):
    factory = _mock_spawning(monkeypatch, module, component)
    manager = manager_class(auto_spawn=False)
    saved = _snapshot(key)
    original = deepcopy(saved)
    manager.start(settings_snapshot=saved)
    manager.start(settings_snapshot=deepcopy(saved))
    assert factory.call_count == 1
    spawned = factory.call_args.kwargs
    assert spawned["args"][0] == "selected-plugin"
    assert pickle.loads(pickle.dumps(spawned["kwargs"])) == {"settings_snapshot": original}

    saved["CONFIG.revision"] = "revision-2"
    saved["PLUGINS.bindings"][0]["parameters"]["level"] = 2
    assert spawned["kwargs"]["settings_snapshot"] == original
    manager.start(settings_snapshot=saved)

    assert factory.call_count == 2
    assert manager.is_ready
    assert factory.call_args.kwargs["kwargs"]["settings_snapshot"]["CONFIG.revision"] == "revision-2"


@pytest.mark.parametrize(("module", "manager_class", "component", "key"), MANAGERS)
def test_snapshot_rejects_conflicting_legacy_override_before_spawn(
    monkeypatch, module, manager_class, component, key,
):
    factory = _mock_spawning(monkeypatch, module, component)
    manager = manager_class()

    with pytest.raises(RuntimeError, match="Apply the .* configuration"):
        manager.start("other-plugin", settings_snapshot=_snapshot(key))

    factory.assert_not_called()


@pytest.mark.parametrize("disabled_key", ["ACTUATION.enabled", "ACTUATION.auto_spawn"])
def test_actuation_snapshot_can_disable_and_reenable_owned_child(monkeypatch, disabled_key):
    factory = _mock_spawning(monkeypatch, actuation, "Actuation")
    manager = actuation.ActuationProcessManager(auto_spawn=False)
    snapshot = _snapshot("ACTUATION.effector")
    manager.start(settings_snapshot=snapshot)
    child = manager._process

    snapshot[disabled_key] = False
    manager.start(settings_snapshot=snapshot)

    child.join.assert_called_once()
    assert not manager.is_alive
    assert not manager.auto_spawn
    assert factory.call_count == 1

    snapshot[disabled_key] = True
    manager.start(settings_snapshot=snapshot)
    assert manager.auto_spawn
    assert factory.call_count == 2


def test_stimulation_snapshot_can_disable_owned_child(monkeypatch):
    factory = _mock_spawning(monkeypatch, stimulation, "Stimulation")
    manager = stimulation.StimulationProcessManager()
    snapshot = _snapshot("STIMULATION.paradigm")
    manager.start(settings_snapshot=snapshot)
    snapshot["STIMULATION.auto_spawn"] = False

    manager.start(settings_snapshot=snapshot)

    assert not manager.is_alive
    assert not manager.auto_spawn
    assert factory.call_count == 1


@pytest.mark.parametrize(("module", "manager_class", "component", "key"), MANAGERS)
def test_child_installs_snapshot_before_importing_application(
    monkeypatch, module, manager_class, component, key,
):
    settings = Mock()
    monkeypatch.setattr(omnibci.conf, "settings", settings)
    application = Mock()
    original_import = builtins.__import__
    snapshot = _snapshot(key)
    app_module = module.__name__.removesuffix("process_manager") + "application"

    def import_after_snapshot(name, *args, **kwargs):
        if name == app_module:
            settings.use_snapshot.assert_called_once_with(snapshot)
            return SimpleNamespace(**{component: application})
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", import_after_snapshot)
    entrypoint = getattr(module, f"_run_{component.lower()}")
    entrypoint(settings_snapshot=snapshot)

    application.assert_called_once_with(stop_event=None)
    application.return_value.run.assert_called_once_with()
    settings.set.assert_not_called()
