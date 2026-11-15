"""
Unit tests for NeuroEngineService (application service).

NeuroEngine itself is fully mocked — no subprocesses are spawned.
"""
import pytest
import asyncio
import threading
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import MagicMock, patch, AsyncMock

from omnibci.application.commands.engine_commands import (
    StartEngineCommand,
    StopEngineCommand,
)
from omnibci.application.queries.engine_queries import GetEngineStatusQuery
from omnibci.application.services.engine_service import NeuroEngineService
from omnibci.application.events import CommandResult
from omnibci.application.plugin_profiles import ProfileRevisionConflict
from omnibci.application.services.command_result_service import CommandExecutionError, CommandResultBroker
from omnibci.shared.events import Event, EventType


class TestNeuroEngineServiceStatus:
    def test_status_before_init_returns_all_false(self):
        svc = NeuroEngineService(engine_factory=MagicMock())
        result = svc.handle_get_status(GetEngineStatusQuery())
        assert result.running is False
        assert result.acquisition_alive is False
        assert result.processor_alive is False


class TestNeuroEngineServicePortState:
    @pytest.mark.parametrize("alive", [None, False, True])
    def test_actuation_process_liveness_is_independent_of_engine_readiness(self, engine_service, alive):
        engine_service._actuation_manager = None if alive is None else MagicMock(is_alive=alive)
        assert engine_service.actuation_process_alive is (alive is True)
        assert not engine_service.is_engine_ready

    def test_uninitialised_service_has_neither_lifecycle_capability(self):
        svc = NeuroEngineService(engine_factory=MagicMock())

        assert svc.is_initialized is False
        assert svc.is_engine_ready is False

    @pytest.mark.asyncio
    async def test_initialised_graph_is_not_yet_running_ready(self):
        svc = NeuroEngineService(engine_factory=MagicMock())
        await svc.startup()

        assert svc.is_initialized is True
        assert svc.is_engine_ready is False

    def test_dispatch_rejects_initialised_but_stopped_engine(
        self, engine_service, mock_engine
    ):
        with pytest.raises(RuntimeError, match="not running"):
            engine_service.dispatch(Event(EventType.StartStream))

        mock_engine.event_bus.put_event.assert_not_called()

    def test_dispatch_accepts_running_ready_engine(
        self, engine_service, mock_engine
    ):
        engine_service._running = True
        engine_service._accepting_commands = True
        engine_service._engine_thread = MagicMock()
        engine_service._engine_thread.is_alive.return_value = True
        event = Event(EventType.StartStream)

        engine_service.dispatch(event)

        mock_engine.event_bus.put_event.assert_called_once_with(event)

    def test_dead_engine_thread_is_not_running_ready(self, engine_service):
        engine_service._running = True
        engine_service._accepting_commands = True
        engine_service._engine_thread = MagicMock()
        engine_service._engine_thread.is_alive.return_value = False

        assert engine_service.is_engine_ready is False

    @pytest.mark.parametrize("worker", ["acquisition_worker", "signal_processing_worker"])
    def test_worker_exit_keeps_foundation_and_other_module_commands_available(
        self, engine_service, mock_engine, worker
    ):
        engine_service._running = True
        engine_service._accepting_commands = True
        engine_service._engine_thread = MagicMock()
        engine_service._engine_thread.is_alive.return_value = True
        broker = CommandResultBroker(engine_service, timeout_s=1)
        timers = []

        def dispatch(event):
            getattr(mock_engine, worker).is_alive.return_value = False
            timer = threading.Timer(0.15, broker.on_result, args=(Event(
                EventType.CommandResult, CommandResult(event.data.command_id, event.type.name, True),
            ),))
            timers.append(timer)
            timer.start()

        mock_engine.event_bus.put_event.side_effect = dispatch
        try:
            assert broker.request(EventType.StartStimTrial).ok
        finally:
            for timer in timers:
                timer.join()
        assert broker.pending_count == 0
        assert engine_service.is_engine_ready is True
        mock_engine.event_bus.put_event.assert_called_once()

    def test_closed_worker_handle_does_not_affect_foundation_readiness(self, engine_service, mock_engine):
        engine_service._running = True
        engine_service._accepting_commands = True
        engine_service._engine_thread = MagicMock()
        engine_service._engine_thread.is_alive.return_value = True
        mock_engine.signal_processing_worker.is_alive.side_effect = ValueError("closed")

        assert engine_service.is_engine_ready is True
        mock_engine.signal_processing_worker.is_alive.assert_not_called()

    @pytest.mark.parametrize("worker,event_type", [
        ("acquisition_worker", EventType.StartStream),
        ("signal_processing_worker", EventType.PrepareSignalProcessing),
    ])
    def test_command_waiter_reports_only_its_failed_module(
        self, engine_service, mock_engine, worker, event_type,
    ):
        engine_service._running = engine_service._accepting_commands = True
        engine_service._engine_thread = MagicMock()
        engine_service._engine_thread.is_alive.return_value = True
        getattr(mock_engine, worker).is_alive.return_value = True
        mock_engine.event_bus.put_event.side_effect = lambda event: setattr(
            getattr(mock_engine, worker).is_alive, "return_value", False,
        )
        broker = CommandResultBroker(engine_service, timeout_s=1)
        with pytest.raises(CommandExecutionError) as raised:
            broker.request(event_type)
        assert raised.value.result.error_code == "MODULE_STOPPED"
        assert broker.pending_count == 0
        assert engine_service.is_engine_ready

    @pytest.mark.parametrize("running", [False, True])
    def test_execution_failure_quarantines_only_a_running_graph(self, engine_service, mock_engine, running):
        engine_service._running = running
        engine_service._accepting_commands = running
        mock_engine.run.side_effect = RuntimeError("worker exited")

        engine_service._run_engine()

        assert not engine_service._accepting_commands
        assert engine_service.admission.recovery_required is running

    @pytest.mark.asyncio
    async def test_stop_closes_dispatch_gate_before_waiting_for_thread(
        self, engine_service, mock_engine
    ):
        join_entered = threading.Event()
        join_release = threading.Event()
        thread = MagicMock()

        def join(_timeout):
            join_entered.set()
            join_release.wait(timeout=1.0)

        thread.join.side_effect = join
        thread.is_alive.return_value = False
        engine_service._engine_thread = thread
        engine_service._running = True
        engine_service._accepting_commands = True

        stop_task = asyncio.create_task(
            engine_service.handle_stop_engine(StopEngineCommand())
        )
        assert await asyncio.to_thread(join_entered.wait, 1.0)

        assert engine_service.is_engine_ready is False
        with pytest.raises(RuntimeError, match="not running"):
            engine_service.dispatch(Event(EventType.StartStream))
        mock_engine.event_bus.put_event.assert_not_called()

        join_release.set()
        await stop_task

    def test_status_with_mock_engine(self, engine_service, mock_engine):
        engine_service._running = True
        engine_service._accepting_commands = True
        engine_service._engine_thread = MagicMock()
        engine_service._engine_thread.is_alive.return_value = True
        mock_engine.acquisition_worker.is_alive.return_value = True
        mock_engine.signal_processing_worker.is_alive.return_value = True
        result = engine_service.handle_get_status(GetEngineStatusQuery())
        assert result.running is True
        assert result.acquisition_alive is True
        assert result.processor_alive is True

    def test_status_when_processes_dead(self, engine_service, mock_engine):
        engine_service._running = True
        engine_service._accepting_commands = True
        engine_service._engine_thread = MagicMock()
        engine_service._engine_thread.is_alive.return_value = True
        mock_engine.acquisition_worker.is_alive.return_value = False
        mock_engine.signal_processing_worker.is_alive.return_value = False
        result = engine_service.handle_get_status(GetEngineStatusQuery())
        assert result.acquisition_alive is False
        assert result.processor_alive is False

    def test_dead_engine_thread_status_does_not_touch_closed_process_handles(
        self, engine_service, mock_engine
    ):
        engine_service._running = True
        engine_service._accepting_commands = True
        engine_service._engine_thread = MagicMock()
        engine_service._engine_thread.is_alive.return_value = False
        mock_engine.acquisition_worker.is_alive.side_effect = ValueError("closed")
        mock_engine.signal_processing_worker.is_alive.side_effect = ValueError("closed")

        result = engine_service.handle_get_status(GetEngineStatusQuery())

        assert result.running is False
        assert result.acquisition_alive is False
        assert result.processor_alive is False
        mock_engine.acquisition_worker.is_alive.assert_not_called()


