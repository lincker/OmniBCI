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
    defaults = dict(
        event_bus=event_bus,
        data_manager=MagicMock(),
        acquisition_worker=MagicMock(),
        acquisition_command_queue=MagicMock(),
        signal_processing_worker=MagicMock(),
        processor_command_queue=MagicMock(),
    )
    defaults.update(overrides)
    return NeuroEngine(**defaults)


def _mark_resources_started(eng: NeuroEngine) -> None:
    """Arrange the resource state for focused shutdown tests."""
    eng._acquisition_started = True
    eng._processor_started = True
    eng._event_bus_started = True


class _BoundedCommandQueue(queue.Queue):
    def put(self, item, block=True, timeout=None):
        # Bound a regression's blocking write so the test cannot hang.
        return super().put(item, block=block, timeout=0.05 if block else None)


_COMMAND_ROUTES = [
    (EventType.StartAcquisitionPreview, "acquisition_command_queue", AcquisitionCommand.start_preview,
     {"generation": "run", "capture_id": "capture", "selection": {}}),
    (EventType.GetAcquisitionPreview, "acquisition_command_queue", AcquisitionCommand.get_preview,
     {"generation": "run"}),
    (EventType.GetSourceMetadata, "acquisition_command_queue", AcquisitionCommand.get_metadata,
     {"refresh": True}),
    (EventType.PrepareSignalProcessing, "processor_command_queue", SignalProcessorCommand.prepare_signal,
     {"plan": {}, "receipt": {}}),
    (EventType.GetSourceCheckpoint, "acquisition_command_queue", AcquisitionCommand.get_checkpoint,
     {"generation": "run", "expected_revision": "old"}),
    (EventType.RestoreSourceCheckpoint, "acquisition_command_queue", AcquisitionCommand.restore_checkpoint,
     {"generation": "run", "expected_revision": "old", "checkpoint": {}}),
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
        EventType.StopAndDrain,
        "acquisition_command_queue",
        AcquisitionCommand.stop_and_drain,
        {},
    ),

]

_EXPECTED_EVENT_TYPES = {
    *(route[0] for route in _COMMAND_ROUTES),
    EventType.AttachProcessingInput,
    EventType.DetachProcessingInput,
    EventType.ResetProcessingInput,
    EventType.ProcessingInputFault,
    EventType.StimulationAck,
}


def _registered_handlers(eng: NeuroEngine) -> dict:
    eng._ready_modules.update(("acquisition", "processing"))
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
# _start: starts the foundation without business modules
# ---------------------------------------------------------------------------

class TestStart:
    @pytest.mark.asyncio
    async def test_starts_foundation_without_business_workers(self):
        eng = _make_engine()
        await eng._start()
        eng.acquisition_worker.start.assert_not_called()
        eng.signal_processing_worker.start.assert_not_called()
        eng.event_bus.start.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_event_bus_start_failure_rolls_back_started_resources(self):
        eng = _make_engine()
        eng.event_bus.start.side_effect = OSError("address already in use")
        eng.acquisition_worker.is_alive.return_value = False
        eng.signal_processing_worker.is_alive.return_value = False

        with pytest.raises(OSError, match="address already in use"):
            await eng._start()

        eng.event_bus.stop.assert_awaited_once()
        eng.acquisition_worker.join.assert_not_called()
        eng.signal_processing_worker.join.assert_not_called()
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
        eng.event_bus.start.side_effect = OSError("bind failed")
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
        eng._ready_modules.add("processing")
        eng._register_event_listeners()
        received = asyncio.get_running_loop().create_future()
        unrelated = asyncio.get_running_loop().create_future()
        bus.register_event_listener(EventType.CommandResult, received.set_result)
        bus.register_event_listener(EventType.TrialCompleted, unrelated.set_result)
        await bus.start()
        try:
            bus.put_event(Event(EventType.PrepareSignalProcessing, CommandRequest("prepare", {})))
            bus.put_event(Event(EventType.TrialCompleted, {"trial_number": 1}))
            result_event = await asyncio.wait_for(received, timeout=1)
            assert result_event.data.command_id == "prepare"
            assert result_event.data.error_code == "COMMAND_QUEUE_FULL"
            assert (await asyncio.wait_for(unrelated, timeout=1)).data == {"trial_number": 1}
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

        target_queue = getattr(eng, queue_name)
        target_queue.put_nowait.assert_called_once_with((command, payload))

        other_queue = (
            eng.processor_command_queue
            if queue_name == "acquisition_command_queue"
            else eng.acquisition_command_queue
        )
        other_queue.put_nowait.assert_not_called()

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
            (AcquisitionCommand.stop_acquisition, None)
        )
        eng.processor_command_queue.put_nowait.assert_called_once_with(
            (SignalProcessorCommand.should_stop, None)
        )
        eng.acquisition_worker.join.assert_called_once_with(timeout=3)
        eng.signal_processing_worker.join.assert_called_once_with(timeout=3)
        eng.acquisition_worker.terminate.assert_not_called()
        eng.signal_processing_worker.terminate.assert_not_called()
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

        eng.acquisition_worker.start.assert_not_called()
        eng.signal_processing_worker.start.assert_not_called()
        eng.event_bus.start.assert_awaited_once()
        eng.event_bus.stop.assert_awaited_once()
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


