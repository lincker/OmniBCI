"""
Unit tests for NeuroEngine (pure orchestrator).

All sub-processes and queues are replaced with MagicMocks so no real
multiprocessing or OS resources are used.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, call, patch
import pytest

from omnibci.infrastructure.acquisition.service import AcquisitionCommand
from omnibci.shared.events import EventType, Event
from omnibci.infrastructure.neuroengine.engine import NeuroEngine
from omnibci.infrastructure.neuroengine.processing.signal_processor import SignalProcessorCommand


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_engine(**overrides) -> NeuroEngine:
    """Return a NeuroEngine wired entirely with MagicMocks."""
    defaults = dict(
        event_bus=MagicMock(),
        data_manager=MagicMock(),
        acquisition_worker=MagicMock(),
        acquisition_command_queue=MagicMock(),
        signal_processing_worker=MagicMock(),
        processor_command_queue=MagicMock(),
        peer_gateway=MagicMock(),
    )
    defaults.update(overrides)
    return NeuroEngine(**defaults)


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------

class TestConstruction:
    def test_all_attributes_stored(self):
        eng = _make_engine()
        assert eng.event_bus is not None
        assert eng.data_manager is not None
        assert eng.acquisition_worker is not None
        assert eng.signal_processing_worker is not None
        assert eng.should_stop is False

    def test_should_stop_defaults_false(self):
        assert _make_engine().should_stop is False


# ---------------------------------------------------------------------------
# _start: spawns processes, starts bus and server
# ---------------------------------------------------------------------------

class TestStart:
    @pytest.mark.asyncio
    async def test_starts_acquisition_worker(self):
        eng = _make_engine()
        eng.event_bus.start = AsyncMock()
        await eng._start()
        eng.acquisition_worker.start.assert_called_once()

    @pytest.mark.asyncio
    async def test_starts_signal_processing_worker(self):
        eng = _make_engine()
        eng.event_bus.start = AsyncMock()
        await eng._start()
        eng.signal_processing_worker.start.assert_called_once()

    @pytest.mark.asyncio
    async def test_starts_event_bus(self):
        eng = _make_engine()
        eng.event_bus.start = AsyncMock()
        await eng._start()
        eng.event_bus.start.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_starts_server_management(self):
        eng = _make_engine()
        eng.event_bus.start = AsyncMock()
        await eng._start()
        eng.peer_gateway.start.assert_called_once()

    @pytest.mark.asyncio
    async def test_registers_eight_event_listeners(self):
        # 6 command routes (incl. StartSession/EndSession) + CloseAll + StimulationAck
        eng = _make_engine()
        eng.event_bus.start = AsyncMock()
        await eng._start()
        assert eng.event_bus.register_event_listener.call_count == 8


# ---------------------------------------------------------------------------
# Event routing
# ---------------------------------------------------------------------------

class TestEventRouting:
    """
    Routing is now data-driven (``_command_routes`` + ``_make_command_router``).
    Comprehensive routing-through-registered-listener coverage lives in
    tests/neuroengine/test_neuro_engine_orchestration_characterization.py;
    these tests pin the routing table itself and the router/flag behaviour.
    """

    def test_command_routes_table_maps_events_to_queue_and_command(self):
        eng = _make_engine()
        routes = eng._command_routes()
        acq, proc = eng.acquisition_command_queue, eng.processor_command_queue
        assert routes == {
            EventType.ConnectDataProvider:            (acq, AcquisitionCommand.connect, False),
            EventType.DisconnectDataProvider:         (acq, AcquisitionCommand.disconnect, False),
            EventType.StartStream:                    (acq, AcquisitionCommand.start_stream, False),
            EventType.StopStream:                     (acq, AcquisitionCommand.stop_stream, False),
            EventType.StartSession:                   (proc, SignalProcessorCommand.start_session, True),
            EventType.EndSession:                     (proc, SignalProcessorCommand.end_session, True),
        }

    def test_router_enqueues_command_and_ignores_event_arg(self):
        eng = _make_engine()
        router = eng._make_command_router(eng.acquisition_command_queue, AcquisitionCommand.connect)
        router(Event(EventType.ConnectDataProvider))  # event arg is ignored
        eng.acquisition_command_queue.put.assert_called_once_with(AcquisitionCommand.connect)

    def test_stop_neuro_engine_sets_should_stop(self):
        eng = _make_engine()
        eng._stop_neuro_engine_system()
        assert eng.should_stop is True

    def test_stop_neuro_engine_accepts_event_arg(self):
        eng = _make_engine()
        eng._stop_neuro_engine_system(Event(EventType.CloseAllNeuroEngineProcessSystem))
        assert eng.should_stop is True


# ---------------------------------------------------------------------------
# _stop: sends commands, joins/terminates processes, closes queues
# ---------------------------------------------------------------------------

class TestStop:
    @pytest.mark.asyncio
    async def test_stop_sends_acquisition_stop(self):
        eng = _make_engine()
        eng.peer_gateway.stop = AsyncMock()
        eng.acquisition_worker.is_alive.return_value = False
        eng.signal_processing_worker.is_alive.return_value = False
        await eng._stop()
        eng.acquisition_command_queue.put.assert_any_call(AcquisitionCommand.stop_acquisition)

    @pytest.mark.asyncio
    async def test_stop_sends_processor_stop(self):
        eng = _make_engine()
        eng.peer_gateway.stop = AsyncMock()
        eng.acquisition_worker.is_alive.return_value = False
        eng.signal_processing_worker.is_alive.return_value = False
        await eng._stop()
        eng.processor_command_queue.put.assert_called_once_with(SignalProcessorCommand.should_stop)

    @pytest.mark.asyncio
    async def test_stop_stops_event_bus(self):
        eng = _make_engine()
        eng.peer_gateway.stop = AsyncMock()
        eng.acquisition_worker.is_alive.return_value = False
        eng.signal_processing_worker.is_alive.return_value = False
        await eng._stop()
        eng.event_bus.stop.assert_called_once()

    @pytest.mark.asyncio
    async def test_terminates_process_that_does_not_exit(self):
        eng = _make_engine()
        eng.peer_gateway.stop = AsyncMock()
        eng.acquisition_worker.is_alive.return_value = True   # still alive after join
        eng.signal_processing_worker.is_alive.return_value = False
        await eng._stop()
        eng.acquisition_worker.terminate.assert_called_once()

    @pytest.mark.asyncio
    async def test_closes_both_queues(self):
        eng = _make_engine()
        eng.peer_gateway.stop = AsyncMock()
        eng.acquisition_worker.is_alive.return_value = False
        eng.signal_processing_worker.is_alive.return_value = False
        await eng._stop()
        eng.processor_command_queue.close.assert_called_once()
        eng.acquisition_command_queue.close.assert_called_once()


# ---------------------------------------------------------------------------
# _join_process static helper
# ---------------------------------------------------------------------------

class TestJoinProcess:
    def test_no_terminate_when_process_exits(self):
        proc = MagicMock()
        proc.is_alive.return_value = False
        NeuroEngine._join_process(proc, "TestProc")
        proc.terminate.assert_not_called()

    def test_terminate_when_process_hangs(self):
        proc = MagicMock()
        proc.is_alive.return_value = True
        NeuroEngine._join_process(proc, "TestProc", timeout=0)
        proc.terminate.assert_called_once()
