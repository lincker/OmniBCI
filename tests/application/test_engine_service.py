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
from omnibci.application.configuration.errors import ProfileRevisionConflict
from omnibci.application.services.command_result_service import CommandExecutionError, CommandResultBroker, CommandTimeoutError
from omnibci.shared.events import Event, EventType


class TestNeuroEngineServiceStatus:
    def test_status_before_init_returns_all_false(self):
        svc = NeuroEngineService(engine_factory=MagicMock())
        result = svc.handle_get_status(GetEngineStatusQuery())
        assert result.running is False
        assert result.acquisition_alive is False
        assert result.processor_alive is False

    @pytest.mark.parametrize("module", ["stimulation", "actuation"])
    def test_peer_delivery_fault_only_quarantines_current_output_channel(
        self, module, engine_service, stimulation_service, actuation_service
    ):
        owners = {"stimulation": stimulation_service, "actuation": actuation_service}
        for owner in owners.values():
            owner._command_results = MagicMock()
        current = None
        for channel in ("previous", "current"):
            current = Event(EventType.PeerDeliveryFault, {
                "peer": module, "channel_id": channel, "reason": "write failed",
            })
            for owner in owners.values():
                owner.on_peer_delivery_fault(current)
            assert engine_service.admission.recovery_required is (channel == "current")
        assert engine_service.admission.recovery_status == {module: "write failed"}
        owners[module]._command_results.on_peer_delivery_fault.assert_called_once_with(current)
        for role, owner in owners.items():
            if role != module:
                owner._command_results.on_peer_delivery_fault.assert_not_called()


class TestNeuroEngineServicePortState:
    def test_engine_port_cannot_be_instantiated_directly(self):
        from omnibci.application.ports.engine_port import EnginePort

        with pytest.raises(TypeError):
            EnginePort()

    def test_neuro_engine_service_is_engine_port(self):
        from omnibci.application.ports.engine_port import EnginePort

        assert issubclass(NeuroEngineService, EnginePort)
        assert isinstance(NeuroEngineService(engine_factory=MagicMock()), EnginePort)

    @pytest.mark.parametrize("alive", [None, False, True])
    def test_actuation_process_liveness_is_independent_of_engine_readiness(self, engine_service, alive, actuation_service):
        engine_service._actuation = actuation_service
        actuation_service._process_manager = None if alive is None else MagicMock(is_alive=alive)
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
        with pytest.raises(RuntimeError, match="not running|stop is in progress"):
            engine_service.dispatch(Event(EventType.StartStream))

        mock_engine.dispatch.assert_not_called()

    def test_dispatch_accepts_running_ready_engine(
        self, engine_service, mock_engine
    ):
        engine_service._running = True
        engine_service._accepting_commands = True
        engine_service._engine_thread = MagicMock()
        engine_service._engine_thread.is_alive.return_value = True
        event = Event(EventType.StartStream)

        engine_service.dispatch(event)

        mock_engine.dispatch.assert_called_once()
        forwarded = mock_engine.dispatch.call_args.args[0]
        assert forwarded.type == event.type and forwarded.data == event.data

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

        mock_engine.dispatch.side_effect = dispatch
        try:
            assert broker.request(EventType.StartStimTrial, {"n_trials": 1, "trial_params": [{}]}).ok
        finally:
            for timer in timers:
                timer.join()
        assert broker.pending_count == 0
        assert engine_service.is_engine_ready is True
        mock_engine.dispatch.assert_called_once()

    def test_closed_worker_handle_does_not_affect_foundation_readiness(self, engine_service, mock_engine):
        engine_service._running = True
        engine_service._accepting_commands = True
        engine_service._engine_thread = MagicMock()
        engine_service._engine_thread.is_alive.return_value = True
        mock_engine.signal_processing_worker.is_alive.side_effect = ValueError("closed")

        assert engine_service.is_engine_ready is True
        mock_engine.signal_processing_worker.is_alive.assert_not_called()

    @pytest.mark.parametrize("worker,event_type,payload", [
        ("acquisition_worker", EventType.StartStream, None),
        ("signal_processing_worker", EventType.ResetProcessingInput,
         {"generation": "run", "engine_revision": "engine", "expected_revision": "processing"}),
    ])
    def test_command_waiter_reports_only_its_failed_module(
        self, engine_service, mock_engine, worker, event_type, payload,
    ):
        engine_service._running = engine_service._accepting_commands = True
        engine_service._engine_thread = MagicMock()
        engine_service._engine_thread.is_alive.return_value = True
        getattr(mock_engine, worker).is_alive.return_value = True
        mock_engine.dispatch.side_effect = lambda event: setattr(
            getattr(mock_engine, worker).is_alive, "return_value", False,
        )
        broker = CommandResultBroker(engine_service, timeout_s=1)
        with pytest.raises(CommandExecutionError) as raised:
            broker.request(event_type, payload)
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
        with pytest.raises(RuntimeError, match="not running|stop is in progress"):
            engine_service.dispatch(Event(EventType.StartStream))
        mock_engine.dispatch.assert_not_called()

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
        self, engine_service, actuation_service
    ):
        manager = MagicMock(is_alive=False)
        manager.start.side_effect = RuntimeError("effector unavailable")
        actuation_service._process_manager = manager
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
            await actuation_service.start({"CONFIG.module_revisions": {"actuation": "a" * 64}})
        manager.start.assert_called_once()
        manager.stop.assert_called_once()
        assert not engine_service.admission.recovery_required
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
        "CONFIG.engine_revision": "engine",
        "CONFIG.module_revisions": {module: module for module in (
            "acquisition", "processing", "stimulation", "actuation",
        )},
        "PLUGINS.bindings": [],
    }
    mock_engine.is_peer_connected.return_value = False
    acquisition, commands, signal = MagicMock(), MagicMock(), MagicMock()
    acquisition.handle_get_status.return_value = SimpleNamespace(connected=True, streaming=False)
    calls = []
    acquisition.handle_stop_and_drain.side_effect = lambda: calls.append("drain")
    acquisition.handle_start_stream.side_effect = lambda cmd: calls.append("read")

    def request(event, payload=None):
        calls.append(event.name)
        data = {"generation": "foundation", "engine_revision": "engine"}
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
    async with engine.runtime_transition("processing"):
        await engine.start_module_for_configuration("processing", engine.applied_snapshot)
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
    if action == "start":
        async with engine.runtime_transition("processing"):
            await engine.start_module_for_configuration("processing", engine.applied_snapshot)
    else:
        await engine.stop_module("processing")
    assert calls == [f"{action} processing"]
    acquisition.handle_stop_and_drain.assert_not_called()
    acquisition.handle_start_stream.assert_not_called()
    assert engine.is_module_running("acquisition")
    assert engine.is_module_running("processing") is (action == "start")
    assert engine.is_engine_ready
    assert engine.generation == "foundation"
    acquisition.reset.assert_not_called()


