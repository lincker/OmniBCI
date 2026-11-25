"""Processing lifecycle changes leave source reading and unrelated peers alone."""
import asyncio
from queue import Queue
from threading import Event as ThreadEvent
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import numpy as np

import pytest

from omnibci.application.events import SignalPreparationReceipt, CommandRequest, ProcessingInputStart, StreamBarrier, StreamDrainReceipt
from omnibci.domain.signal import SignalChunk
from omnibci.infrastructure.acquisition.acquisition import AcquisitionCommand, AcquisitionRuntime
from omnibci.infrastructure.ipc.data_manager import DataManager
from omnibci.infrastructure.neuroengine.gateway.peer_gateway import PeerGateway
from tests.neuroengine.test_module_peer_reconfiguration import config
from tests.neuroengine.test_peer_gateway import FakeBus, FakeTransport
from tests.neuroengine.test_channel_mapping import _observation, _plan, _processor, _receipt
from tests.neuroengine.test_neuro_engine import _make_engine
from omnibci.infrastructure.signal_processing import signal_configuration
from omnibci.shared.events import Event, EventType


def test_input_boundary_is_delivered_when_sample_gate_is_closed():
    data = DataManager(Queue(), processing_active=ThreadEvent())
    boundary = StreamBarrier("detach", "engine", "acquisition", 3)
    data.put_data(boundary)
    assert data.get_data(timeout=0.001) == boundary


@pytest.mark.asyncio
async def test_source_can_start_without_processing_preparation():
    gate = ThreadEvent()
    gate.set()
    source = SimpleNamespace(trigger_mode="events", resume_mode="restart-only", connect=AsyncMock(),
                             start_stream=AsyncMock(), read_chunk=AsyncMock())
    acquisition = AcquisitionRuntime(
        source, Queue(), DataManager(Queue(), gate), source_id="test",
        processing_active=gate,
    )
    acquisition.fetch = AsyncMock()
    await acquisition._on_connect()
    await acquisition._on_start_stream()
    await acquisition.fetch_task
    assert acquisition._streaming


@pytest.mark.asyncio
async def test_slow_stimulation_replacement_does_not_block_actuation():
    gateway = PeerGateway(FakeBus())
    gateway.add_peer(config("stimulation", FakeTransport()))
    gateway.add_peer(config("actuation", FakeTransport()))
    await gateway.start()
    entered, release = asyncio.Event(), asyncio.Event()
    replacing = asyncio.create_task(gateway.replace_peer(
        "stimulation", config("stimulation", FakeTransport(start_entered=entered, start_release=release))))
    try:
        await entered.wait()
        await asyncio.wait_for(gateway.replace_peer("actuation", config("actuation", FakeTransport())), 0.1)
    finally:
        release.set()
        await replacing
        await gateway.stop()


def input_pair(monkeypatch, data=None):
    plan = _plan()
    if data is None:
        data = DataManager(Queue(), processing_active=ThreadEvent())
    gate = data.processing_active
    events = []
    source = SimpleNamespace(trigger_mode="events", resume_mode="restart-only",
                             start_stream=AsyncMock(), stop_stream=AsyncMock())
    acquisition = AcquisitionRuntime(
        source, Queue(), data, SimpleNamespace(put_event=events.append),
        source_id="test-source",
        processing_active=gate, generation="generation-one", engine_revision="engine",
        acquisition_revision=plan.acquisition_revision,
    )
    acquisition.connection_id = "connection-one"
    acquisition._connected = acquisition._streaming = True
    acquisition._observation = _observation(plan)
    processor = _processor(plan)
    processor.data_manager = data
    processor.event_bus = SimpleNamespace(put_event=events.append)
    monkeypatch.setattr(signal_configuration, "load_signal_pipeline", MagicMock(return_value=(MagicMock(), object(), [])))
    receipt = {**_receipt(plan), "input_id": "input-one"}
    processor._prepare_signal(CommandRequest("prepare", {"plan": plan.to_dict(), "receipt": receipt}))
    assert events.pop().data.ok
    return acquisition, processor, data, receipt, events


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value", [("connection_id", "stale-connection"),
                                         ("source_fingerprint", "f" * 64)])
