"""Managed child startup transports a detached snapshot before runtime imports."""

import builtins
import multiprocessing as mp
import os
import pickle
import subprocess
import sys
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import omnibci.conf
from omnibci.infrastructure.actuation import process_manager as actuation
from omnibci.infrastructure.stimulation import process_manager as stimulation
from omnibci.infrastructure.process_lifecycle import StartupReport, StartupReporter


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

    application.assert_called_once_with(stop_event=None, settings_snapshot=snapshot)
    application.return_value.run.assert_called_once_with()
    settings.set.assert_not_called()


def _report_plugin_directory(connection):
    with connection:
        connection.send(os.environ.get("OMNIBCI_PLUGIN_DIR"))


def _ready_with_current_plugin_directory(selected, stop_event, connection, *, settings_snapshot):
    reporter = StartupReporter(
        connection, component=settings_snapshot["TEST.component"],
        config_revision=settings_snapshot["CONFIG.revision"],
        generation=settings_snapshot["CONFIG.generation"],
    )
    if os.environ.get("OMNIBCI_PLUGIN_DIR") != settings_snapshot["TEST.plugin_dir"]:
        reporter.failed("environment", RuntimeError("Child inherited an outdated plugin directory"))
        return
    reporter.ready()
    stop_event.wait(10)


def _check_current_plugin_directory(component, directory):
    # This entry point runs only in a disposable interpreter, never in pytest.
    mp.set_start_method("forkserver")
    os.environ["OMNIBCI_PLUGIN_DIR"] = directory + "/previous"
    receiver, sender = mp.Pipe(duplex=False)
    warmup = mp.Process(target=_report_plugin_directory, args=(sender,))
    try:
        warmup.start()
        sender.close()
        assert receiver.poll(10), "Forkserver warmup did not report its environment"
        assert receiver.recv() == directory + "/previous"
        warmup.join(10)
        assert warmup.exitcode == 0
    finally:
        if warmup.is_alive():
            warmup.terminate()
            warmup.join(5)
        warmup.close()
        receiver.close()
        sender.close()

    current = directory + "/current"
    os.environ["OMNIBCI_PLUGIN_DIR"] = current
    module, manager_class, _, key = next(item for item in MANAGERS if item[2] == component)
    setattr(module, f"_run_{component.lower()}", _ready_with_current_plugin_directory)
    snapshot = {**_snapshot(key), "TEST.component": component, "TEST.plugin_dir": current}
    manager = manager_class()
    try:
        manager.start(settings_snapshot=snapshot)
        assert manager.is_ready
    finally:
        manager.stop()


@pytest.mark.integration
@pytest.mark.skipif("forkserver" not in mp.get_all_start_methods(), reason="Requires forkserver")
@pytest.mark.parametrize("component", ["Actuation", "Stimulation"])
def test_managed_child_uses_current_plugin_directory_after_forkserver_start(component, tmp_path):
    result = subprocess.run(
        [sys.executable, __file__, component, str(tmp_path)],
        capture_output=True, text=True, timeout=40,
    )
    assert result.returncode == 0, result.stdout + result.stderr


if __name__ == "__main__":
    _check_current_plugin_directory(sys.argv[1], sys.argv[2])