class TestNeuroEngineServiceStart:
    @pytest.mark.asyncio
    async def test_start_sets_running(self, engine_service):
        with patch(
            "omnibci.application.services.engine_service.Thread"
        ) as mock_thread_cls:
            mock_thread = MagicMock()
            mock_thread_cls.return_value = mock_thread
            await engine_service.handle_start_engine(StartEngineCommand())
        assert engine_service._running is True
        assert engine_service._accepting_commands is True
        assert engine_service.is_engine_ready is True
        engine_service._engine.wait_until_started.assert_called_once_with(
            engine_service._STARTUP_TIMEOUT_S
        )

    @pytest.mark.asyncio
    async def test_start_twice_is_idempotent(self, engine_service):
        """Second StartEngineCommand is ignored when already running."""
        with patch(
            "omnibci.application.services.engine_service.Thread"
        ) as mock_thread_cls:
            mock_thread_cls.return_value = MagicMock()
            await engine_service.handle_start_engine(StartEngineCommand())
            thread_call_count = mock_thread_cls.call_count
            await engine_service.handle_start_engine(StartEngineCommand())
            assert mock_thread_cls.call_count == thread_call_count  # no new thread

    @pytest.mark.asyncio
    async def test_concurrent_start_creates_one_engine_thread(self, engine_service):
        entered = threading.Event()
        release = threading.Event()

        def wait_until_started(_timeout):
            entered.set()
            release.wait(timeout=1.0)

        engine_service._engine.wait_until_started.side_effect = wait_until_started
        first = asyncio.create_task(
            engine_service.handle_start_engine(StartEngineCommand())
        )
        assert await asyncio.to_thread(entered.wait, 1.0)
        second = asyncio.create_task(
            engine_service.handle_start_engine(StartEngineCommand())
        )
        release.set()
        await asyncio.gather(first, second)

        assert engine_service._engine.run.call_count == 1

    @pytest.mark.asyncio
    async def test_stop_waits_for_in_flight_start(self, engine_service):
        entered = threading.Event()
        release = threading.Event()

        def wait_until_started(_timeout):
            entered.set()
            release.wait(timeout=1.0)

        engine_service._engine.wait_until_started.side_effect = wait_until_started
        start_task = asyncio.create_task(
            engine_service.handle_start_engine(StartEngineCommand())
        )
        assert await asyncio.to_thread(entered.wait, 1.0)
        stop_task = asyncio.create_task(
            engine_service.handle_stop_engine(StopEngineCommand())
        )
        release.set()
        await asyncio.gather(start_task, stop_task)

        assert engine_service._running is False
        assert engine_service._needs_rebuild is True

    @pytest.mark.asyncio
    async def test_start_without_init_raises(self):
        svc = NeuroEngineService(engine_factory=MagicMock())
        # _engine is None — must raise
        with pytest.raises(RuntimeError, match="not initialised"):
            await svc.handle_start_engine(StartEngineCommand())

    @pytest.mark.asyncio
    async def test_startup_failure_is_reported_and_never_marks_running(
        self, engine_service
    ):
        engine_service._engine.wait_until_started.side_effect = RuntimeError(
            "bind failed"
        )

        with pytest.raises(RuntimeError, match="bind failed"):
            await engine_service.handle_start_engine(StartEngineCommand())

        assert engine_service._running is False
        assert engine_service._needs_rebuild is True

    @pytest.mark.asyncio
    async def test_startup_timeout_with_live_thread_blocks_second_graph(
        self, engine_service
    ):
        engine_service._engine.wait_until_started.side_effect = TimeoutError(
            "startup timed out"
        )
        thread = MagicMock()
        thread.is_alive.return_value = True

        with patch(
            "omnibci.application.services.engine_service.Thread",
            return_value=thread,
        ):
            with pytest.raises(TimeoutError, match="timed out"):
                await engine_service.handle_start_engine(StartEngineCommand())

        assert engine_service._restart_blocked is True
        assert engine_service.is_engine_ready is False
        with pytest.raises(RuntimeError, match="restart is blocked"):
            await engine_service.handle_start_engine(StartEngineCommand())

    @pytest.mark.asyncio
    async def test_actuation_failure_does_not_block_acquisition(
        self, engine_service
    ):
        manager = MagicMock()
        manager.start.side_effect = RuntimeError("effector unavailable")
        engine_service._actuation_manager = manager
        engine_service._graph_snapshot = {"ACTUATION.auto_spawn": True}
        thread = MagicMock()
        thread.is_alive.return_value = True

        with patch(
            "omnibci.application.services.engine_service.Thread",
            return_value=thread,
        ):
            await engine_service.handle_start_engine(StartEngineCommand())

        manager.start.assert_not_called()
        with pytest.raises(RuntimeError, match="effector unavailable"):
            await engine_service.start_module("actuation")
        manager.start.assert_called_once_with(settings_snapshot=engine_service.applied_snapshot)
        manager.stop.assert_not_called()
        assert engine_service.is_engine_ready

