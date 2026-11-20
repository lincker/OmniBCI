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
    def test_peer_delivery_fault_only_quarantines_current_output_channel(self, module):
        svc = NeuroEngineService(engine_factory=MagicMock())
        svc._engine = MagicMock()
        svc._engine.is_current_peer_channel.side_effect = lambda peer, channel: channel == "current-channel"
        svc._commands = MagicMock()
        stale = Event(EventType.PeerDeliveryFault, {
            "peer": module, "channel_id": "previous-channel", "reason": "write failed",
        })
        svc.on_peer_delivery_fault(stale)
        assert not svc.admission.recovery_required
        svc._commands.on_peer_delivery_fault.assert_not_called()

        current = Event(EventType.PeerDeliveryFault, {
            "peer": module, "channel_id": "current-channel", "reason": "write failed",
        })
        svc.on_peer_delivery_fault(current)
        assert svc.admission.recovery_status == {module: "write failed"}
        svc._commands.on_peer_delivery_fault.assert_called_once_with(current)
        for independent_module in ("acquisition", "processing"):
            with svc.admission.command(independent_module):
                pass


class TestNeuroEngineServicePortState:
    @pytest.mark.parametrize("alive", [None, False, True])
    def test_actuation_process_liveness_is_independent_of_engine_readiness(self, engine_service, alive):
        engine_service._actuation.process_manager = None if alive is None else MagicMock(is_alive=alive)
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
        self, engine_service
    ):
        manager = MagicMock(is_alive=False)
        manager.start.side_effect = RuntimeError("effector unavailable")
        engine_service._actuation.process_manager = manager
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
            async with engine_service.runtime_transition("actuation"):
                await engine_service.start_module_for_configuration("actuation", engine_service.applied_snapshot)
        manager.start.assert_called_once_with(settings_snapshot=engine_service.applied_snapshot)
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
    graph.set_actuation_active.assert_not_awaited()
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
async def test_external_actuation_can_stop_and_resume_without_acquisition_or_processing(running_modules):
    engine, graph, acquisition, _, _, calls = running_modules
    engine._applied_snapshot["ACTUATION.auto_spawn"] = False
    graph.is_peer_connected.return_value = True
    async with engine.runtime_transition("actuation"):
        await engine.start_module_for_configuration("actuation", engine.applied_snapshot)
    assert engine.is_module_running("actuation")
    assert not engine.is_module_running("acquisition")
    assert not engine.is_module_running("processing")
    await engine.stop_module("actuation")
    assert not engine.is_module_running("actuation")
    async with engine.runtime_transition("actuation"):
        await engine.start_module_for_configuration("actuation", engine.applied_snapshot)
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
        engine._actuation.active = already_running
        graph.is_peer_connected.return_value = already_running
    else:
        worker = graph.acquisition_worker if module == "acquisition" else graph.signal_processing_worker
        worker.is_alive.return_value = already_running
    engine.admission.quarantine("Unconfirmed module outcome")
    with pytest.raises(ProfileRevisionConflict, match="recovery"):
        async with engine.runtime_transition(module):
            await engine.start_module_for_configuration(module, engine.applied_snapshot)
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
    engine._actuation.process_manager = manager
    engine._actuation.active = True
    graph.is_peer_connected.return_value = True
    async with engine.runtime_transition():
        await engine.stop_actuation_for_configuration()
    commands.request.assert_not_called()
    graph.set_actuation_active.assert_awaited_once_with(False)
    manager.stop.assert_called_once()
    assert not engine.is_module_running("actuation")
    assert not calls


@pytest.mark.asyncio
async def test_cancelled_prepare_caller_keeps_foundation_stop_waiting_for_listener(running_modules):
    engine, graph, _, _, _, _ = running_modules
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    order = []

    def listener_ready():
        entered.set()
        assert release.wait(3)
        order.append("listener ready")
        finished.set()

    async def prepare(*args):
        await asyncio.to_thread(listener_ready)

    def stopped(_timeout):
        order.append("foundation stopped")
        engine._engine_thread.is_alive.return_value = False

    graph.ensure_output_ready.side_effect = prepare
    engine._engine_thread.join.side_effect = stopped
    caller = asyncio.create_task(engine.prepare_output("stimulation"))
    stop = None
    try:
        assert await asyncio.to_thread(entered.wait, 1)
        caller.cancel()
        with pytest.raises(asyncio.CancelledError):
            await caller
        stop = asyncio.create_task(engine.handle_stop_engine(StopEngineCommand()))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert not stop.done()
        assert order == []
        release.set()
        await asyncio.wait_for(stop, 2)
        assert order == ["listener ready", "foundation stopped"]
        assert not engine.is_engine_ready
    finally:
        release.set()
        await asyncio.to_thread(finished.wait, 1)
        if stop is not None:
            await asyncio.gather(stop, return_exceptions=True)


