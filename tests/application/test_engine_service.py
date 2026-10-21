"""
Unit tests for NeuroEngineService (application service).

NeuroEngine itself is fully mocked — no subprocesses are spawned.
"""
import pytest
import asyncio
import threading
from unittest.mock import MagicMock, patch, AsyncMock

from omnibci.application.commands.engine_commands import (
    StartEngineCommand,
    StopEngineCommand,
)
from omnibci.application.queries.engine_queries import GetEngineStatusQuery
from omnibci.application.services.engine_service import NeuroEngineService
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
    def test_worker_exit_closes_command_admission_before_engine_cleanup(
        self, engine_service, mock_engine, worker
    ):
        engine_service._running = True
        engine_service._accepting_commands = True
        engine_service._engine_thread = MagicMock()
        engine_service._engine_thread.is_alive.return_value = True
        mock_engine.event_bus.put_event.side_effect = lambda event: setattr(
            getattr(mock_engine, worker).is_alive, "return_value", False,
        )
        broker = CommandResultBroker(engine_service, timeout_s=1)

        with pytest.raises(CommandExecutionError) as raised:
            broker.request(EventType.StartStream)

        assert raised.value.result.error_code == "ENGINE_STOPPED"
        assert broker.pending_count == 0
        assert engine_service.is_engine_ready is False
        with pytest.raises(RuntimeError, match="not running"):
            engine_service.dispatch(Event(EventType.StartStream))
        mock_engine.event_bus.put_event.assert_called_once()

    def test_closed_worker_handle_is_not_running_ready(self, engine_service, mock_engine):
        engine_service._running = True
        engine_service._accepting_commands = True
        engine_service._engine_thread = MagicMock()
        engine_service._engine_thread.is_alive.return_value = True
        mock_engine.signal_processing_worker.is_alive.side_effect = ValueError("closed")

        assert engine_service.is_engine_ready is False

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
        engine_service._graph_snapshot = {"ACTUATION.enabled": True, "ACTUATION.auto_spawn": True}
        thread = MagicMock()
        thread.is_alive.return_value = True

        with patch(
            "omnibci.application.services.engine_service.Thread",
            return_value=thread,
        ):
            await engine_service.handle_start_engine(StartEngineCommand())

        manager.start.assert_not_called()
        with pytest.raises(RuntimeError, match="effector unavailable"):
            engine_service.prepare_actuation()
        manager.start.assert_called_once_with(settings_snapshot=engine_service.applied_snapshot)
        manager.stop.assert_not_called()
        assert engine_service.is_engine_ready

    @pytest.mark.parametrize("settings", [{}, {"ACTUATION.enabled": False},
                                          {"ACTUATION.enabled": True, "ACTUATION.auto_spawn": False}])
    def test_preparation_does_not_start_disabled_or_external_actuation(self, engine_service, settings):
        engine_service._actuation_manager = MagicMock()
        engine_service._applied_snapshot = settings
        engine_service.prepare_actuation()
        engine_service._actuation_manager.start.assert_not_called()


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
