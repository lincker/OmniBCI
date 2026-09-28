"""Managed peer readiness belongs to its pinned configuration and generation."""

from importlib import import_module
from unittest.mock import MagicMock

import pytest

from omnibci.infrastructure.process_lifecycle import (
    ChildProcessStartupError, ChildProcessState, StartupReport,
)


@pytest.fixture(params=[("stimulation", "Stimulation", "paradigm"),
                       ("actuation", "Actuation", "effector")])
def peer(request):
    kind, component, selection = request.param
    module = import_module(f"omnibci.infrastructure.{kind}.process_manager")
    snapshot = {"CONFIG.revision": "revision", "CONFIG.generation": "run-2",
                f"{kind.upper()}.{selection}": "selected",
                f"{kind.upper()}.enabled": True, f"{kind.upper()}.auto_spawn": True}
    return kind, component, module, snapshot


@pytest.mark.parametrize("outcome", ["ready", "failure"])
def test_entrypoint_report_preserves_snapshot_identity(peer, monkeypatch, outcome):
    kind, component, module, snapshot = peer
    app_module = import_module(f"omnibci.infrastructure.{kind}.application")
    connection = MagicMock()
    runtime = MagicMock()

    def construct(**kwargs):
        if outcome == "failure":
            raise ValueError("initialization failed")
        runtime.run.side_effect = kwargs["startup_reporter"].ready
        return runtime

    monkeypatch.setattr(app_module, component, construct)
    from omnibci.conf import settings
    monkeypatch.setattr(settings, "use_snapshot", MagicMock())
    entrypoint = getattr(module, f"_run_{kind}")
    if outcome == "failure":
        with pytest.raises(ValueError):
            entrypoint(startup_connection=connection, settings_snapshot=snapshot)
    else:
        entrypoint(startup_connection=connection, settings_snapshot=snapshot)
    report = connection.send.call_args.args[0]
    assert report.config_revision == "revision"
    assert report.generation == "run-2"
    assert report.state is (ChildProcessState.READY if outcome == "ready" else ChildProcessState.FAILED)
    connection.close.assert_called_once()


@pytest.mark.parametrize("revision,generation", [
    ("revision", "run-2"), ("old", "run-2"), ("revision", "run-1"),
])
def test_manager_requires_exact_snapshot_ack(peer, monkeypatch, revision, generation):
    _, component, module, snapshot = peer
    process = MagicMock()
    process.is_alive.return_value = True
    process.join.side_effect = lambda **kwargs: setattr(process.is_alive, "return_value", False)
    receiver, sender = MagicMock(), MagicMock()
    receiver.poll.return_value = True
    receiver.recv.return_value = StartupReport.ready(
        component, config_revision=revision, generation=generation,
    )
    monkeypatch.setattr(module.mp, "Process", MagicMock(return_value=process))
    monkeypatch.setattr(module.mp, "Event", MagicMock())
    monkeypatch.setattr(module.mp, "Pipe", MagicMock(return_value=(receiver, sender)))
    manager = getattr(module, f"{component}ProcessManager")()
    if (revision, generation) == ("revision", "run-2"):
        manager.start(settings_snapshot=snapshot)
        assert manager.is_ready
        manager.stop()
    else:
        with pytest.raises(ChildProcessStartupError, match="mismatch"):
            manager.start(settings_snapshot=snapshot)
        assert manager.state is ChildProcessState.FAILED
        assert manager._process is None
    sender.close.assert_called_once()
    receiver.close.assert_called_once()