@pytest.mark.asyncio
async def test_actuation_settlement_failure_still_cleans_managed_process_and_requires_recovery(running_modules):
    engine, graph, _, commands, _, _ = running_modules
    manager = MagicMock(is_alive=True)
    manager.stop.side_effect = lambda: setattr(manager, "is_alive", False)
    engine._actuation.process_manager = manager
    engine._actuation.active = True
    graph.is_peer_connected.return_value = True
    commands.request.side_effect = CommandTimeoutError("settle", "SettleActuation", .01)
    with pytest.raises(CommandTimeoutError):
        await engine.stop_module("actuation")
    manager.stop.assert_called_once()
    assert not manager.is_alive
    assert engine.admission.requires_recovery("actuation")
    assert not engine.admission.requires_recovery("processing")


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
async def test_failed_external_resume_remains_recoverable_before_active_flag_is_set(running_modules):
    engine, graph, _, commands, _, _ = running_modules
    engine._applied_snapshot["ACTUATION.auto_spawn"] = False
    graph.is_peer_connected.return_value = True
    request = commands.request.side_effect
    commands.request.side_effect = CommandTimeoutError("resume", "ResumeActuation", .01)
    with pytest.raises(CommandTimeoutError):
        async with engine.runtime_transition("actuation"):
            await engine.start_module_for_configuration("actuation", engine.applied_snapshot)
    assert not engine._actuation.active
    assert engine.admission.requires_recovery("actuation")
    commands.request.side_effect = request
    await engine.stop_module("actuation")
    assert commands.request.call_args.args[0] is EventType.SettleActuation
    assert not engine.admission.requires_recovery("actuation")
    graph.reconfigure_output_modules.assert_not_called()


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
@pytest.mark.parametrize("operation", ["start_output", "stop_output", "replace_output", "release_output",
                                      "start_foundation", "stop_foundation"])
async def test_module_owner_cannot_execute_another_modules_lifecycle(running_modules, operation):
    engine, graph, _, _, _, _ = running_modules
    operations = {
        "start_output": lambda: engine.start_module_for_configuration("actuation", engine.applied_snapshot),
        "stop_output": lambda: engine.stop_module_for_configuration("actuation"),
        "replace_output": lambda: engine.reconfigure_output_modules(engine.applied_snapshot, ("actuation",)),
        "release_output": engine.stop_actuation_for_configuration,
        "start_foundation": lambda: engine.start_for_configuration(engine.applied_snapshot),
        "stop_foundation": engine.stop_for_configuration,
    }
    async with engine.runtime_transition("processing"):
        with pytest.raises(RuntimeError, match="own"):
            await operations[operation]()


@pytest.mark.asyncio
@pytest.mark.parametrize("module", ["stimulation", "actuation"])
async def test_external_recovery_can_rebuild_foundation_and_publish_only_retained_configuration(running_modules, module):
    from omnibci.application.services.stimulation_service import StimulationAppService
    from omnibci.application.commands.stimulation_commands import StopStimulationCommand

    engine, graph, _, commands, _, _ = running_modules
    old = engine.applied_snapshot
    old[f"{module.upper()}.auto_spawn"] = False
    setting = "STIMULATION.renderer" if module == "stimulation" else "ACTUATION.server_ip"
    old[setting] = "original"
    old["PLUGINS.bindings"] = [{"slot": f"{module}.main", "plugin_id": "retained"},
                               {"slot": "processing.decoder", "plugin_id": "unrelated"}]
    engine._applied_snapshot = deepcopy(old)
    graph.is_peer_connected.return_value = True
    if module == "actuation":
        engine._actuation.active = True
        engine._actuation._snapshot = deepcopy(old)
    else:
        engine._stimulation = StimulationAppService(engine, command_results=commands, admission=engine.admission)
        engine._stimulation._active_identity = engine._stimulation._identity(old)
    commands.request.side_effect = CommandTimeoutError("stop", "StopOutput", .01)
    engine._engine_thread.join.side_effect = lambda _: setattr(engine._engine_thread.is_alive, "return_value", False)

    with pytest.raises(RuntimeError, match="not confirmed"):
        await engine.handle_stop_engine(StopEngineCommand())
    assert not engine.restart_blocked
    assert engine.admission.recovery_status.keys() == {module}
    assert any(snapshot.get(setting) == "original"
               for snapshot in engine.runtime_package_snapshots)
    assert [binding["plugin_id"] for snapshot in engine.runtime_package_snapshots
            for binding in snapshot.get("PLUGINS.bindings", [])] == ["retained"]

    engine._snapshot_provider = lambda: {"CONFIG.engine_revision": "engine", "CONFIG.module_revisions": {}, "PLUGINS.bindings": []}
    engine._engine_factory = lambda snapshot: graph
    engine._module_snapshot_provider = MagicMock(side_effect=AssertionError("Recovery must not read selected values"))
    thread = MagicMock()
    thread.is_alive.return_value = True
    with patch("omnibci.application.services.engine_service.Thread", return_value=thread):
        await engine.handle_start_engine(StartEngineCommand())
    assert engine.admission.recovery_status.keys() == {module}
    with pytest.raises(ProfileRevisionConflict, match="[Pp]repar"):
        engine.node_configuration_snapshot(lambda: {}, module=module)

    commands.request.reset_mock()
    await engine.prepare_output(module)
    restored = graph.ensure_output_ready.await_args.args[1]
    assert restored[setting] == "original"
    assert restored["CONFIG.generation"] != old["CONFIG.generation"]
    assert restored["CONFIG.module_revisions"][module] == old["CONFIG.module_revisions"][module]
    commands.request.assert_not_called()
    engine._module_snapshot_provider.assert_not_called()
    state, publication = engine.node_configuration_snapshot(lambda: {}, module=module)
    assert state == "running" and publication == engine.applied_snapshot
    assert engine.admission.recovery_status.keys() == {module}

    def stopped(event):
        return SimpleNamespace(data={"generation": engine.generation, "engine_revision": "engine",
                                     "module_revision": module, "paused": True,
                                     "execution_complete": True, "outputs_settled": True})
    commands.request.side_effect = stopped
    if module == "actuation":
        with pytest.raises(ProfileRevisionConflict, match="recovery"):
            async with engine.runtime_transition(module):
                await engine.start_module_for_configuration(module, engine.applied_snapshot)
        await engine.stop_module(module)
    else:
        engine._stimulation.handle_stop_stimulation(StopStimulationCommand())
    assert not engine.admission.requires_recovery(module)