@pytest.mark.asyncio
async def test_processing_start_failure_leaves_source_reading_with_previous_configuration(running_modules):
    engine, graph, acquisition, _, _, calls = running_modules
    graph.acquisition_worker.is_alive.return_value = True
    acquisition.handle_get_status.return_value.streaming = True
    graph.start_module.side_effect = RuntimeError("decoder failed")
    before = engine.applied_snapshot
    with pytest.raises(RuntimeError, match="decoder failed"):
        async with engine.runtime_transition("processing"):
            await engine.start_module_for_configuration("processing", engine.applied_snapshot)
    assert calls == []
    acquisition.handle_stop_and_drain.assert_not_called()
    acquisition.handle_start_stream.assert_not_called()
    assert engine.applied_snapshot == before
    assert not engine.is_module_running("processing")
    assert engine.is_module_running("acquisition")
    assert engine.is_engine_ready


@pytest.mark.asyncio
async def test_processing_start_does_not_require_source_checkpoint(running_modules):
    engine, graph, acquisition, commands, _, calls = running_modules
    graph.acquisition_worker.is_alive.return_value = True
    acquisition.handle_get_status.return_value.streaming = True
    commands.request.side_effect = None
    commands.request.return_value = SimpleNamespace(data={
        "generation": "foundation", "engine_revision": "engine",
        "acquisition_revision": "acquisition", "supported": False,
        "resume_mode": "restart-only",
    })
    async with engine.runtime_transition("processing"):
        await engine.start_module_for_configuration("processing", engine.applied_snapshot)
    graph.start_module.assert_awaited_once()
    commands.request.assert_not_called()
    acquisition.handle_stop_and_drain.assert_not_called()
    acquisition.handle_start_stream.assert_not_called()
    assert calls == ["start processing"]
    assert engine.is_engine_ready


@pytest.mark.asyncio
@pytest.mark.parametrize("module", ["acquisition", "processing"])
@pytest.mark.parametrize("already_running", [False, True])
async def test_module_start_cannot_bypass_runtime_recovery(running_modules, module, already_running):
    engine, graph, _, commands, _, calls = running_modules
    worker = graph.acquisition_worker if module == "acquisition" else graph.signal_processing_worker
    worker.is_alive.return_value = already_running
    engine.admission.quarantine("Unconfirmed module outcome")
    with pytest.raises(ProfileRevisionConflict, match="recovery"):
        async with engine.runtime_transition(module):
            await engine.start_module_for_configuration(module, engine.applied_snapshot)
    graph.start_module.assert_not_awaited()
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
async def test_engine_stop_can_finish_during_external_preparation(running_modules, actuation_service, actuation_host):
    engine, graph, _, _, _, _ = running_modules
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    order = []

    def listener_ready(*args):
        entered.set()
        assert release.wait(3)
        order.append("listener ready")
        finished.set()

    def stopped(_timeout):
        order.append("foundation stopped")
        engine._engine_thread.is_alive.return_value = False

    actuation_host.ensure_peer_ready_sync.side_effect = listener_ready
    engine._engine_thread.join.side_effect = stopped
    caller = asyncio.create_task(asyncio.to_thread(actuation_service.prepare_external,
        {"ACTUATION.auto_spawn": False, "CONFIG.module_revisions": {"actuation": "a" * 64}}))
    stop = None
    try:
        assert await asyncio.to_thread(entered.wait, 1)
        caller.cancel()
        with pytest.raises(asyncio.CancelledError):
            await caller
        stop = asyncio.create_task(engine.handle_stop_engine(StopEngineCommand()))
        await asyncio.wait_for(stop, 2)
        assert order == ["foundation stopped"]
        release.set()
        assert await asyncio.to_thread(finished.wait, 1)
        assert order == ["foundation stopped", "listener ready"]
        assert not engine.is_engine_ready
    finally:
        release.set()
        await asyncio.to_thread(finished.wait, 1)
        if stop is not None:
            await asyncio.gather(stop, return_exceptions=True)


@pytest.mark.asyncio
async def test_acquisition_stop_releases_worker_when_processing_cannot_confirm_drain(running_modules):
    engine, graph, acquisition, _, _, calls = running_modules
    graph.acquisition_worker.is_alive.return_value = True
    graph.signal_processing_worker.is_alive.return_value = True
    acquisition.handle_stop_and_drain.side_effect = CommandTimeoutError("drain", "StopAndDrain", .01)
    await engine.stop_module("acquisition")
    assert calls == ["stop acquisition"]
    assert not engine.is_module_running("acquisition")
    assert engine.is_module_running("processing")
    assert engine.admission.requires_recovery("processing")
    assert not engine.admission.requires_recovery("acquisition")