def _ack(command="StartStimTrial", status="ok", trial_params=None, message=None) -> Event:
    return Event(EventType.StimulationAck,
                 {"command": command, "status": status,
                  "trial_params": {"event_id": 3} if trial_params is None else trial_params, "message": message})


class TestStimulationFeedbackHandler:
    def test_ok_ack_counted(self):
        from omnibci.infrastructure.neuroengine.stimulation_feedback import StimulationFeedbackHandler

        h = StimulationFeedbackHandler()
        h.on_ack(_ack(status="ok"))
        assert h.ok_count == 1 and h.error_count == 0
        assert h.last_ack["trial_params"] == {"event_id": 3}

    def test_error_ack_counted(self):
        from omnibci.infrastructure.neuroengine.stimulation_feedback import StimulationFeedbackHandler

        h = StimulationFeedbackHandler()
        h.on_ack(_ack(status="error", message="renderer crashed"))
        assert h.error_count == 1 and h.ok_count == 0
        assert h.last_ack["message"] == "renderer crashed"

    def test_mixed_sequence(self):
        from omnibci.infrastructure.neuroengine.stimulation_feedback import StimulationFeedbackHandler

        h = StimulationFeedbackHandler()
        for s in ("ok", "ok", "error", "ok"):
            h.on_ack(_ack(status=s))
        assert (h.ok_count, h.error_count) == (3, 1)

    def test_missing_status_defaults_ok(self):
        from omnibci.infrastructure.neuroengine.stimulation_feedback import StimulationFeedbackHandler

        h = StimulationFeedbackHandler()
        h.on_ack(Event(EventType.StimulationAck, {"command": "X"}))
        assert h.ok_count == 1

    def test_non_dict_payload_tolerated(self):
        from omnibci.infrastructure.neuroengine.stimulation_feedback import StimulationFeedbackHandler

        h = StimulationFeedbackHandler()
        h.on_ack(Event(EventType.StimulationAck, None))
        assert h.ok_count == 1
        assert h.last_ack == {}


class TestEngineStimulationFeedback:
    def test_handler_instance_present(self):
        from omnibci.infrastructure.neuroengine.stimulation_feedback import StimulationFeedbackHandler

        eng = _make_engine()
        assert isinstance(eng.stim_feedback, StimulationFeedbackHandler)


class _TranslatorBus:
    def __init__(self):
        self.put = []

    def put_event(self, event):
        self.put.append(event)


class TestActuationCommandTranslator:
    def test_trial_outcome_payload_emits_command(self):
        from omnibci.application.events import TrialOutcome
        from omnibci.domain.classification import Classification
        from omnibci.infrastructure.neuroengine.actuation_translator import ActuationCommandTranslator

        bus = _TranslatorBus()
        tr = ActuationCommandTranslator(bus)
        outcome = TrialOutcome(
            generation="run-1",
            trial_number=3,
            classification=Classification(target_id=5, event_id=2, confidence=0.9),
        )
        tr.on_trial_completed(Event(EventType.TrialCompleted, outcome))
        assert len(bus.put) == 1
        cmd = bus.put[0]
        assert cmd.type == EventType.ActuationCommand
        assert cmd.data.parameters["target_id"] == 5
        assert cmd.data.parameters["event_id"] == 2
        assert cmd.data.parameters["confidence"] == 0.9

    def test_bare_classification_payload_is_rejected(self):
        from omnibci.domain.classification import Classification
        from omnibci.infrastructure.neuroengine.actuation_translator import ActuationCommandTranslator

        bus = _TranslatorBus()
        tr = ActuationCommandTranslator(bus)
        tr.on_trial_completed(Event(EventType.TrialCompleted, Classification(5, 2, 0.9)))
        assert bus.put == []

    def test_payload_without_target_id_emits_nothing(self):
        from omnibci.infrastructure.neuroengine.actuation_translator import ActuationCommandTranslator

        bus = _TranslatorBus()
        tr = ActuationCommandTranslator(bus)
        tr.on_trial_completed(Event(EventType.TrialCompleted, object()))
        assert bus.put == []

    def test_commands_carry_unique_identity_and_issued_at(self):
        from omnibci.application.events import TrialOutcome
        from omnibci.domain.classification import Classification
        from omnibci.infrastructure.neuroengine.actuation_translator import ActuationCommandTranslator

        bus = _TranslatorBus()
        tr = ActuationCommandTranslator(bus)
        before = time.time()
        for tid in (1, 2, 3):
            tr.on_trial_completed(Event(EventType.TrialCompleted,
                TrialOutcome("run", tid, Classification(tid, 1, 0.9))))
        after = time.time()
        assert len({event.data.command_id for event in bus.put}) == 3
        for event in bus.put:
            assert before <= event.data.issued_at <= after

    def test_skipped_payload_does_not_emit_an_intent(self):
        from omnibci.application.events import TrialOutcome
        from omnibci.domain.classification import Classification
        from omnibci.infrastructure.neuroengine.actuation_translator import ActuationCommandTranslator

        bus = _TranslatorBus()
        tr = ActuationCommandTranslator(bus)
        tr.on_trial_completed(Event(EventType.TrialCompleted, object()))
        tr.on_trial_completed(Event(EventType.TrialCompleted, TrialOutcome("run", 1, Classification(1, 1))))
        assert len(bus.put) == 1
        assert bus.put[0].data.origin["trial_number"] == 1


