"""
Unit tests for NeuroEngine (pure orchestrator).

All sub-processes and queues are replaced with MagicMocks so no real
multiprocessing or OS resources are used.
"""
from __future__ import annotations

import asyncio
import queue
import threading
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from omnibci.application.events import CommandFailureKind, CommandRequest
from omnibci.infrastructure.acquisition.acquisition import AcquisitionCommand
from omnibci.infrastructure.ipc.event_bus import EventBus
from omnibci.shared.events import EventType, Event
from omnibci.infrastructure.neuroengine.engine import NeuroEngine
from omnibci.infrastructure.signal_processing.signal_processor import SignalProcessorCommand


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_engine(**overrides) -> NeuroEngine:
    """Return a NeuroEngine wired entirely with MagicMocks."""
    event_bus = MagicMock()
    event_bus.start = AsyncMock()
    event_bus.stop = AsyncMock()
    peer_gateway = MagicMock()
    peer_gateway.start = AsyncMock()
    peer_gateway.stop = AsyncMock()
    defaults = dict(
        event_bus=event_bus,
        data_manager=MagicMock(),
        acquisition_worker=MagicMock(),
        acquisition_command_queue=MagicMock(),
        signal_processing_worker=MagicMock(),
        processor_command_queue=MagicMock(),
        peer_gateway=peer_gateway,
    )
    defaults.update(overrides)
    return NeuroEngine(**defaults)


def _mark_resources_started(eng: NeuroEngine) -> None:
    """Arrange the resource state for focused shutdown tests."""
    eng._acquisition_started = True
    eng._processor_started = True
    eng._event_bus_started = True
    eng._gateway_started = True


class _BoundedCommandQueue(queue.Queue):
    def put(self, item, block=True, timeout=None):
        # Bound a regression's blocking write so the test cannot hang.
        return super().put(item, block=block, timeout=0.05 if block else None)


_COMMAND_ROUTES = [
    (EventType.GetSourceMetadata, "acquisition_command_queue", AcquisitionCommand.get_metadata,
     {"refresh": True}),
    (EventType.PrepareSignalProcessing, "processor_command_queue", SignalProcessorCommand.prepare_signal,
     {"plan": {}, "receipt": {}}),
    (EventType.UpdateProcessingContext, "processor_command_queue", SignalProcessorCommand.update_context,
     {"generation": "run", "expected_revision": "old", "settings_snapshot": {}}),
    (EventType.GetSourceCheckpoint, "acquisition_command_queue", AcquisitionCommand.get_checkpoint,
     {"generation": "run", "expected_revision": "old"}),
    (EventType.RestoreSourceCheckpoint, "acquisition_command_queue", AcquisitionCommand.restore_checkpoint,
     {"generation": "run", "expected_revision": "old", "checkpoint": {}}),
    (EventType.UpdateAcquisitionRevision, "acquisition_command_queue", AcquisitionCommand.update_revision,
     {"generation": "run", "expected_revision": "old", "target_revision": "new"}),
    (EventType.BeginRawRecording, "acquisition_command_queue", AcquisitionCommand.begin_recording,
     {"session_id": "session-1", "subject_id": "subject-1"}),
    (EventType.EndRawRecording, "acquisition_command_queue", AcquisitionCommand.end_recording,
     {"session_id": "session-1"}),
    (EventType.AbortRawRecording, "acquisition_command_queue", AcquisitionCommand.abort_recording,
     {"session_id": "session-1"}),
    (
        EventType.StageProcessingConfiguration,
        "processor_command_queue",
        SignalProcessorCommand.stage_configuration,
        {"settings_snapshot": {}, "expected_revision": "old", "generation": "run"},
    ),
    (
        EventType.CancelProcessingConfiguration,
        "processor_command_queue",
        SignalProcessorCommand.cancel_configuration,
        {"command_id": "pending", "generation": "run"},
    ),
    (
        EventType.ConnectDataProvider,
        "acquisition_command_queue",
        AcquisitionCommand.connect,
        None,
    ),
    (
        EventType.DisconnectDataProvider,
        "acquisition_command_queue",
        AcquisitionCommand.disconnect,
        None,
    ),
    (
        EventType.StartStream,
        "acquisition_command_queue",
        AcquisitionCommand.start_stream,
        None,
    ),
    (
        EventType.StopStream,
        "acquisition_command_queue",
        AcquisitionCommand.stop_stream,
        None,
    ),
    (
        EventType.StopAndDrain,
        "acquisition_command_queue",
        AcquisitionCommand.stop_and_drain,
        {"session_id": "session-1"},
    ),
    (
        EventType.StartSession,
        "processor_command_queue",
        SignalProcessorCommand.start_session,
        {"session_id": "session-1", "paradigm": "classic40SSVEP"},
    ),
    (
        EventType.EndSession,
        "processor_command_queue",
        SignalProcessorCommand.end_session,
        "session-1",
    ),
]