@pytest.mark.asyncio
async def test_foundation_start_still_rejects_engine_recovery(engine_service):
    engine_service.admission.quarantine("Unknown foundation ownership", "engine")
    with pytest.raises(ProfileRevisionConflict, match="recovery"):
        await engine_service.handle_start_engine(StartEngineCommand())


@pytest.mark.asyncio
async def test_unreleased_managed_output_still_blocks_foundation_restart(running_modules):
    engine, graph, _, _, _, _ = running_modules
    manager = MagicMock(is_alive=True)
    manager.stop.side_effect = RuntimeError("Process did not exit")
    engine._actuation.process_manager = manager
    engine._actuation.active = True
    engine._actuation._snapshot = engine.applied_snapshot
    engine._engine_thread.join.side_effect = lambda _: setattr(engine._engine_thread.is_alive, "return_value", False)
    with pytest.raises(RuntimeError, match="not confirmed"):
        await engine.handle_stop_engine(StopEngineCommand())
    assert engine.restart_blocked
    assert engine.admission.recovery_status.keys() == {"engine", "actuation"}
    with pytest.raises(ProfileRevisionConflict, match="recovery"):
        await engine.handle_start_engine(StartEngineCommand())
    manager.stop.side_effect = lambda: setattr(manager, "is_alive", False)
    await engine.handle_stop_engine(StopEngineCommand())
    assert not engine.restart_blocked
    assert not engine.admission.recovery_required


@pytest.mark.asyncio
async def test_external_recovery_stop_requires_receipt_even_before_enable(running_modules):
    engine, graph, _, commands, _, _ = running_modules
    engine._applied_snapshot["ACTUATION.auto_spawn"] = False
    engine.admission.quarantine("Configuration settlement was not confirmed", "actuation")
    graph.is_peer_connected.return_value = True
    request = commands.request.side_effect
    commands.request.side_effect = CommandTimeoutError("settle", "SettleActuation", .01)
    with pytest.raises(CommandTimeoutError):
        await engine.stop_module("actuation")
    assert engine.admission.requires_recovery("actuation")
    assert engine._actuation.owned_snapshot["ACTUATION.auto_spawn"] is False
    commands.request.side_effect = request
    await engine.stop_module("actuation")
    assert not engine.admission.requires_recovery("actuation")


@pytest.mark.asyncio
async def test_a_later_stimulation_recovery_uses_its_current_configuration(running_modules):
    engine, graph, _, _, _, _ = running_modules
    previous = engine.applied_snapshot
    previous["STIMULATION.auto_spawn"] = False
    previous["STIMULATION.renderer"] = "previous"
    engine._recovery_snapshots["stimulation"] = previous
    engine._applied_snapshot["STIMULATION.auto_spawn"] = False
    engine._applied_snapshot["STIMULATION.renderer"] = "current"
    engine._applied_snapshot["CONFIG.module_revisions"]["stimulation"] = "current-revision"
    engine.admission.quarantine("A later external stop was not confirmed", "stimulation")
    await engine.prepare_output("stimulation")
    prepared = graph.ensure_output_ready.await_args.args[1]
    assert prepared["STIMULATION.renderer"] == "current"
    assert prepared["CONFIG.module_revisions"]["stimulation"] == "current-revision"