class TestNeuroEngineServiceStop:
    @pytest.mark.asyncio
    async def test_stop_clears_running(self, engine_service):
        engine_service._running = True
        engine_service._engine_thread = MagicMock()
        engine_service._engine_thread.is_alive.return_value = False
        await engine_service.handle_stop_engine(StopEngineCommand())
        assert engine_service._running is False

    @pytest.mark.asyncio
    async def test_stop_when_not_running_is_safe(self, engine_service):
        engine_service._running = False
        await engine_service.handle_stop_engine(StopEngineCommand())  # no exception

    @pytest.mark.asyncio
    async def test_hung_thread_retains_ownership_and_blocks_rebuild(
        self, engine_service
    ):
        engine_service._running = True
        engine_service._engine_thread = MagicMock()
        engine_service._engine_thread.is_alive.return_value = True

        with pytest.raises(RuntimeError, match="did not stop"):
            await engine_service.handle_stop_engine(StopEngineCommand())

        assert engine_service._running is True
        assert engine_service._accepting_commands is False
        assert engine_service.is_engine_ready is False
        assert engine_service._needs_rebuild is False
        assert engine_service._engine_thread is not None


@pytest.fixture
def running_modules(engine_service, mock_engine):
    engine_service._running = engine_service._accepting_commands = True
    engine_service._engine_thread = MagicMock()
    engine_service._engine_thread.is_alive.return_value = True
    engine_service._applied_snapshot = {
        "CONFIG.generation": "foundation",
        "CONFIG.system_revision": "system",
        "CONFIG.module_revisions": {module: module for module in (
            "acquisition", "processing", "stimulation", "actuation",
        )},
        "PLUGINS.bindings": [],
    }
    mock_engine.peer_gateway.peer.return_value.is_connected = False
    acquisition, commands, signal = MagicMock(), MagicMock(), MagicMock()
    acquisition.handle_get_status.return_value = SimpleNamespace(connected=True, streaming=False)
    calls = []
    acquisition.handle_stop_and_drain.side_effect = lambda: calls.append("drain")
    acquisition.handle_start_stream.side_effect = lambda cmd: calls.append("read")

    def request(event, payload=None):
        calls.append(event.name)
        data = {"generation": "foundation", "system_revision": "system"}
        if event is EventType.GetSourceCheckpoint:
            data.update(acquisition_revision="acquisition", supported=True, checkpoint={"cursor": 7})
        elif event is EventType.RestoreSourceCheckpoint:
            assert payload["checkpoint"] == {"cursor": 7}
            data.update(acquisition_revision="acquisition", restored=True)
        elif event is EventType.ResumeActuation:
            data.update(module_revision="actuation", resumed=True, paused=False)
        elif event is EventType.SettleActuation:
            data.update(module_revision="actuation", paused=True, execution_complete=True, outputs_settled=True)
        else:
            raise AssertionError(f"Unexpected command: {event}")
        return SimpleNamespace(data=data)

    commands.request.side_effect = request

    async def start_module(module, snapshot):
        calls.append(f"start {module}")
        worker = mock_engine.acquisition_worker if module == "acquisition" else mock_engine.signal_processing_worker
        worker.is_alive.return_value = True

    async def stop_module(module):
        calls.append(f"stop {module}")
        worker = mock_engine.acquisition_worker if module == "acquisition" else mock_engine.signal_processing_worker
        worker.is_alive.return_value = False

    mock_engine.start_module.side_effect = start_module
    mock_engine.stop_module.side_effect = stop_module
    engine_service.configure_modules(
        acquisition, commands, signal, lambda module: deepcopy(engine_service.applied_snapshot),
    )
    return engine_service, mock_engine, acquisition, commands, signal, calls