async def test_source_owner_rejects_stale_preparation_before_any_input_delivery(monkeypatch, field, value):
    acquisition, processor, data, receipt, events = input_pair(monkeypatch)
    stale = {**receipt, field: value}
    await acquisition.execute((AcquisitionCommand.attach_processing, CommandRequest("attach", stale)))
    assert events[-1].data.error_code == "PROCESSING_INPUT_REJECTED"
    assert not data.has_consumer
    data.put_data(SignalChunk(0, 1, np.zeros((4, 3))))
    assert data.get_data(0) is None
    assert processor._signal_receipt.to_dict() == receipt


@pytest.mark.asyncio
async def test_attach_and_detach_use_fifo_without_stopping_source_or_replaying_gap(monkeypatch):
    acquisition, processor, data, receipt, events = input_pair(monkeypatch)
    acquisition._last_sequence = 4
    await acquisition.execute((AcquisitionCommand.attach_processing, CommandRequest("attach", receipt)))
    assert not events  # Only the consumer can confirm successful attachment.
    await processor.next_chunk()
    assert events.pop().data.data == {**receipt, "attached": True, "last_sequence": 4}
    assert processor._last_sequence == 4
    data.put_data(SignalChunk(0, 5, np.zeros((4, 3))))
    acquisition._last_sequence = acquisition._last_processing_sequence = 5
    identity = {"generation": acquisition.generation, "engine_revision": acquisition.engine_revision,
                "expected_revision": acquisition.acquisition_revision}
    await acquisition.execute((AcquisitionCommand.detach_processing, CommandRequest("detach", identity)))
    assert not data.has_consumer
    # A keeps advancing after the boundary. This sample is never queued for P.
    data.put_data(SignalChunk(0, 6, np.zeros((4, 3))))
    acquisition._last_sequence = 6
    assert (await processor.next_chunk()).sequence_id == 5
    await processor.next_chunk()
    result = events.pop().data
    assert result.ok and result.command == EventType.DetachProcessingInput.name
    assert StreamDrainReceipt.from_value(result.data).last_sequence == 5
    assert not processor._input_attached
    assert data.empty()
    receipt = {**receipt, "input_id": "input-two"}
    processor._signal_receipt = SignalPreparationReceipt.from_value(receipt)
    await acquisition.execute((AcquisitionCommand.attach_processing, CommandRequest("reattach", receipt)))
    await processor.next_chunk()
    assert processor._last_sequence == 6
    assert events.pop().data.ok
    acquisition.data_source.stop_stream.assert_not_called()
    acquisition.data_source.start_stream.assert_not_called()
    assert acquisition._streaming


@pytest.mark.asyncio
async def test_unprepared_or_stale_input_does_not_reach_processing(monkeypatch):
    acquisition, processor, data, receipt, events = input_pair(monkeypatch)
    data.buffer.put(ProcessingInputStart("stale", SignalPreparationReceipt.from_value({**receipt, "input_id": "old-input"}), 10))
    await processor.next_chunk()
    assert not events.pop().data.ok
    data.buffer.put(SignalChunk(0, 11, np.zeros((4, 3))))
    assert await processor.next_chunk() is None
    assert not processor._input_attached
    await acquisition.execute((AcquisitionCommand.attach_processing,
                               CommandRequest("stale-source", {**receipt, "connection_id": "other-source"})))
    assert not events.pop().data.ok
    assert not data.has_consumer and data.empty()


@pytest.mark.asyncio
async def test_failed_role_cleanup_does_not_block_another_peer():
    gateway = PeerGateway(FakeBus())
    await gateway.start()
    broken = FakeTransport(fail_start=True, fail_stop=True)
    try:
        with pytest.raises(BaseExceptionGroup):
            await gateway.replace_peer("stimulation", config("stimulation", broken))
        await gateway.replace_peer("actuation", config("actuation", FakeTransport()))
        assert gateway.is_peer_ready("actuation")
        assert gateway.peer("stimulation").has_lifecycle_resources
    finally:
        broken.fail_stop = False
        await gateway.stop()