@pytest.mark.asyncio
async def test_processing_stop_releases_worker_but_retains_failed_input_boundary(running_modules):
    engine, graph, _, _, signal, calls = running_modules
    graph.acquisition_worker.is_alive.return_value = True
    graph.signal_processing_worker.is_alive.return_value = True
    signal.detach.side_effect = CommandTimeoutError("detach", "DetachProcessingInput", .01)
    with pytest.raises(CommandTimeoutError):
        await engine.stop_module("processing")
    assert calls == ["stop processing"]
    assert not engine.is_module_running("processing")
    assert engine.is_module_running("acquisition")
    assert engine.admission.requires_recovery("processing")
    assert not engine.admission.requires_recovery("acquisition")


@pytest.mark.asyncio
async def test_server_shutdown_waits_for_a_foundation_start_that_is_not_ready_yet(engine_service):
    entered, release = asyncio.Event(), asyncio.Event()
    order = []

    async def start(command):
        entered.set()
        await release.wait()
        order.append("start complete")

    async def stop(command):
        order.append("stop complete")

    engine_service._start_engine_locked = start
    engine_service._stop_engine_locked = stop
    starting = asyncio.create_task(engine_service.handle_start_engine(StartEngineCommand()))
    await entered.wait()
    stopping = asyncio.create_task(engine_service.shutdown())
    await asyncio.sleep(0)
    assert not stopping.done()
    release.set()
    await asyncio.gather(starting, stopping)
    assert order == ["start complete", "stop complete"]


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["start_foundation", "stop_foundation"])
async def test_module_owner_cannot_execute_another_modules_lifecycle(running_modules, operation):
    engine, graph, _, _, _, _ = running_modules
    operations = {
        "start_foundation": lambda: engine.start_for_configuration(engine.applied_snapshot),
        "stop_foundation": engine.stop_for_configuration,
    }
    async with engine.runtime_transition("processing"):
        with pytest.raises(RuntimeError, match="own"):
            await operations[operation]()


@pytest.mark.asyncio
async def test_foundation_start_still_rejects_engine_recovery(engine_service):
    engine_service.admission.quarantine("Unknown foundation ownership", "engine")
    with pytest.raises(ProfileRevisionConflict, match="recovery"):
        await engine_service.handle_start_engine(StartEngineCommand())


@pytest.mark.asyncio
async def test_unreleased_managed_output_does_not_block_foundation_stop(running_modules, actuation_service):
    engine, graph, _, _, _, _ = running_modules
    await actuation_service.start({"CONFIG.module_revisions": {"actuation": "a" * 64}})
    actuation_service._process_manager.stop.side_effect = RuntimeError("Process did not exit")
    with pytest.raises(RuntimeError, match="did not exit"):
        await actuation_service.stop()
    engine._engine_thread.join.side_effect = lambda _: setattr(engine._engine_thread.is_alive, "return_value", False)
    await engine.handle_stop_engine(StopEngineCommand())
    assert not engine.restart_blocked and not engine.is_engine_ready
    assert actuation_service.process_alive
    assert engine.admission.recovery_status.keys() == {"actuation"}


@pytest.mark.asyncio
async def test_engine_stop_preserves_independent_stimulation_recovery(running_modules, stimulation_service, stimulation_snapshot):
    engine, graph, _, _, _, _ = running_modules
    stimulation_service.prepare_external(stimulation_snapshot)
    retained = stimulation_service.applied_snapshot
    engine.admission.quarantine("External stop unconfirmed", "stimulation")
    engine._engine_thread.join.side_effect = lambda _: setattr(engine._engine_thread.is_alive, "return_value", False)
    await engine.handle_stop_engine(StopEngineCommand())
    assert engine.admission.requires_recovery("stimulation")
    assert stimulation_service.applied_snapshot == retained
    graph.start_module.assert_not_awaited()


def _restart_mock_engine():
    engine = MagicMock()
    engine.shutdown_complete = True
    engine.run = MagicMock()
    return engine


@pytest.fixture()
def restart_factory():
    return MagicMock(side_effect=lambda snapshot: _restart_mock_engine())


@pytest.fixture()
def restart_svc(restart_factory):
    service = NeuroEngineService(engine_factory=restart_factory)
    service._engine = _restart_mock_engine()
    service._running = False
    return service