@pytest.mark.asyncio
async def test_processing_can_start_and_stop_without_acquisition(running_modules):
    engine, graph, acquisition, _, signal, calls = running_modules
    await engine.start_module("processing")
    assert engine.is_module_running("processing")
    assert not engine.is_module_running("acquisition")
    await engine.stop_module("processing")
    assert not engine.is_module_running("processing")
    assert engine.is_engine_ready
    assert engine.generation == "foundation"
    assert calls == ["start processing", "stop processing"]
    acquisition.handle_stop_and_drain.assert_not_called()
    acquisition.handle_start_stream.assert_not_called()
    assert signal.reset.call_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["start", "stop"])
async def test_processing_lifecycle_preserves_a_reading_source_and_foundation(running_modules, action):
    engine, graph, acquisition, _, _, calls = running_modules
    graph.acquisition_worker.is_alive.return_value = True
    graph.signal_processing_worker.is_alive.return_value = action == "stop"
    acquisition.handle_get_status.return_value.streaming = True
    await getattr(engine, f"{action}_module")("processing")
    assert calls == ["drain", "GetSourceCheckpoint", f"{action} processing", "RestoreSourceCheckpoint", "read"]
    assert engine.is_module_running("acquisition")
    assert engine.is_module_running("processing") is (action == "start")
    assert engine.is_engine_ready
    assert engine.generation == "foundation"
    graph.set_actuation_active.assert_not_awaited()
    acquisition.reset.assert_not_called()