_EXPECTED_EVENT_TYPES = {
    *(route[0] for route in _COMMAND_ROUTES),
    EventType.CloseAllNeuroEngineProcessSystem,
    EventType.StimulationAck,
}


def _registered_handlers(eng: NeuroEngine) -> dict:
    eng._register_event_listeners()
    return {
        registration.args[0]: registration.args[1]
        for registration in eng.event_bus.register_event_listener.call_args_list
    }


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


# ---------------------------------------------------------------------------
# _start: spawns processes, starts bus and server
# ---------------------------------------------------------------------------

class TestStart:
    @pytest.mark.asyncio
    async def test_starts_all_subsystems(self):
        eng = _make_engine()
        await eng._start()
        eng.acquisition_worker.start.assert_called_once()
        eng.signal_processing_worker.start.assert_called_once()
        eng.event_bus.start.assert_awaited_once()
        eng.peer_gateway.start.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_gateway_bind_failure_rolls_back_started_resources(self):
        eng = _make_engine()
        eng.peer_gateway.start.side_effect = OSError("address already in use")
        eng.acquisition_worker.is_alive.return_value = False
        eng.signal_processing_worker.is_alive.return_value = False

        with pytest.raises(OSError, match="address already in use"):
            await eng._start()

        eng.event_bus.stop.assert_awaited_once()
        eng.peer_gateway.stop.assert_awaited_once()
        eng.acquisition_worker.join.assert_called_once()
        eng.signal_processing_worker.join.assert_called_once()
        eng.acquisition_command_queue.close.assert_called_once()
        eng.processor_command_queue.close.assert_called_once()
        assert eng.shutdown_complete is True


class TestThreadReadiness:
    def test_success_is_visible_only_after_async_start(self):
        eng = _make_engine()
        eng.acquisition_worker.is_alive.return_value = False
        eng.signal_processing_worker.is_alive.return_value = False
        eng.should_stop = True
        thread = threading.Thread(target=eng.run)

        thread.start()
        eng.wait_until_started(timeout=1.0)
        thread.join(timeout=1.0)

        assert not thread.is_alive()
        assert eng.shutdown_complete is True

    def test_async_start_failure_crosses_thread_boundary(self):
        eng = _make_engine()
        eng.peer_gateway.start.side_effect = OSError("bind failed")
        eng.acquisition_worker.is_alive.return_value = False
        eng.signal_processing_worker.is_alive.return_value = False
        raised = []

        def run():
            try:
                eng.run()
            except BaseException as exc:
                raised.append(exc)

        thread = threading.Thread(target=run)
        thread.start()
        with pytest.raises(RuntimeError, match="startup failed"):
            eng.wait_until_started(timeout=1.0)
        thread.join(timeout=1.0)

        assert isinstance(raised[0], OSError)
        assert eng.shutdown_complete is True


# ---------------------------------------------------------------------------
# Event registration and routing through the public bus seam
# ---------------------------------------------------------------------------