class TestRestartRebuildsEngine:
    @pytest.mark.asyncio
    async def test_start_stop_start_builds_new_engine(self, restart_svc, restart_factory):
        first = restart_svc._engine
        rebuilt = _restart_mock_engine()
        restart_factory.side_effect = None
        restart_factory.return_value = rebuilt

        await restart_svc.handle_start_engine(StartEngineCommand())
        await restart_svc.handle_stop_engine(StopEngineCommand())
        await restart_svc.handle_start_engine(StartEngineCommand())

        assert restart_svc._engine is rebuilt
        assert restart_svc._engine is not first
        restart_factory.assert_called_once()

    @pytest.mark.asyncio
    async def test_first_start_does_not_rebuild(self, restart_svc, restart_factory):
        first = restart_svc._engine
        await restart_svc.handle_start_engine(StartEngineCommand())
        restart_factory.assert_not_called()
        assert restart_svc._engine is first

    @pytest.mark.asyncio
    async def test_subscriptions_replayed_onto_new_bus(self, restart_svc, restart_factory):
        handler = MagicMock()
        restart_svc.subscribe(EventType.TrialCompleted, handler)

        await restart_svc.handle_start_engine(StartEngineCommand())
        await restart_svc.handle_stop_engine(StopEngineCommand())

        rebuilt = _restart_mock_engine()
        restart_factory.side_effect = None
        restart_factory.return_value = rebuilt
        await restart_svc.handle_start_engine(StartEngineCommand())

        rebuilt.subscribe.assert_called_once_with(EventType.TrialCompleted, handler)

    @pytest.mark.asyncio
    async def test_stop_is_idempotent(self, restart_svc):
        await restart_svc.handle_start_engine(StartEngineCommand())
        await restart_svc.handle_stop_engine(StopEngineCommand())
        await restart_svc.handle_stop_engine(StopEngineCommand())
        assert restart_svc._running is False

    @pytest.mark.asyncio
    async def test_restart_cycle_twice(self, restart_svc, restart_factory):
        await restart_svc.handle_start_engine(StartEngineCommand())
        await restart_svc.handle_stop_engine(StopEngineCommand())

        restart_factory.side_effect = [_restart_mock_engine(), _restart_mock_engine()]
        await restart_svc.handle_start_engine(StartEngineCommand())
        await restart_svc.handle_stop_engine(StopEngineCommand())
        await restart_svc.handle_start_engine(StartEngineCommand())
        assert restart_factory.call_count == 2


class _OperationOnlyGraph:
    __slots__ = ("started", "stopped", "shutdown_complete", "subscriptions", "events")

    def __init__(self):
        self.started = threading.Event()
        self.stopped = threading.Event()
        self.shutdown_complete = False
        self.subscriptions = []
        self.events = []

    def run(self):
        self.started.set()
        self.stopped.wait(5)
        self.shutdown_complete = True

    def wait_until_started(self, timeout):
        assert self.started.wait(timeout)

    def request_stop(self):
        self.stopped.set()

    def subscribe(self, event_type, handler):
        self.subscriptions.append((event_type, handler))

    def dispatch(self, event):
        self.events.append(event)

    def is_peer_connected(self, peer):
        return peer == "stimulation"

    def is_current_peer_channel(self, peer, channel):
        return peer == "actuation" and channel == "current"

    def is_module_running(self, module):
        return False

    async def set_actuation_active(self, active):
        pass


@pytest.mark.asyncio
async def test_empty_foundation_passes_one_explicit_detached_snapshot():
    graph = _OperationOnlyGraph()
    inputs = []

    def build(snapshot):
        inputs.append(dict(snapshot))
        snapshot["factory-only"] = True
        return graph

    service = NeuroEngineService(build)
    await service.startup()
    try:
        await service.handle_start_engine(StartEngineCommand())
        assert len(inputs) == 1 and inputs[0]["CONFIG.generation"] == service.generation
        assert inputs[0]["PLUGINS.bindings"] == []
        assert "factory-only" not in service.applied_snapshot
        await service.handle_stop_engine(StopEngineCommand())
        assert graph.shutdown_complete
    finally:
        graph.stopped.set()
        if service._engine_thread is not None:
            service._engine_thread.join(1)


@pytest.mark.asyncio
async def test_subscriptions_dispatch_and_peer_faults_use_only_declared_operations(actuation_service):
    from unittest.mock import Mock

    graph = _OperationOnlyGraph()
    service = NeuroEngineService(lambda snapshot: graph, snapshot_provider=lambda: {"PLUGINS.bindings": []})
    listener = Mock()
    await service.startup()
    service.subscribe(EventType.TrialCompleted, listener)
    try:
        await service.handle_start_engine(StartEngineCommand())
        assert graph.subscriptions == [(EventType.TrialCompleted, listener)]
        command = Event(EventType.StartStimTrial, {"n_trials": 1, "trial_params": [{"target_id": 1}]})
        service.dispatch(command)
        assert len(graph.events) == 1 and graph.events[0].type == command.type
        assert graph.events[0].data == command.data
        command.data["trial_params"][0]["target_id"] = 2
        assert graph.events[0].data["trial_params"] == [{"target_id": 1}]
        assert not service.is_peer_connected("stimulation")
        for channel in ("old", "current"):
            actuation_service.on_peer_delivery_fault(Event(EventType.PeerDeliveryFault, {
                "peer": "actuation", "channel_id": channel, "reason": "lost delivery",
            }))
            assert actuation_service._admission.requires_recovery("actuation") is (channel == "current")
        await service.handle_stop_engine(StopEngineCommand())
        assert graph.shutdown_complete
    finally:
        graph.stopped.set()
        if service._engine_thread is not None:
            service._engine_thread.join(1)


@pytest.fixture
def thread_factory(monkeypatch):
    from unittest.mock import Mock

    def create(**kwargs):
        thread = MagicMock()
        thread.is_alive.return_value = True
        thread.join.side_effect = lambda _timeout: setattr(thread.is_alive, "return_value", False)
        return thread

    factory = Mock(side_effect=create)
    monkeypatch.setattr("omnibci.application.services.engine_service.Thread", factory)
    return factory


def _runtime_graph():
    graph = MagicMock(shutdown_complete=True)
    graph.set_actuation_active = AsyncMock()
    graph.ensure_output_ready = AsyncMock()
    return graph


def _runtime_snapshot(revision="revision-1"):
    return {
        "CONFIG.module_revisions": {"acquisition": revision, "processing": revision,
                                    "stimulation": revision, "actuation": revision},
        "CONFIG.engine_revision": "engine-1",
        "ACQUISITION.sample_rate": 250,
        "STIMULATION.paradigm": "selected-paradigm",
        "ACTUATION.effector": "selected-effector",
        "PLUGINS.bindings": [{"slot": "processing.decoder", "parameters": {"window_duration": 3.0}}],
    }