@pytest.mark.asyncio
async def test_processing_start_failure_resumes_source_with_previous_configuration(running_modules):
    engine, graph, acquisition, _, _, calls = running_modules
    graph.acquisition_worker.is_alive.return_value = True
    acquisition.handle_get_status.return_value.streaming = True
    graph.start_module.side_effect = RuntimeError("decoder failed")
    before = engine.applied_snapshot
    with pytest.raises(RuntimeError, match="decoder failed"):
        await engine.start_module("processing")
    assert calls == ["drain", "GetSourceCheckpoint", "RestoreSourceCheckpoint", "read"]
    assert engine.applied_snapshot == before
    assert not engine.is_module_running("processing")
    assert engine.is_module_running("acquisition")
    assert engine.is_engine_ready


@pytest.mark.asyncio
async def test_processing_change_does_not_guess_resume_when_source_has_no_checkpoint(running_modules):
    engine, graph, acquisition, commands, _, calls = running_modules
    graph.acquisition_worker.is_alive.return_value = True
    acquisition.handle_get_status.return_value.streaming = True
    commands.request.side_effect = None
    commands.request.return_value = SimpleNamespace(data={
        "generation": "foundation", "system_revision": "system",
        "acquisition_revision": "acquisition", "supported": False,
        "resume_without_checkpoint_safe": False,
    })
    with pytest.raises(RuntimeError, match="does not support resuming"):
        await engine.start_module("processing")
    graph.start_module.assert_not_awaited()
    acquisition.handle_start_stream.assert_not_called()
    assert calls == ["drain"]
    assert engine.is_engine_ready


