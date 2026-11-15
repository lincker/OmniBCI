"""The foundation supports independent worker lifetimes and source-only reads."""
import asyncio
from dataclasses import replace
from queue import Queue
from threading import Event as ThreadEvent
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import numpy as np
import pytest

from omnibci.application.events import CommandRequest, StreamDrainReceipt
from omnibci.domain.classification import Classification
from omnibci.infrastructure.acquisition.acquisition import AcquisitionCommand, AcquisitionRuntime
from omnibci.infrastructure.ipc.data_manager import DataManager
from omnibci.infrastructure.signal_processing.signal_processor import SignalProcessor
from omnibci.shared.events import Event, EventType
from tests.neuroengine.test_channel_mapping import _plan, _processor, _receipt, _snapshot
from tests.neuroengine.test_neuro_engine import _make_engine


@pytest.mark.asyncio
async def test_acquisition_starts_and_stops_without_starting_processing_or_outputs():
    engine = _make_engine()
    engine.acquisition_worker.is_alive.return_value = True
    engine.acquisition_worker.join.side_effect = lambda **_: setattr(
        engine.acquisition_worker.is_alive, "return_value", False)
    await engine._start()
    try:
        await engine.start_module("acquisition", {})
        assert engine.is_module_running("acquisition")
        assert not engine.is_module_running("processing")
        assert not engine._actuation_active
        await engine.stop_module("acquisition")
        assert not engine.is_module_running("acquisition")
        assert engine._started
        engine.signal_processing_worker.start.assert_not_called()
        engine.event_bus.stop.assert_not_awaited()
        engine.peer_gateway.stop.assert_not_awaited()
    finally:
        await engine._stop()


@pytest.mark.parametrize("event_type", [EventType.ConnectDataProvider, EventType.PrepareSignalProcessing])
def test_stopped_module_rejects_correlated_command_without_queueing(event_type):
    engine = _make_engine()
    engine._register_event_listeners()
    handler = next(call.args[1] for call in engine.event_bus.register_event_listener.call_args_list
                   if call.args[0] == event_type)
    handler(Event(event_type, CommandRequest("request")))
    result = engine.event_bus.put_event.call_args.args[0].data
    assert not result.ok
    assert result.command_id == "request" and result.error_code == "MODULE_NOT_RUNNING"
    engine.acquisition_command_queue.put_nowait.assert_not_called()
    engine.processor_command_queue.put_nowait.assert_not_called()


@pytest.mark.asyncio
async def test_source_only_read_does_not_accumulate_processing_data_and_confirms_own_stop():
    active = ThreadEvent()
    data = DataManager(Queue(), processing_active=active)
    counters = [0, 0, -1]
    source = SimpleNamespace(
        is_connected=True, is_streaming=True,
        read_chunk=AsyncMock(side_effect=[np.zeros((2, 4)), np.zeros((2, 3)), None]),
        stop_stream=AsyncMock(),
    )
    source.stop_stream.side_effect = lambda: setattr(source, "is_streaming", False)
    events = []
    acquisition = AcquisitionRuntime(
        source, Queue(), data, SimpleNamespace(put_event=events.append),
        source_id="source", preparation_required=True, processing_active=active,
        runtime_counters=counters, generation="run", acquisition_revision="source-v1",
        system_revision="system",
    )
    await acquisition.fetch()
    assert data.empty()
    assert acquisition.stream_sample_count == 7
    assert counters[2] == 1
    await acquisition.execute((AcquisitionCommand.stop_and_drain, CommandRequest("drain")))
    result = events[-1].data
    assert result.ok and result.command_id == "drain"
    receipt = StreamDrainReceipt.from_value(result.data)
    assert receipt.processing_revision is None
    assert receipt.last_sequence == 1 and receipt.completed_trials == 0
    assert data.empty()


def test_processing_restart_preserves_trial_epoch_and_source_sequence_numbers():
    counters = [0, 0, 12]
    events = []
    for expected in (1, 2):
        processor = SignalProcessor(
            None, None, Queue(), DataManager(Queue()), SimpleNamespace(put_event=events.append),
            generation="foundation", processing_revision="p", acquisition_revision="a",
            runtime_counters=counters,
        )
        assert processor._last_sequence == 12
        processor.record_epoch_started(1, 2)
        processor.current_epoch = SimpleNamespace(processing_revision="p", acquisition_revision="a")
        outcome = processor.complete_trial(Classification(0, 1))
        assert outcome.trial_number == expected
        assert processor._epoch_number == expected
    assert [(event.data.generation, event.data.trial_number) for event in events] == [
        ("foundation", 1), ("foundation", 2),
    ]


def test_processing_adopts_new_acquisition_context_without_restarting(monkeypatch):
    from omnibci.infrastructure.signal_processing import signal_configuration

    original = _plan()
    processor = _processor(original)
    changed = replace(original, acquisition_revision="source-v2")
    snapshot = _snapshot(changed)
    snapshot["CONFIG.module_revisions"]["processing"] = "revision-one"
    snapshot["STIMULATION.renderer"] = "must-not-be-adopted"
    loader = MagicMock(return_value=(object(), object(), []))
    monkeypatch.setattr(signal_configuration, "load_signal_pipeline", loader)
    processor._prepare_signal(CommandRequest("prepare", {
        "plan": changed.to_dict(), "receipt": _receipt(changed), "settings_snapshot": snapshot,
    }))
    result = processor.event_bus.put_event.call_args.args[0].data
    assert result.ok, result.message
    assert processor.acquisition_revision == "source-v2"
    assert processor.processing_revision == "revision-one"
    assert "STIMULATION.renderer" not in processor._settings_snapshot


@pytest.mark.parametrize("key", ["CONFIG.generation", "CONFIG.system_revision", "CONFIG.module_revisions"])
def test_processing_rejects_source_context_for_another_runtime(key):
    plan = _plan()
    processor = _processor(plan)
    snapshot = _snapshot(plan)
    snapshot[key] = {"processing": "other"} if key == "CONFIG.module_revisions" else "other"
    processor._prepare_signal(CommandRequest("prepare", {
        "plan": plan.to_dict(), "receipt": _receipt(plan), "settings_snapshot": snapshot,
    }))
    assert not processor.event_bus.put_event.call_args.args[0].data.ok
    assert processor.acquisition_revision == "revision-one"


@pytest.mark.asyncio
async def test_module_lifecycle_can_be_called_from_application_loop():
    engine = _make_engine()
    engine.acquisition_worker.is_alive.return_value = True
    engine.acquisition_worker.join.side_effect = lambda **_: setattr(
        engine.acquisition_worker.is_alive, "return_value", False)
    await engine._start()
    try:
        await asyncio.to_thread(asyncio.run, engine.start_module("acquisition", {}))
        assert engine.is_module_running("acquisition")
        await asyncio.to_thread(asyncio.run, engine.stop_module("acquisition"))
        assert not engine.is_module_running("acquisition")
    finally:
        await engine._stop()