@pytest.mark.asyncio
async def test_processing_readiness_wait_does_not_block_acquisition_stop():
    engine = _make_engine()
    for worker in (engine.acquisition_worker, engine.signal_processing_worker):
        worker.is_alive.return_value = True
        worker.join.side_effect = lambda worker=worker, **_: setattr(worker.is_alive, "return_value", False)
    await engine._start()
    await engine.start_module("acquisition", {})
    entered, release = asyncio.Event(), asyncio.Event()
    async def wait_ready(channels):
        entered.set()
        await release.wait()
    engine._wait_for_workers = wait_ready
    starting = asyncio.create_task(engine.start_module("processing", {}))
    try:
        await entered.wait()
        await asyncio.wait_for(engine.stop_module("acquisition"), 0.1)
        assert not engine.is_module_running("acquisition")
        assert not engine.is_module_running("processing")
    finally:
        release.set()
        await starting
        await engine._stop()


def test_source_reset_rejects_old_identity_and_preserves_processing_runtime(monkeypatch):
    _, processor, _, receipt, events = input_pair(monkeypatch)
    processor._reset_input(CommandRequest("wrong-source", {
        "generation": "generation-one", "engine_revision": "engine",
        "expected_acquisition_revision": "old-source",
    }))
    assert not events.pop().data.ok
    assert processor._signal_receipt.to_dict() == receipt
    processor._reset_input(CommandRequest("old-input-fault", {
        "generation": "generation-one", "engine_revision": "engine",
        "expected_acquisition_revision": receipt["acquisition_revision"], "input_id": "old-input",
    }))
    assert not events.pop().data.ok
    assert processor._signal_receipt.to_dict() == receipt
    processor._reset_input(CommandRequest("source-exited", {
        "generation": "generation-one", "engine_revision": "engine",
        "expected_acquisition_revision": receipt["acquisition_revision"],
    }))
    result = events.pop().data
    assert result.ok and result.data["attached"] is False
    assert "drained" not in result.data
    assert processor._signal_plan is None and processor._signal_receipt is None
    assert not processor._should_stop


@pytest.mark.asyncio
async def test_full_processing_queue_does_not_stop_acquisition_or_preview():
    gate = ThreadEvent()
    gate.set()
    data = DataManager(Queue(maxsize=1), gate)
    events = []
    source = SimpleNamespace(trigger_mode="events", resume_mode="restart-only",
                             read_chunk=AsyncMock(side_effect=[np.zeros((2, 3)) for _ in range(3)] + [None]),
                             stop_stream=AsyncMock())
    source.stop_stream.side_effect = lambda: setattr(source, "is_streaming", False)
    acquisition = AcquisitionRuntime(source, Queue(), data, SimpleNamespace(put_event=events.append),
                                     source_id="source", generation="run", engine_revision="engine",
                                     acquisition_revision="acquisition", processing_active=gate)
    acquisition._processing_receipt = SignalPreparationReceipt.from_value({**_receipt(_plan()), "input_id": "input-one"})
    acquisition.preview = SimpleNamespace(active=True, offer=MagicMock(), finish=MagicMock())
    await acquisition.fetch()
    assert source.read_chunk.await_count == 4
    assert acquisition.stream_chunk_count == 3
    assert acquisition.preview.offer.call_count == 3
    assert acquisition._fetch_failure is None
    assert not gate.is_set()
    faults = [event for event in events if event.type.name == "ProcessingInputFault"]
    assert len(faults) == 1 and faults[0].data["input_id"] == "input-one"


