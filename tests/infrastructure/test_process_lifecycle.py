"""Local READY/FAILED child-process control protocol."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from omnibci.infrastructure.process_lifecycle import (
    ChildProcessStartupError,
    ChildProcessStartupTimeout,
    ChildProcessState,
    StartupReport,
    StartupReporter,
    wait_for_startup_report,
)


def _process(*, alive: bool = True, exitcode: int | None = None):
    process = MagicMock()
    process.is_alive.return_value = alive
    process.exitcode = exitcode
    return process


def test_reporter_sends_exactly_one_terminal_result_and_closes() -> None:
    connection = MagicMock()
    reporter = StartupReporter(connection, component="Actuation")

    assert reporter.ready() is True
    assert reporter.failed("late", RuntimeError("ignored")) is False

    connection.send.assert_called_once_with(StartupReport.ready("Actuation"))
    connection.close.assert_called_once_with()
    assert reporter.reported is True


def test_reporter_flattens_unpickleable_exception_to_text() -> None:
    connection = MagicMock()
    reporter = StartupReporter(connection, component="Stimulation")

    reporter.failed("renderer", ValueError("no display"))

    report = connection.send.call_args.args[0]
    assert report == StartupReport(
        component="Stimulation",
        state=ChildProcessState.FAILED,
        stage="renderer",
        error_type="ValueError",
        message="no display",
    )


def test_wait_accepts_ready_only_while_child_is_alive() -> None:
    receiver = MagicMock()
    receiver.poll.return_value = True
    receiver.recv.return_value = StartupReport.ready("Actuation")
    process = _process(alive=True)

    report = wait_for_startup_report(
        receiver,
        process,
        component="Actuation",
        timeout_s=1.0,
    )

    assert report.state is ChildProcessState.READY


def test_wait_surfaces_child_failure_details() -> None:
    receiver = MagicMock()
    receiver.poll.return_value = True
    receiver.recv.return_value = StartupReport.failed(
        "Actuation",
        stage="initialization",
        error=RuntimeError("device busy"),
    )

    with pytest.raises(ChildProcessStartupError, match="device busy") as raised:
        wait_for_startup_report(
            receiver,
            _process(),
            component="Actuation",
            timeout_s=1.0,
        )

    assert raised.value.report is receiver.recv.return_value


def test_wait_detects_exit_before_report() -> None:
    receiver = MagicMock()
    receiver.poll.return_value = False
    process = _process(alive=False, exitcode=7)

    with pytest.raises(
        ChildProcessStartupError,
        match=r"exitcode=7",
    ):
        wait_for_startup_report(
            receiver,
            process,
            component="Stimulation",
            timeout_s=1.0,
        )

    process.join.assert_called_once_with(timeout=0)


def test_wait_times_out_boundedly() -> None:
    receiver = MagicMock()
    receiver.poll.return_value = False

    with pytest.raises(ChildProcessStartupTimeout, match="within"):
        wait_for_startup_report(
            receiver,
            _process(alive=True),
            component="Stimulation",
            timeout_s=0.001,
        )


def test_reporter_preserves_configuration_identity() -> None:
    connection = MagicMock()
    reporter = StartupReporter(connection, component="Acquisition",
                               module_revisions={"acquisition": "revision"}, engine_revision="engine", generation="run-2")
    reporter.ready()
    report = connection.send.call_args.args[0]
    assert report.module_revisions == {"acquisition": "revision"}
    assert report.engine_revision == "engine"
    assert report.generation == "run-2"


@pytest.mark.parametrize("revision,generation,engine_revision", [("old", "run-2", "engine"), ("revision", "run-1", "engine"), ("revision", "run-2", "old-engine"), (None, None, None)])
def test_wait_rejects_stale_or_unidentified_report(revision, generation, engine_revision) -> None:
    receiver = MagicMock()
    receiver.poll.return_value = True
    receiver.recv.return_value = StartupReport.ready(
        "Acquisition", module_revisions={"acquisition": revision}, engine_revision=engine_revision, generation=generation,
    )
    with pytest.raises(ChildProcessStartupError, match="mismatch"):
        wait_for_startup_report(receiver, _process(), component="Acquisition",
                                timeout_s=1, expected_module_revisions={"acquisition": "revision"}, expected_engine_revision="engine",
                                expected_generation="run-2")


@pytest.mark.parametrize("timeout", [0, -1, True, float("nan"), float("inf")])
def test_wait_rejects_invalid_deadlines(timeout) -> None:
    with pytest.raises(ValueError, match="timeout"):
        wait_for_startup_report(MagicMock(), _process(), component="Acquisition",
                                timeout_s=timeout)


@pytest.mark.parametrize("report", [
    "ready", StartupReport.ready("other"),
    StartupReport("Acquisition", ChildProcessState.STARTING, "plugins"),
])
def test_wait_rejects_invalid_acknowledgements(report) -> None:
    receiver = MagicMock()
    receiver.poll.return_value = True
    receiver.recv.return_value = report
    with pytest.raises(ChildProcessStartupError):
        wait_for_startup_report(receiver, _process(), component="Acquisition", timeout_s=1)


@pytest.fixture(params=[("stimulation", "Stimulation", "paradigm"),
                        ("actuation", "Actuation", "effector")])
def peer(request):
    from importlib import import_module
    from omnibci.application.configuration.models import ProfileBinding
    from omnibci.infrastructure.plugin_management.catalog import PluginCatalog
    from tests.plugin_configuration_helpers import builtin_selection, prepared_snapshot

    kind, component, selection = request.param
    module = import_module(f"omnibci.infrastructure.{kind}.process_manager")
    alias = "STIMULATION_THUBCI_SSVEP_classic40" if kind == "stimulation" else "logger"
    descriptor = PluginCatalog().resolve_selection(selection, builtin_selection(selection, alias))
    snapshot = {
        "CONFIG.module_revisions": {"acquisition": "a" * 64, "processing": "a" * 64, "stimulation": "a" * 64, "actuation": "a" * 64},
        "CONFIG.engine_revision": "c" * 64,
        "CONFIG.generation": "run-2",
        "PLUGINS.bindings": [ProfileBinding(
            f"{kind}.{selection}", selection, None, descriptor.selection, {},
        ).to_dict()],
        f"{kind.upper()}.enabled": True,
        f"{kind.upper()}.auto_spawn": True,
    }
    return kind, component, module, prepared_snapshot(snapshot)


@pytest.mark.parametrize("outcome", ["ready", "failure"])
def test_entrypoint_report_preserves_snapshot_identity(peer, monkeypatch, outcome):
    from importlib import import_module

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
    entrypoint = getattr(module, f"_run_{kind}")
    if outcome == "failure":
        with pytest.raises(ValueError):
            entrypoint(startup_connection=connection, settings_snapshot=snapshot)
    else:
        entrypoint(startup_connection=connection, settings_snapshot=snapshot)
    report = connection.send.call_args.args[0]
    assert report.module_revisions == {kind: "a" * 64}
    assert report.engine_revision == "c" * 64
    assert report.generation == "run-2"
    assert report.state is (ChildProcessState.READY if outcome == "ready" else ChildProcessState.FAILED)
    connection.close.assert_called_once()


@pytest.mark.parametrize("revision,generation", [
    ("a" * 64, "run-2"), ("old", "run-2"), ("a" * 64, "run-1"),
])
def test_manager_requires_exact_snapshot_ack(peer, monkeypatch, revision, generation):
    from dataclasses import replace
    from omnibci.infrastructure.plugin_management.preparation import execution_receipt

    kind, component, module, snapshot = peer
    process = MagicMock()
    process.exitcode = 0
    process.is_alive.return_value = True
    process.join.side_effect = lambda **kwargs: setattr(process.is_alive, "return_value", False)
    receiver, sender = MagicMock(), MagicMock()
    receiver.poll.return_value = True
    receiver.recv.return_value = StartupReport.ready(
        component, module_revisions={kind: revision}, engine_revision="c" * 64, generation=generation,
    )

    def spawn(**kwargs):
        source = kwargs["kwargs"]["settings_snapshot"]
        receiver.recv.return_value = replace(
            receiver.recv.return_value,
            execution=execution_receipt(source, kind, source["CONFIG.operation_id"]),
        )
        return process

    monkeypatch.setattr(module.mp, "Process", MagicMock(side_effect=spawn))
    monkeypatch.setattr(module.mp, "Event", MagicMock())
    monkeypatch.setattr(module.mp, "Pipe", MagicMock(return_value=(receiver, sender)))
    manager = getattr(module, f"{component}ProcessManager")()
    if (revision, generation) == ("a" * 64, "run-2"):
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


@pytest.mark.parametrize("component", ["Actuation", "Stimulation"])
def test_desktop_output_retains_live_child_without_termination(monkeypatch, component):
    from omnibci.infrastructure.actuation import process_manager as actuation
    from omnibci.infrastructure.plugin_management.preparation import execution_receipt
    from omnibci.infrastructure.stimulation import process_manager as stimulation
    from tests.infrastructure.test_child_runtime_snapshot import _snapshot

    module, manager_class = (
        (actuation, actuation.ActuationProcessManager)
        if component == "Actuation"
        else (stimulation, stimulation.StimulationProcessManager)
    )
    process = MagicMock()
    process.is_alive.return_value = True
    receiver, sender = MagicMock(), MagicMock()
    receiver.poll.return_value = True

    def spawn(**kwargs):
        source = kwargs["kwargs"]["settings_snapshot"]
        receiver.recv.return_value = StartupReport.ready(
            component,
            module_revisions={component.lower(): source["CONFIG.module_revisions"][component.lower()]},
            engine_revision=source["CONFIG.engine_revision"],
            generation=source["CONFIG.generation"],
            execution=execution_receipt(source, component.lower(), source["CONFIG.operation_id"]),
        )
        return process

    process_factory = MagicMock(side_effect=spawn)
    monkeypatch.setattr(module.mp, "Process", process_factory)
    monkeypatch.setattr(module.mp, "Event", MagicMock())
    monkeypatch.setattr(module.mp, "Pipe", MagicMock(return_value=(receiver, sender)))
    manager = manager_class(allow_process_termination=False)
    key = "STIMULATION.paradigm" if component == "Stimulation" else "ACTUATION.effector"
    snapshot = _snapshot(key)
    manager.start(settings_snapshot=snapshot)
    assert process_factory.call_args.kwargs["daemon"] is False

    with pytest.raises(RuntimeError, match="did not stop gracefully"):
        manager.stop()
    assert manager._process is process
    assert manager.state is ChildProcessState.FAILED
    manager._stop_event.set.assert_called_once()
    process.terminate.assert_not_called()
    process.kill.assert_not_called()
    process.close.assert_not_called()
    with pytest.raises(RuntimeError, match="did not stop gracefully"):
        manager.start(settings_snapshot=snapshot)
    assert process_factory.call_count == 1

    process.is_alive.return_value = False
    process.exitcode = 0
    manager.stop()
    assert manager._process is None
    process.close.assert_called_once()


@pytest.mark.asyncio
async def test_desktop_engine_attempts_both_workers_and_retains_live_handles():
    from unittest.mock import AsyncMock
    from omnibci.infrastructure.neuroengine.engine import NeuroEngine

    engine = NeuroEngine(
        event_bus=MagicMock(stop=AsyncMock()), data_manager=MagicMock(),
        acquisition_worker=MagicMock(), acquisition_command_queue=MagicMock(),
        signal_processing_worker=MagicMock(), processor_command_queue=MagicMock(),
        allow_process_termination=False,
    )
    engine._acquisition_started = engine._processor_started = True
    engine._event_bus_started = True
    for worker in (engine.acquisition_worker, engine.signal_processing_worker):
        worker.is_alive.return_value = True
    with pytest.raises(ExceptionGroup) as error:
        await engine._stop()
    assert len(error.value.exceptions) == 2
    for worker in (engine.acquisition_worker, engine.signal_processing_worker):
        worker.join.assert_called_once_with(timeout=3)
        worker.terminate.assert_not_called()
        worker.kill.assert_not_called()
        worker.close.assert_not_called()
    engine.acquisition_command_queue.put_nowait.assert_called_once()
    engine.processor_command_queue.put_nowait.assert_called_once()
    engine.acquisition_command_queue.close.assert_not_called()
    engine.processor_command_queue.close.assert_not_called()
    engine.event_bus.stop.assert_awaited_once()
    assert engine.shutdown_complete is False


@pytest.mark.asyncio
async def test_repeated_shutdown_does_not_ignore_a_retained_failed_graph():
    from omnibci.application.services.engine_service import NeuroEngineService

    service = NeuroEngineService(engine_factory=MagicMock())
    engine = MagicMock(shutdown_complete=False)
    service._engine = engine
    service._restart_blocked = True
    service._running = False
    service._engine_thread = None
    with pytest.raises(RuntimeError, match="unreleased resources"):
        await service.shutdown()
    assert service._engine is engine
    assert service._restart_blocked is True
