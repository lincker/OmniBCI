"""The real engine signal queue bounds backlog and preserves input boundaries."""

from queue import Full
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import numpy as np
import pytest

from omnibci.application.events import SignalPreparationReceipt, CommandRequest, ProcessingInputStart, StreamBarrier
from omnibci.domain.signal import SignalChunk
from omnibci.infrastructure.acquisition.acquisition import AcquisitionCommand
from omnibci.infrastructure.neuroengine.builder import NeuroEngineBuilder, NeuroEngineConfig
from omnibci.shared.events import EventType
from tests.neuroengine.test_channel_mapping import _plan, _processor, _receipt
from tests.neuroengine.test_independent_input_boundaries import input_pair


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
        await acquisition.execute((command, CommandRequest(command_id, identity)))
        result = next(event.data for event in events if event.type is EventType.CommandResult
                      and event.data.command_id == command_id)
        assert not result.ok
        assert result.data is None

    assert not data.has_consumer
    assert [data.get_data(timeout=1).sequence_id for _ in range(2)] == [0, 1]
    assert data.get_data(timeout=0.01) is None