def test_engine_routes_input_fault_with_attachment_identity_only_to_processing():
    engine = _make_engine(generation="run", engine_revision="engine",
                          module_revisions={"acquisition": "acquisition", "processing": "processing"})
    engine._ready_modules.add("processing")
    engine.signal_processing_worker.is_alive.return_value = True
    fault = {"generation": "run", "engine_revision": "engine", "acquisition_revision": "acquisition",
             "input_id": "failed-input", "reason": "queue full"}
    engine._on_processing_input_fault(Event(EventType.ProcessingInputFault, fault))
    command, request = engine.processor_command_queue.put_nowait.call_args.args[0]
    assert command.value == "reset_processing_input"
    assert request.payload["input_id"] == "failed-input"
    assert request.payload["expected_acquisition_revision"] == "acquisition"
    engine.acquisition_command_queue.put_nowait.assert_not_called()
    engine.processor_command_queue.reset_mock()
    engine._on_processing_input_fault(Event(EventType.ProcessingInputFault, {**fault, "generation": "old-run"}))
    engine.processor_command_queue.put_nowait.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("edit", [
    lambda value: value.pop("processing_revision"),
    lambda value: value.update(processing_revision=""),
    lambda value: value.update(processing_revision=True),
    lambda value: value.update(extra="unowned"),
    lambda value: value.update(plan_fingerprint="z" * 64),
    lambda value: value.update(input_id=" "),
])
async def test_invalid_preparation_never_opens_the_acquisition_gate(monkeypatch, edit):
    acquisition, processor, data, receipt, events = input_pair(monkeypatch)
    edit(receipt)
    await acquisition.execute((AcquisitionCommand.attach_processing, CommandRequest("attach", receipt)))
    assert events and not events[-1].data.ok
    assert not data.has_consumer
    assert data.empty()
    assert acquisition._processing_receipt is None


@pytest.mark.parametrize("edit", [
    lambda value: value.update(source_fingerprint="z" * 64),
    lambda value: value.update(input_id=" "),
])
def test_invalid_preparation_cannot_replace_a_processing_pipeline(monkeypatch, edit):
    _, processor, _, receipt, events = input_pair(monkeypatch)
    original_decoder = processor._decoder
    edit(receipt)
    processor._prepare_signal(CommandRequest("replace", {
        "plan": processor._signal_plan.to_dict(), "receipt": receipt,
    }))
    assert not events[-1].data.ok
    assert processor._decoder is original_decoder


@pytest.mark.asyncio
async def test_prepared_identity_crosses_fifo_as_an_immutable_value(monkeypatch):
    from dataclasses import FrozenInstanceError, replace

    acquisition, _, data, payload, events = input_pair(monkeypatch)
    receipt = SignalPreparationReceipt.from_value(payload)
    await acquisition.execute((AcquisitionCommand.attach_processing, CommandRequest("attach", payload)))
    marker = data.get_data(0)
    assert marker.receipt == acquisition._processing_receipt == receipt
    assert not events
    payload["input_id"] = "changed-after-admission"
    encoded = receipt.to_dict()
    encoded["connection_id"] = "changed-after-projection"
    assert marker.receipt.input_id == receipt.input_id == "input-one"
    assert receipt.connection_id == "connection-one"
    assert SignalPreparationReceipt.from_value(receipt.to_dict()) == receipt
    with pytest.raises(FrozenInstanceError):
        receipt.input_id = "mutated"
    with pytest.raises(ValueError):
        replace(receipt, processing_revision=None)
    with pytest.raises(ValueError):
        SignalPreparationReceipt.from_value({**receipt.to_dict(), "extra": "unowned"})


from dataclasses import replace as dc_replace
from queue import Full
from omnibci.domain.classification import Classification
from omnibci.infrastructure.neuroengine.builder import NeuroEngineBuilder, NeuroEngineConfig
from omnibci.infrastructure.signal_processing.signal_processor import SignalProcessor
from tests.neuroengine.test_channel_mapping import _snapshot


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
        trigger_mode="events", resume_mode="restart-only",
        read_chunk=AsyncMock(side_effect=[np.zeros((2, 4)), np.zeros((2, 3)), None]),
        stop_stream=AsyncMock(),
    )
    events = []
    acquisition = AcquisitionRuntime(
        source, Queue(), data, SimpleNamespace(put_event=events.append),
        source_id="source", processing_active=active,
        runtime_counters=counters, generation="run", acquisition_revision="source-v1",
        engine_revision="engine",
    )
    acquisition._connected = acquisition._streaming = True
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
    original = _plan()
    processor = _processor(original)
    changed = dc_replace(original, acquisition_revision="source-v2")
    snapshot = _snapshot(changed)
    snapshot["CONFIG.module_revisions"]["processing"] = "revision-one"
    snapshot["STIMULATION.renderer"] = "must-not-be-adopted"
    loader = MagicMock(return_value=(MagicMock(), object(), []))
    monkeypatch.setattr(signal_configuration, "load_signal_pipeline", loader)
    processor._prepare_signal(CommandRequest("prepare", {
        "plan": changed.to_dict(), "receipt": _receipt(changed), "settings_snapshot": snapshot,
    }))
    result = processor.event_bus.put_event.call_args.args[0].data
    assert result.ok, result.message
    assert processor.acquisition_revision == "source-v2"
    assert processor.processing_revision == "revision-one"
    assert "STIMULATION.renderer" not in processor._settings_snapshot