@pytest.mark.asyncio
async def test_starting_processing_applies_only_its_current_module_revision(running_modules):
    engine, graph, _, _, _, _ = running_modules
    selected = engine.applied_snapshot
    selected["CONFIG.module_revisions"].update(processing="next-processing", stimulation="next-stimulation")
    selected["STIMULATION.renderer"] = "unapplied-renderer"
    engine._module_snapshot_provider = lambda module: selected
    await engine.start_module("processing")
    snapshot = graph.start_module.await_args.args[1]
    assert snapshot["CONFIG.module_revisions"]["processing"] == "next-processing"
    assert snapshot["CONFIG.module_revisions"]["stimulation"] == "stimulation"
    assert "STIMULATION.renderer" not in snapshot
    assert engine.applied_snapshot == snapshot


@pytest.mark.asyncio
async def test_external_actuation_can_stop_and_resume_without_acquisition_or_processing(running_modules):
    engine, graph, acquisition, _, _, calls = running_modules
    engine._applied_snapshot["ACTUATION.auto_spawn"] = False
    graph.peer_gateway.peer.return_value.is_connected = True
    await engine.start_module("actuation")
    assert engine.is_module_running("actuation")
    assert not engine.is_module_running("acquisition")
    assert not engine.is_module_running("processing")
    await engine.stop_module("actuation")
    assert not engine.is_module_running("actuation")
    await engine.start_module("actuation")
    assert engine.is_module_running("actuation")
    assert calls == ["ResumeActuation", "SettleActuation", "ResumeActuation"]
    assert [call.args[0] for call in graph.set_actuation_active.await_args_list] == [True, False, True]
    acquisition.handle_stop_and_drain.assert_not_called()
    assert engine.is_engine_ready


@pytest.mark.asyncio
@pytest.mark.parametrize("module", ["acquisition", "processing", "actuation"])
@pytest.mark.parametrize("already_running", [False, True])
async def test_module_start_cannot_bypass_runtime_recovery(running_modules, module, already_running):
    engine, graph, _, commands, _, calls = running_modules
    if module == "actuation":
        engine._actuation_active = already_running
        graph.peer_gateway.peer.return_value.is_connected = already_running
    else:
        worker = graph.acquisition_worker if module == "acquisition" else graph.signal_processing_worker
        worker.is_alive.return_value = already_running
    engine.admission.quarantine("Unconfirmed module outcome")
    with pytest.raises(ProfileRevisionConflict, match="recovery"):
        await engine.start_module(module)
    graph.start_module.assert_not_awaited()
    graph.set_actuation_active.assert_not_awaited()
    commands.request.assert_not_called()
    assert not calls
    assert engine.is_engine_ready


@pytest.mark.asyncio
async def test_module_stop_remains_available_during_runtime_recovery(running_modules):
    engine, graph, _, _, _, calls = running_modules
    graph.signal_processing_worker.is_alive.return_value = True
    engine.admission.quarantine("Unconfirmed processing outcome")
    await engine.stop_module("processing")
    assert calls == ["stop processing"]
    assert not engine.is_module_running("processing")
    assert engine.is_engine_ready
    assert engine.admission.recovery_required


@pytest.mark.asyncio
async def test_confirmed_actuation_replacement_does_not_repeat_settlement(running_modules):
    engine, graph, _, commands, _, calls = running_modules
    manager = MagicMock()
    engine._actuation_manager = manager
    engine._actuation_active = True
    graph.peer_gateway.peer.return_value.is_connected = True
    async with engine.runtime_transition():
        await engine.stop_actuation_for_configuration()
    commands.request.assert_not_called()
    graph.set_actuation_active.assert_awaited_once_with(False)
    manager.stop.assert_called_once()
    assert not engine.is_module_running("actuation")
    assert not calls