class TestActuationFeedbackHandler:
    def test_ack_state_fault_accounting(self):
        from omnibci.infrastructure.neuroengine.actuation_feedback import ActuationFeedbackHandler

        h = ActuationFeedbackHandler()
        h.on_feedback(Event(EventType.ActuationAck, {"status": "ok", "command_id": "1"}))
        h.on_feedback(Event(EventType.ActuationState, {"pos": 1.0}))
        h.on_feedback(Event(EventType.ActuationFault, {"code": "ESTOP"}))
        assert h.ack_count == 1
        assert h.fault_count == 1
        assert h.last_state == {"pos": 1.0}
        assert h.last_fault == {"code": "ESTOP"}

    def test_acks_bucketed_by_status(self):
        from omnibci.infrastructure.neuroengine.actuation_feedback import ActuationFeedbackHandler

        h = ActuationFeedbackHandler()
        h.on_feedback(Event(EventType.ActuationAck, {"status": "ok", "command_id": "1"}))
        h.on_feedback(Event(EventType.ActuationAck, {"status": "superseded", "command_id": "2"}))
        h.on_feedback(Event(EventType.ActuationAck, {"status": "stale", "command_id": "3"}))
        h.on_feedback(Event(EventType.ActuationAck, {"status": "ok", "command_id": "4"}))
        assert h.ack_count == 4
        assert (h.ok_count, h.superseded_count, h.stale_count) == (2, 1, 1)
        assert h.last_acked_command_id == "4"

    def test_ack_without_status_and_identity_is_rejected(self):
        from omnibci.infrastructure.neuroengine.actuation_feedback import ActuationFeedbackHandler

        h = ActuationFeedbackHandler()
        h.on_feedback(Event(EventType.ActuationAck, {"ok": True}))
        assert h.ack_count == 0 and h.last_acked_command_id is None

    @pytest.mark.parametrize("status", [[], {}, None, True])
    def test_malformed_ack_status_is_ignored(self, status):
        from omnibci.infrastructure.neuroengine.actuation_feedback import ActuationFeedbackHandler

        handler = ActuationFeedbackHandler()
        handler.on_feedback(Event(EventType.ActuationAck, {"status": status, "command_id": "1"}))
        assert handler.ack_count == 0

    def test_last_command_identity_is_retained(self):
        from omnibci.infrastructure.neuroengine.actuation_feedback import ActuationFeedbackHandler

        h = ActuationFeedbackHandler()
        h.on_feedback(Event(EventType.ActuationAck, {"status": "ok", "command_id": "1"}))
        h.on_feedback(Event(EventType.ActuationAck, {"status": "ok", "command_id": "5"}))
        assert h.last_acked_command_id == "5"


class TestActuationPeerConfig:
    def test_routes_and_transport(self):
        from omnibci.infrastructure.ipc.peer_transport import ListenTransport
        from omnibci.infrastructure.neuroengine.gateway.peers import ACTUATION, actuation_peer_config

        cfg = actuation_peer_config(host="127.0.0.1", port=8001)
        assert cfg.name == ACTUATION
        assert isinstance(cfg.transport, ListenTransport)
        assert cfg.transport._server.max_clients == 1
        assert cfg.routes.outbound == frozenset({EventType.ActuationCommand, EventType.SettleActuation,
                                                EventType.ResumeActuation})
        assert cfg.routes.inbound == frozenset({
            EventType.ActuationAck, EventType.ActuationState, EventType.ActuationFault,
            EventType.CommandResult,
        })


class TestBuilderActuationFoundation:
    def _build(self, **cfg_kwargs):
        from unittest.mock import patch
        from omnibci.infrastructure.neuroengine.builder import NeuroEngineBuilder, NeuroEngineConfig
        with (
            patch("omnibci.infrastructure.neuroengine.builder.AcquisitionWorker", return_value=MagicMock()),
            patch("omnibci.infrastructure.neuroengine.builder.SignalProcessingWorker", return_value=MagicMock()),
        ):
            return NeuroEngineBuilder(NeuroEngineConfig(**cfg_kwargs), settings_snapshot={}).build()

    def test_transport_is_deferred_and_commands_disabled_by_default(self):
        eng = self._build()
        assert not hasattr(eng, "peer_gateway")
        assert not hasattr(eng, "actuation_translator")
        assert not hasattr(eng, "actuation_feedback")

    def test_output_parameters_do_not_configure_foundation_listeners(self):
        from omnibci.infrastructure.neuroengine.builder import NeuroEngineBuilder
        eng = NeuroEngineBuilder(settings_snapshot={"ACTUATION.server_port": "invalid-until-prepared"}).build()
        assert not hasattr(eng, "peer_gateway")