@pytest.mark.asyncio
async def test_unknown_shutdown_confirmation_blocks_replacement(thread_factory):
    graph = _runtime_graph()
    graph.shutdown_complete = None
    service = NeuroEngineService(lambda snapshot: graph, snapshot_provider=lambda: {"PLUGINS.bindings": []})
    await service.startup()
    await service.handle_start_engine(StartEngineCommand())
    with pytest.raises(RuntimeError, match="resources"):
        await service.handle_stop_engine(StopEngineCommand())
    assert service.restart_blocked
    with pytest.raises(RuntimeError, match="recovery"):
        await service.handle_start_engine(StartEngineCommand())


@pytest.mark.asyncio
async def test_graph_and_late_outputs_ignore_saved_edits(thread_factory, stimulation_host, actuation_service):
    from unittest.mock import Mock
    from omnibci.application.commands.stimulation_commands import StartStimulationCommand
    from omnibci.application.services.stimulation_service import StimulationAppService

    saved = _runtime_snapshot()
    provider = Mock(return_value=saved)
    graph = _runtime_graph()
    factory = Mock(return_value=graph)
    actuation = actuation_service._process_manager
    service = NeuroEngineService(factory, snapshot_provider=provider)
    await service.startup()
    await service.handle_start_engine(StartEngineCommand())
    pinned = service.applied_snapshot

    saved["CONFIG.module_revisions"]["acquisition"] = "saved-later"
    saved["PLUGINS.bindings"][0]["parameters"]["window_duration"] = 9.0
    returned = service.applied_snapshot
    returned["PLUGINS.bindings"][0]["parameters"]["window_duration"] = 12.0
    manager = Mock(auto_spawn=True)
    stimulation = StimulationAppService(stimulation_host, manager)
    stimulation.handle_start_stimulation(StartStimulationCommand(), settings_snapshot=pinned)
    actuation.start.assert_not_called()
    await actuation_service.start(pinned)

    provider.assert_called_once_with()
    assert factory.call_args.args == (pinned,)
    actuation.start.assert_called_once_with(settings_snapshot=actuation_service.applied_snapshot)
    assert actuation_service.applied_snapshot["CONFIG.generation"] != service.generation
    manager.start.assert_called_once_with(settings_snapshot=stimulation.applied_snapshot)
    assert stimulation.applied_snapshot["CONFIG.generation"] != service.generation
    assert service.applied_snapshot == pinned
    assert service.applied_revisions["acquisition"] == "revision-1"
    assert isinstance(service.generation, str) and service.generation
    assert "CONFIG.generation" not in saved


@pytest.mark.asyncio
async def test_factory_cannot_mutate_pinned_snapshot(thread_factory):
    saved = _runtime_snapshot()

    def build(snapshot):
        snapshot["PLUGINS.bindings"][0]["parameters"]["window_duration"] = 99
        return _runtime_graph()

    service = NeuroEngineService(build, snapshot_provider=lambda: saved)
    await service.startup()
    await service.handle_start_engine(StartEngineCommand())

    assert service.applied_snapshot["PLUGINS.bindings"] == saved["PLUGINS.bindings"]


@pytest.mark.asyncio
async def test_restart_resolves_once_and_explicit_rollback_does_not_resolve(thread_factory):
    from unittest.mock import Mock

    provider = Mock(side_effect=[_runtime_snapshot("first"), _runtime_snapshot("second")])
    factory = Mock(side_effect=lambda snapshot: _runtime_graph())
    service = NeuroEngineService(factory, snapshot_provider=provider)
    await service.startup()
    await service.handle_start_engine(StartEngineCommand())
    original = service.applied_snapshot
    await service.handle_stop_engine(StopEngineCommand())
    await service.handle_start_engine(StartEngineCommand())
    second_generation = service.generation

    assert service.applied_revisions["acquisition"] == "second"
    assert second_generation != original["CONFIG.generation"]
    await service.handle_stop_engine(StopEngineCommand())
    await service._start_engine_locked(StartEngineCommand(), settings_snapshot=original)

    assert provider.call_count == 2
    assert service.applied_revisions["acquisition"] == "first"
    assert service.generation not in (original["CONFIG.generation"], second_generation)
    assert factory.call_args.args[0]["CONFIG.module_revisions"]["acquisition"] == "first"


@pytest.mark.asyncio
async def test_failed_candidate_keeps_last_successful_revision(thread_factory):
    from unittest.mock import Mock

    failed_graph = _runtime_graph()
    failed_graph.wait_until_started.side_effect = RuntimeError("initialization failed")
    factory = Mock(side_effect=[_runtime_graph(), failed_graph])
    provider = Mock(side_effect=[_runtime_snapshot("first"), _runtime_snapshot("failed")])
    service = NeuroEngineService(factory, snapshot_provider=provider)
    await service.startup()
    await service.handle_start_engine(StartEngineCommand())
    original = deepcopy(service.applied_snapshot)
    await service.handle_stop_engine(StopEngineCommand())

    with pytest.raises(RuntimeError, match="initialization failed"):
        await service.handle_start_engine(StartEngineCommand())

    assert service.applied_snapshot == original
    assert not service.is_engine_ready


@pytest.mark.asyncio
async def test_missing_snapshot_is_not_silently_loaded_from_global_settings(thread_factory):
    from unittest.mock import Mock

    factory = Mock()
    service = NeuroEngineService(factory, snapshot_provider=lambda: None)
    await service.startup()

    with pytest.raises(ValueError, match="snapshot_provider must return"):
        await service.handle_start_engine(StartEngineCommand())

    factory.assert_not_called()