class TestEventRouting:
    @pytest.mark.parametrize("event_type,queue_name", [
        (EventType.StartStream, "acquisition_command_queue"),
        (EventType.StopAndDrain, "acquisition_command_queue"),
        (EventType.StageProcessingConfiguration, "processor_command_queue"),
    ])
    def test_full_command_queue_returns_correlated_rejection(self, event_type, queue_name):
        command_queue = _BoundedCommandQueue(maxsize=1)
        command_queue.put_nowait("existing command")
        eng = _make_engine(**{queue_name: command_queue})
        handler = _registered_handlers(eng)[event_type]

        handler(Event(event_type, CommandRequest("request-1", {})))

        result_event = eng.event_bus.put_event.call_args.args[0]
        assert result_event.type is EventType.CommandResult
        assert result_event.data.command_id == "request-1"
        assert result_event.data.command == event_type.name
        assert result_event.data.ok is False
        assert result_event.data.failure_kind is CommandFailureKind.REJECTED
        assert result_event.data.error_code == "COMMAND_QUEUE_FULL"
        assert command_queue.get_nowait() == "existing command"
        assert command_queue.empty()

    @pytest.mark.asyncio
    async def test_full_command_queue_keeps_event_dispatch_responsive(self):
        command_queue = _BoundedCommandQueue(maxsize=1)
        command_queue.put_nowait("existing command")
        bus = EventBus(queue.Queue())
        eng = _make_engine(event_bus=bus, processor_command_queue=command_queue)
        eng._register_event_listeners()
        received = asyncio.get_running_loop().create_future()
        bus.register_event_listener(EventType.CommandResult, received.set_result)
        await bus.start()
        try:
            bus.put_event(Event(EventType.StartSession, CommandRequest("session-1", {})))
            bus.put_event(Event(EventType.CloseAllNeuroEngineProcessSystem))
            result_event = await asyncio.wait_for(received, timeout=1)
            assert result_event.data.command_id == "session-1"
            assert result_event.data.error_code == "COMMAND_QUEUE_FULL"
            assert eng.should_stop is True
        finally:
            await bus.stop()

    def test_registers_each_expected_event_once(self):
        eng = _make_engine()
        _registered_handlers(eng)
        registrations = eng.event_bus.register_event_listener.call_args_list
        event_types = [registration.args[0] for registration in registrations]
        assert len(event_types) == len(_EXPECTED_EVENT_TYPES)
        assert set(event_types) == _EXPECTED_EVENT_TYPES

    @pytest.mark.parametrize(
        "event_type,queue_name,command,payload",
        _COMMAND_ROUTES,
    )
    def test_registered_command_routes(
        self,
        event_type,
        queue_name,
        command,
        payload,
    ):
        eng = _make_engine()
        handler = _registered_handlers(eng)[event_type]
        handler(Event(event_type, payload))

        carries_payload = payload is not None or event_type in {
            EventType.ConnectDataProvider,
            EventType.DisconnectDataProvider,
            EventType.StartStream,
            EventType.StopStream,
        }
        expected = (command, payload) if carries_payload else command
        target_queue = getattr(eng, queue_name)
        target_queue.put_nowait.assert_called_once_with(expected)

        other_queue = (
            eng.processor_command_queue
            if queue_name == "acquisition_command_queue"
            else eng.acquisition_command_queue
        )
        other_queue.put_nowait.assert_not_called()

    def test_registered_close_handler_stops_without_queueing(self):
        eng = _make_engine()
        handler = _registered_handlers(eng)[
            EventType.CloseAllNeuroEngineProcessSystem
        ]
        handler(Event(EventType.CloseAllNeuroEngineProcessSystem))

        assert eng.should_stop is True
        eng.acquisition_command_queue.put_nowait.assert_not_called()
        eng.processor_command_queue.put_nowait.assert_not_called()


# ---------------------------------------------------------------------------
# _stop: sends commands, joins/terminates processes, closes queues
# ---------------------------------------------------------------------------