@pytest.mark.parametrize("key", ["CONFIG.generation", "CONFIG.engine_revision", "CONFIG.module_revisions"])
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


@pytest.fixture
def bounded_data():
    engine = NeuroEngineBuilder(NeuroEngineConfig(queue_size=2), settings_snapshot={}).build()
    try:
        yield engine.data_manager
    finally:
        for queue in (engine.acquisition_command_queue, engine.processor_command_queue,
                      *engine._owned_ipc_queues):
            engine._close_queue(queue)


def test_builder_bounds_signal_and_boundary_messages_in_one_fifo(bounded_data):
    data = bounded_data
    start = ProcessingInputStart("attach", SignalPreparationReceipt.from_value({**_receipt(_plan()), "input_id": "input-one"}), -1)
    barrier = StreamBarrier("detach", "engine", "acquisition", 0)
    data.put_data(start)
    data.processing_active.set()
    data.put_data(SignalChunk(0, 0, np.zeros((4, 3))))
    with pytest.raises(Full):
        data.put_data(barrier)
    assert data.get_data(timeout=1) == start
    data.processing_active.clear()
    assert data.put_data(SignalChunk(0, 1, np.zeros((4, 3)))) is False
    data.put_data(barrier)
    assert data.get_data(timeout=1).sequence_id == 0
    assert data.get_data(timeout=1) == barrier
    assert data.get_data(timeout=0.01) is None


@pytest.mark.parametrize("capacity", [0, -1])
def test_nonpositive_capacity_cannot_create_an_unbounded_signal_queue(capacity):
    with pytest.raises(ValueError, match="queue_size must be positive"):
        NeuroEngineConfig(queue_size=capacity)


@pytest.mark.asyncio
async def test_full_signal_queue_preserves_source_reading_and_preview(monkeypatch, bounded_data):
    acquisition, processor, data, receipt, events = input_pair(monkeypatch, bounded_data)
    await acquisition.execute((AcquisitionCommand.attach_processing, CommandRequest("attach", receipt)))
    await processor.next_chunk(timeout=1)
    events.clear()
    acquisition.data_source.read_chunk = AsyncMock(side_effect=[np.zeros((4, 3)) for _ in range(4)] + [None])
    acquisition.data_source.stop_stream.side_effect = lambda: setattr(acquisition.data_source, "is_streaming", False)
    acquisition.preview = SimpleNamespace(active=True, offer=MagicMock(), finish=MagicMock())

    await acquisition.fetch()

    assert acquisition.data_source.read_chunk.await_count == 5
    assert acquisition.stream_chunk_count == 4
    assert acquisition.preview.offer.call_count == 4
    assert acquisition._fetch_failure is None
    assert not data.has_consumer
    faults = [event.data for event in events if event.type is EventType.ProcessingInputFault]
    assert len(faults) == 1 and faults[0]["input_id"] == receipt["input_id"]
    assert [data.get_data(timeout=1).sequence_id for _ in range(2)] == [0, 1]
    assert data.get_data(timeout=0.01) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("restart_processor", [False, True])