def _pkg_snapshot(version):
    return {"CONFIG.revision": f"revision-{version}", "PLUGINS.bindings": [{
        "selection": {"package_id": "acme.device", "contribution_id": "output", "version": version},
        "parameters": {"gain": 1.0},
    }]}


@pytest.mark.parametrize("running,alive,blocked", [(True, False, False), (False, True, False), (False, False, True)])
def test_paused_or_unresolved_graph_retains_acknowledged_and_attempted_packages(running, alive, blocked):
    from unittest.mock import Mock

    service = NeuroEngineService(Mock())
    service._applied_snapshot = _pkg_snapshot("1.0.0")
    service._graph_snapshot = _pkg_snapshot("2.0.0")
    service._running = running
    service._restart_blocked = blocked
    service._engine_thread = Mock(is_alive=Mock(return_value=alive))
    service._accepting_commands = False

    references = service.runtime_package_snapshots

    assert references == (_pkg_snapshot("1.0.0"), _pkg_snapshot("2.0.0"))
    references[0]["PLUGINS.bindings"][0]["selection"]["version"] = "changed"
    references[1]["PLUGINS.bindings"][0]["parameters"]["gain"] = 99
    assert service._applied_snapshot == _pkg_snapshot("1.0.0")
    assert service._graph_snapshot == _pkg_snapshot("2.0.0")
    assert not service.is_engine_ready


def test_clean_stopped_engine_does_not_retain_historical_package_snapshots():
    from unittest.mock import Mock

    service = NeuroEngineService(Mock())
    service._applied_snapshot = _pkg_snapshot("1.0.0")
    service._graph_snapshot = _pkg_snapshot("2.0.0")
    service._engine_thread = Mock(is_alive=Mock(return_value=False))
    assert service.runtime_package_snapshots == ()
    assert service.applied_snapshot == _pkg_snapshot("1.0.0")


@pytest.mark.asyncio
async def test_successful_stop_releases_runtime_references_but_preserves_history(thread_factory):
    from unittest.mock import Mock

    service = NeuroEngineService(
        Mock(return_value=_runtime_graph()),
        snapshot_provider=lambda: _pkg_snapshot("1.0.0"),
    )
    await service.startup()
    await service.handle_start_engine(StartEngineCommand())
    applied = service.applied_snapshot
    assert service.runtime_package_snapshots == (applied, applied)

    await service.handle_stop_engine(StopEngineCommand())

    assert service.runtime_package_snapshots == ()
    assert service.applied_snapshot == applied


@pytest.mark.asyncio
async def test_unconfirmed_shutdown_keeps_package_ownership_until_successful_recovery(thread_factory):
    from unittest.mock import Mock

    graph = _runtime_graph()
    service = NeuroEngineService(Mock(return_value=graph), snapshot_provider=lambda: _pkg_snapshot("1.0.0"))
    await service.startup()
    await service.handle_start_engine(StartEngineCommand())
    graph.shutdown_complete = False
    with pytest.raises(RuntimeError):
        await service.handle_stop_engine(StopEngineCommand())
    assert service.restart_blocked and not service.is_engine_ready
    assert service.runtime_package_snapshots

    graph.shutdown_complete = True
    await service.handle_stop_engine(StopEngineCommand())
    assert not service.runtime_package_snapshots


def test_preparation_copies_inputs_and_excludes_externally_owned_outputs():
    from unittest.mock import Mock

    def prepare(snapshot, modules):
        assert modules == ("processing",)
        snapshot["PLUGINS.artifacts"] = {"processing.decoder": {"pin": "checked"}}
        return snapshot

    callback = Mock(side_effect=prepare)
    engine = NeuroEngineService(Mock(), plugin_preparer=callback)
    selected = {"PLUGINS.bindings": [], "STIMULATION.auto_spawn": False, "ACTUATION.auto_spawn": False}
    original = deepcopy(selected)
    prepared = engine.prepare_plugins(selected, ("processing", "stimulation", "actuation"))
    assert prepared["PLUGINS.artifacts"]["processing.decoder"]["pin"] == "checked"
    assert selected == original
    callback.assert_called_once()


def test_module_snapshot_is_prepared_before_returning_it_to_a_host():
    from unittest.mock import Mock

    callback = Mock(side_effect=lambda snapshot, modules: {**snapshot, "PLUGINS.artifacts": {"slot": "checked"}})
    engine = NeuroEngineService(Mock(), plugin_preparer=callback)
    engine._applied_snapshot = {"PLUGINS.bindings": [], "CONFIG.engine_revision": "engine"}
    engine._module_snapshot_provider = lambda module: {
        "PLUGINS.bindings": [], "CONFIG.engine_revision": "engine",
        "CONFIG.module_revisions": {module: "revision"},
    }
    selected = engine.selected_module_snapshot("acquisition")
    assert selected["PLUGINS.artifacts"] == {"slot": "checked"}
    assert callback.call_args.args[1] == ("acquisition",)
    assert "PLUGINS.artifacts" not in engine.applied_snapshot


def test_missing_preparation_dependency_fails_explicitly_for_selected_code():
    from unittest.mock import Mock

    engine = NeuroEngineService(Mock())
    with pytest.raises(RuntimeError, match="preparation"):
        engine.prepare_plugins({"PLUGINS.bindings": [{"slot": "acquisition.source"}]}, ("acquisition",))