class TestStop:
    @pytest.mark.asyncio
    async def test_stop_tears_down_all_started_resources(self):
        eng = _make_engine()
        _mark_resources_started(eng)
        eng.acquisition_worker.is_alive.return_value = False
        eng.signal_processing_worker.is_alive.return_value = False
        await eng._stop()

        eng.acquisition_command_queue.put_nowait.assert_called_once_with(
            AcquisitionCommand.stop_acquisition
        )
        eng.processor_command_queue.put_nowait.assert_called_once_with(
            SignalProcessorCommand.should_stop
        )
        eng.acquisition_worker.join.assert_called_once_with(timeout=3)
        eng.signal_processing_worker.join.assert_called_once_with(timeout=3)
        eng.acquisition_worker.terminate.assert_not_called()
        eng.signal_processing_worker.terminate.assert_not_called()
        eng.peer_gateway.stop.assert_awaited_once()
        eng.event_bus.stop.assert_awaited_once()
        eng.processor_command_queue.close.assert_called_once()
        eng.acquisition_command_queue.close.assert_called_once()
        assert eng.shutdown_complete is True

    @pytest.mark.asyncio
    async def test_terminates_process_that_does_not_exit(self):
        eng = _make_engine()
        _mark_resources_started(eng)
        eng.acquisition_worker.is_alive.side_effect = [True, False, False]
        eng.signal_processing_worker.is_alive.return_value = False
        await eng._stop()
        eng.acquisition_worker.terminate.assert_called_once()

    @pytest.mark.asyncio
    async def test_closes_builder_owned_ipc_queues(self):
        event_queue, data_buffer = MagicMock(), MagicMock()
        eng = _make_engine(owned_ipc_queues=(event_queue, data_buffer))
        _mark_resources_started(eng)
        eng.acquisition_worker.is_alive.return_value = False
        eng.signal_processing_worker.is_alive.return_value = False

        await eng._stop()

        event_queue.close.assert_called_once_with()
        event_queue.cancel_join_thread.assert_called_once_with()
        event_queue.join_thread.assert_not_called()
        data_buffer.close.assert_called_once_with()
        data_buffer.cancel_join_thread.assert_called_once_with()
        data_buffer.join_thread.assert_not_called()

    @pytest.mark.asyncio
    async def test_queue_close_failure_keeps_shutdown_incomplete(self):
        eng = _make_engine()
        _mark_resources_started(eng)
        eng.acquisition_worker.is_alive.return_value = False
        eng.signal_processing_worker.is_alive.return_value = False
        eng.acquisition_command_queue.close.side_effect = OSError("close failed")

        with pytest.raises(ExceptionGroup, match="shutdown failed"):
            await eng._stop()

        assert eng.shutdown_complete is False
        assert eng._queues_closed is False


# ---------------------------------------------------------------------------
# Full serve lifecycle and event-driven shutdown
# ---------------------------------------------------------------------------

class TestServeLifecycle:
    @pytest.mark.asyncio
    async def test_preset_stop_runs_start_and_stop(self):
        eng = _make_engine()
        eng.acquisition_worker.is_alive.return_value = False
        eng.signal_processing_worker.is_alive.return_value = False
        eng.should_stop = True

        await eng.serve()

        eng.acquisition_worker.start.assert_called_once()
        eng.signal_processing_worker.start.assert_called_once()
        eng.event_bus.start.assert_awaited_once()
        eng.peer_gateway.start.assert_awaited_once()
        eng.event_bus.stop.assert_awaited_once()
        eng.peer_gateway.stop.assert_awaited_once()
        assert eng.shutdown_complete is True

    def test_cross_thread_stop_wakes_main_loop_promptly(self):
        eng = _make_engine()
        for worker in (eng.acquisition_worker, eng.signal_processing_worker):
            worker.is_alive.return_value = True
            worker.join.side_effect = lambda **kw: [
                setattr(process.is_alive, "return_value", False)
                for process in (eng.acquisition_worker, eng.signal_processing_worker)
            ]
        stop_time = {}

        def stop_engine():
            time.sleep(0.05)
            stop_time["requested_at"] = time.perf_counter()
            eng.should_stop = True

        thread = threading.Thread(target=stop_engine)
        thread.start()
        asyncio.run(eng.serve())
        thread.join(timeout=1.0)

        elapsed = time.perf_counter() - stop_time["requested_at"]
        assert elapsed < 0.5
        eng.event_bus.stop.assert_awaited_once()
        eng.peer_gateway.stop.assert_awaited_once()