async def test_new_attachment_discards_old_backlog_and_recovers_after_full_boundary(
        monkeypatch, bounded_data, restart_processor):
    acquisition, processor, data, old_receipt, events = input_pair(monkeypatch, bounded_data)
    plan = processor._signal_plan
    await acquisition.execute((AcquisitionCommand.attach_processing, CommandRequest("old-attach", old_receipt)))
    data.put_data(SignalChunk(0, 0, np.zeros((4, 3))))
    acquisition._last_sequence = acquisition._last_processing_sequence = 0
    identity = {"generation": acquisition.generation, "engine_revision": acquisition.engine_revision,
                "expected_revision": acquisition.acquisition_revision}
    await acquisition.execute((AcquisitionCommand.detach_processing, CommandRequest("full-detach", identity)))
    assert not events[-1].data.ok
    assert acquisition._streaming
    if restart_processor:
        processor = _processor(plan)
        processor.data_manager = data
        processor.event_bus = SimpleNamespace(put_event=events.append)
    else:
        processor._reset_input(CommandRequest("reset", {
            "generation": acquisition.generation, "engine_revision": acquisition.engine_revision,
            "expected_acquisition_revision": acquisition.acquisition_revision,
            "input_id": old_receipt["input_id"],
        }))
        assert events[-1].data.ok

    receipt = {**old_receipt, "input_id": "failed-attachment"}
    processor._prepare_signal(CommandRequest("prepare", {"plan": plan.to_dict(), "receipt": receipt}))
    assert events[-1].data.ok
    await acquisition.execute((AcquisitionCommand.attach_processing, CommandRequest("full-attach", receipt)))
    assert not events[-1].data.ok
    assert not data.has_consumer
    await processor.next_chunk(timeout=1)
    assert not events[-1].data.ok and events[-1].data.command_id == "old-attach"
    assert await processor.next_chunk(timeout=1) is None
    assert not processor._input_attached

    receipt = {**old_receipt, "input_id": "recovered-attachment"}
    processor._prepare_signal(CommandRequest("prepare-again", {"plan": plan.to_dict(), "receipt": receipt}))
    assert events[-1].data.ok
    acquisition._last_sequence = 4
    await acquisition.execute((AcquisitionCommand.attach_processing, CommandRequest("reattach", receipt)))
    assert data.has_consumer and not acquisition._processing_failed
    await processor.next_chunk(timeout=1)
    assert events[-1].data.ok and events[-1].data.command_id == "reattach"
    assert processor._last_sequence == 4
    processor._reset_input(CommandRequest("late-old-fault", {
        "generation": acquisition.generation, "engine_revision": acquisition.engine_revision,
        "expected_acquisition_revision": acquisition.acquisition_revision,
        "input_id": old_receipt["input_id"],
    }))
    assert not events[-1].data.ok
    assert processor._input_attached and data.has_consumer

    data.put_data(SignalChunk(0, 5, np.zeros((4, 3))))
    acquisition._last_sequence = acquisition._last_processing_sequence = 5
    await acquisition.execute((AcquisitionCommand.detach_processing, CommandRequest("detach", identity)))
    assert (await processor.next_chunk(timeout=1)).sequence_id == 5
    await processor.next_chunk(timeout=1)
    assert events[-1].data.ok and events[-1].data.command_id == "detach"
    assert events[-1].data.data["last_sequence"] == 5
    acquisition.data_source.stop_stream.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("command", [AcquisitionCommand.stop_and_drain, AcquisitionCommand.detach_processing])
async def test_full_queue_never_confirms_a_missing_boundary_on_retry(monkeypatch, bounded_data, command):
    acquisition, processor, data, receipt, events = input_pair(monkeypatch, bounded_data)
    acquisition.data_source.stop_stream.side_effect = lambda: setattr(acquisition.data_source, "is_streaming", False)
    await acquisition.execute((AcquisitionCommand.attach_processing, CommandRequest("attach", receipt)))
    await processor.next_chunk(timeout=1)
    events.clear()
    for sequence in range(2):
        data.put_data(SignalChunk(0, sequence, np.zeros((4, 3))))
    acquisition._last_sequence = acquisition._last_processing_sequence = 1
    identity = {"generation": acquisition.generation, "engine_revision": acquisition.engine_revision,
                "expected_revision": acquisition.acquisition_revision}

    for command_id in ("full-boundary", "retry-boundary"):
        payload = identity if command is AcquisitionCommand.detach_processing else None
        await acquisition.execute((command, CommandRequest(command_id, payload)))
        result = next(event.data for event in events if event.type is EventType.CommandResult
                      and event.data.command_id == command_id)
        assert not result.ok
        assert result.data is None

    assert not data.has_consumer
    assert [data.get_data(timeout=1).sequence_id for _ in range(2)] == [0, 1]
    assert data.get_data(timeout=0.01) is None