@pytest.mark.asyncio
async def test_module_acknowledgement_and_rollback_preserve_concurrent_output_update():
    from unittest.mock import Mock

    service = NeuroEngineService(Mock())
    service._engine = Mock()
    service._running = service._accepting_commands = True
    service._engine_thread = Mock(is_alive=Mock(return_value=True))
    original = {
        "CONFIG.generation": "generation", "CONFIG.engine_revision": "engine",
        "CONFIG.module_revisions": {"stimulation": "s0", "actuation": "e0"},
        "STIMULATION.renderer": "old", "ACTUATION.server_port": 1234,
        "PLUGINS.bindings": [
            {"slot": "stimulation.paradigm", "parameters": {"value": 0}},
            {"slot": "actuation.effector", "parameters": {"value": 0}},
        ],
    }
    service._applied_snapshot = deepcopy(original)
    first, second = deepcopy(original), deepcopy(original)
    first["CONFIG.module_revisions"]["stimulation"] = "s1"
    first["STIMULATION.renderer"] = "new"
    first["PLUGINS.bindings"][0]["parameters"]["value"] = 1
    second["CONFIG.module_revisions"]["actuation"] = "e1"
    second["ACTUATION.server_port"] = 5678
    second["PLUGINS.bindings"][1]["parameters"]["value"] = 2
    async with service.runtime_transition("stimulation"):
        service.acknowledge_configuration(first, modules=("stimulation",))
    async with service.runtime_transition("actuation"):
        service.acknowledge_configuration(second, modules=("actuation",))
    applied = service.applied_snapshot
    assert applied["CONFIG.module_revisions"] == {"stimulation": "s1", "actuation": "e1"}
    assert applied["STIMULATION.renderer"] == "new"
    assert applied["ACTUATION.server_port"] == 5678
    async with service.runtime_transition("actuation"):
        service.acknowledge_configuration(original, modules=("actuation",))
    assert service.applied_snapshot["STIMULATION.renderer"] == "new"
    assert service.applied_revisions["stimulation"] == "s1"


@pytest.fixture
def reading_engine():
    from unittest.mock import Mock

    engine = NeuroEngineService(Mock())
    engine._running = engine._accepting_commands = True
    engine._engine = Mock()
    engine._engine_thread = Mock(is_alive=lambda: True)
    engine._applied_snapshot = {"CONFIG.generation": "generation", "CONFIG.module_revisions": {"acquisition": "revision"}}
    engine._acquisition = Mock(handle_get_status=lambda _: SimpleNamespace(connected=True, connection_id="connection"))
    return engine


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["generation", "expected_revision", "connection_id"])
async def test_reading_rejects_stale_identity_without_dispatching(reading_engine, field):
    identity = {"generation": "generation", "expected_revision": "revision", "connection_id": "connection"}
    identity[field] = "stale"
    with pytest.raises(ProfileRevisionConflict, match="Source connection changed"):
        await reading_engine.start_reading(**identity)
    reading_engine._acquisition.handle_start_stream.assert_not_called()


@pytest.mark.asyncio
async def test_reading_checks_identity_in_owned_task_and_does_not_merge_other_requests(reading_engine):
    entered, release = threading.Event(), threading.Event()

    def read(*args, **kwargs):
        assert reading_engine.admission.owns("acquisition")
        entered.set()
        assert release.wait(3)

    reading_engine._acquisition.handle_start_stream.side_effect = read
    request = asyncio.create_task(reading_engine.start_reading(
        generation="generation", expected_revision="revision", connection_id="connection"))
    try:
        assert await asyncio.to_thread(entered.wait, 1)
        with pytest.raises(ProfileRevisionConflict, match="in progress"):
            await reading_engine.start_reading(generation="stale", expected_revision="revision", connection_id="connection")
        request.cancel()
        with pytest.raises(asyncio.CancelledError):
            await request
        with pytest.raises(ProfileRevisionConflict):
            reading_engine.admission.reserve_transition("acquisition")
    finally:
        release.set()
        await reading_engine._lifecycle_tasks[("read", "acquisition")]
    reading_engine._acquisition.handle_start_stream.assert_called_once()
    reading_engine._acquisition.prepare_processing.assert_called_once()


@pytest.mark.asyncio
async def test_stop_stream_serializes_concurrent_processing_stop_and_allows_acquisition_stop(running_modules):
    from omnibci.application.commands.acquisition_commands import StopStreamCommand
    from omnibci.application.events import StreamDrainReceipt
    from omnibci.application.services.acquisition_service import AcquisitionAppService

    engine, graph, _, _, signal, calls = running_modules
    graph.acquisition_worker.is_alive.return_value = True
    graph.signal_processing_worker.is_alive.return_value = True
    entered, release = threading.Event(), threading.Event()
    receipt = StreamDrainReceipt("foundation", "acquisition", 5, 1, False)

    def request(event_type, payload=None, *, timeout_s=None):
        assert event_type is EventType.StopAndDrain
        entered.set()
        assert release.wait(3)
        calls.append("drain_done")
        return CommandResult("stop-1", "StopAndDrain", True, data=receipt.to_dict())

    executor = MagicMock()
    executor.request.side_effect = request
    real_acquisition = AcquisitionAppService(executor, admission=engine.admission)
    engine._acquisition = real_acquisition

    stream_stop = asyncio.create_task(asyncio.to_thread(real_acquisition.handle_stop_stream, StopStreamCommand()))
    assert await asyncio.to_thread(entered.wait, 1.0)
    processing_stop = asyncio.create_task(engine.stop_module("processing"))
    await asyncio.sleep(0.05)
    assert not processing_stop.done()
    assert "stop processing" not in calls

    release.set()
    await asyncio.gather(stream_stop, processing_stop)
    assert calls == ["drain_done", "stop processing"]

    # Acquisition stop re-enters handle_stop_and_drain inside its own
    # single-scope ("acquisition",) stop transition without nested conflicts.
    graph.acquisition_worker.is_alive.return_value = True
    executor.request.side_effect = lambda event_type, payload=None, *, timeout_s=None: CommandResult(
        "stop-2", "StopAndDrain", True, data=receipt.to_dict(),
    )
    await engine.stop_module("acquisition")
    assert not engine.is_module_running("acquisition")
    assert not engine.admission.requires_recovery("acquisition")


@pytest.mark.asyncio
async def test_engine_stop_reconciles_unconfirmed_shutdown_and_unlocks_restart(thread_factory):
    from tests.neuroengine.test_neuro_engine import _make_engine

    first = _make_engine(allow_process_termination=False)
    first._startup_complete.set()
    first._acquisition_started = True
    first.acquisition_worker.is_alive.return_value = True
    first.signal_processing_worker.is_alive.return_value = False
    second = _runtime_graph()
    built = [first, second]
    service = NeuroEngineService(lambda snapshot: built.pop(0), snapshot_provider=lambda: {"PLUGINS.bindings": []})
    await service.startup()
    await service.handle_start_engine(StartEngineCommand())

    with pytest.raises(RuntimeError, match="unreleased resources"):
        await service.handle_stop_engine(StopEngineCommand())
    assert service.restart_blocked is True
    assert service.admission.requires_recovery("engine")
    assert first.shutdown_complete is False

    with pytest.raises(ProfileRevisionConflict, match="recovery"):
        await service.handle_start_engine(StartEngineCommand())

    # Once the lingering worker exits, retrying Stop reconciles cleanup off the
    # retired Engine loop and unlocks a fresh generation.
    first.acquisition_worker.is_alive.return_value = False
    await service.handle_stop_engine(StopEngineCommand())
    assert first.shutdown_complete is True
    assert service.restart_blocked is False
    assert not service.admission.requires_recovery("engine")

    await service.handle_start_engine(StartEngineCommand())
    assert service._engine is second
    assert service.is_engine_ready is True


@pytest.mark.asyncio
async def test_processing_stop_drains_abandoned_queue_and_blocks_dirty_restart(running_modules):
    import queue as std_queue
    from omnibci.application.events import StreamBarrier
    from omnibci.domain.signal import SignalChunk
    from omnibci.infrastructure.acquisition.acquisition import AcquisitionCommand
    from omnibci.infrastructure.ipc.data_manager import DataManager
    from tests.neuroengine.test_neuro_engine import _make_engine
    import numpy as np

    # 1. Real NeuroEngine queue drain: unconfirmed flush marks queue dirty and
    # blocks processing restart; confirmed flush drains stale chunks and clears dirty.
    gate = threading.Event()
    gate.set()
    data = DataManager(std_queue.Queue(maxsize=2), processing_active=gate)
    cmd_queue = std_queue.Queue(maxsize=2)
    eng = _make_engine(data_manager=data, acquisition_command_queue=cmd_queue, processing_active=gate)
    eng._acquisition_started = True
    eng.acquisition_worker.is_alive.return_value = True
    eng.signal_processing_worker.is_alive.return_value = False
    eng._ready_modules.add("acquisition")
    eng._started = True
    data.put_data(SignalChunk(0, 0, np.zeros((2, 2))))
    data.put_data(SignalChunk(0, 1, np.zeros((2, 2))))

    assert await eng._drain_abandoned_processing_queue(timeout_s=0.1) is False
    assert eng.processing_queue_dirty is True
    with pytest.raises(RuntimeError, match="cleanup was not confirmed"):
        await eng._start_module("processing", {})

    # Discard earlier unconsumed flush command and respond to the next flush request.
    while not cmd_queue.empty():
        cmd_queue.get_nowait()

    def responder():
        command, token = cmd_queue.get(timeout=1.0)
        assert command is AcquisitionCommand.flush_processing_queue
        deadline = threading.Event()
        for _ in range(50):
            try:
                data.put_data(StreamBarrier(token, "run", "acq", 1, command="FlushProcessingQueue"))
                return
            except std_queue.Full:
                deadline.wait(0.01)

    worker = threading.Thread(target=responder, daemon=True)
    worker.start()
    assert await eng._drain_abandoned_processing_queue(timeout_s=1.0) is True
    worker.join(timeout=1.0)
    assert eng.processing_queue_dirty is False
    assert data.empty()

    # 2. NeuroEngineService keeps processing quarantined while dirty and clears on clean retry.
    service, graph, _, _, signal, _ = running_modules
    graph.acquisition_worker.is_alive.return_value = True
    graph.signal_processing_worker.is_alive.return_value = True
    signal.detach.side_effect = CommandTimeoutError("detach", "DetachProcessingInput", 0.01)
    graph.processing_queue_dirty = True
    graph.drain_abandoned_processing_queue = AsyncMock(return_value=False)

    with pytest.raises(CommandTimeoutError) as failed:
        await service.stop_module("processing")
    assert any("boundary could not be confirmed" in note for note in getattr(failed.value, "__notes__", ()))
    assert service.admission.requires_recovery("processing")

    # Retry while still dirty keeps processing quarantined.
    signal.detach.side_effect = None
    with pytest.raises(RuntimeError, match="boundary could not be confirmed"):
        await service.stop_module("processing")
    assert service.admission.requires_recovery("processing")

    # Once flush confirms, retry clears recovery.
    graph.drain_abandoned_processing_queue = AsyncMock(return_value=True)
    graph.processing_queue_dirty = False
    await service.stop_module("processing")
    assert not service.admission.requires_recovery("processing")
