"""Level triggers become single-sample events without changing source data."""

import logging
from pathlib import Path

import numpy as np
import pytest

from omnibci.domain.signal import SignalChunk
from omnibci.infrastructure.acquisition.preprocessing import TriggerEdgePreprocessor
from omnibci.infrastructure.plugins.acquisition.file.reader import FileReader
from omnibci.infrastructure.plugins.acquisition.neuroscan.curry8.curry8 import Curry8DataReader
from omnibci.infrastructure.plugins.acquisition.neuroscan.scan45.scan45 import Scan45DataReader


def chunk(triggers, *, sequence=0, dtype=np.int32):
    samples = np.arange(len(triggers))
    raw = np.asarray([samples, samples + 100, triggers], dtype=dtype)
    return SignalChunk(timestamp=123_500_000_000, sequence_id=sequence, raw_data=raw)


@pytest.mark.parametrize("triggers,expected", [
    ([0, 7, 7, 7, 0], [0, 7, 0, 0, 0]),
    ([7, 7, 8, 8], [7, 0, 8, 0]),
    ([7, 0, 7], [7, 0, 7]),
    ([7, 255, 9, 255], [7, 255, 9, 255]),
    ([0, 0, 0], [0, 0, 0]),
    ([7], [7]),
])
@pytest.mark.parametrize("dtype", [np.int16, np.int32, np.float32, np.float64])
def test_only_held_values_are_suppressed(triggers, expected, dtype):
    source = chunk(triggers, sequence=12, dtype=dtype)
    original = source.raw_data.copy()

    result = TriggerEdgePreprocessor().process(source)

    np.testing.assert_array_equal(result.triggers, expected)
    np.testing.assert_array_equal(result.data, original[:-1])
    np.testing.assert_array_equal(source.raw_data, original)
    assert result.timestamp == source.timestamp
    assert result.sequence_id == source.sequence_id
    assert result.raw_data.shape == original.shape
    assert result.raw_data.dtype == original.dtype


@pytest.mark.parametrize("chunk_size", [1, 2, 3, 5, 20])
def test_event_positions_are_independent_of_packet_boundaries(chunk_size):
    triggers = [0, 7, 7, 7, 8, 8, 0, 8, 8, 0, 7, 7]
    expected = [0, 7, 0, 0, 8, 0, 0, 8, 0, 0, 7, 0]
    processor = TriggerEdgePreprocessor()
    outputs = []

    for start in range(0, len(triggers), chunk_size):
        source = chunk(triggers[start:start + chunk_size])
        outputs.extend(processor.process(source).triggers.tolist())

    assert outputs == expected


def test_empty_chunk_preserves_previous_input_value():
    processor = TriggerEdgePreprocessor()
    processor.process(chunk([7, 7]))
    empty = chunk([])

    assert processor.process(empty) is empty
    np.testing.assert_array_equal(processor.process(chunk([7, 8])).triggers, [0, 8])


def test_reset_accepts_the_first_event_of_a_new_stream():
    processor = TriggerEdgePreprocessor()
    processor.process(chunk([7, 7]))
    processor.reset()

    np.testing.assert_array_equal(processor.process(chunk([7, 7])).triggers, [7, 0])


def test_instances_do_not_share_previous_events():
    first, second = TriggerEdgePreprocessor(), TriggerEdgePreprocessor()
    first.process(chunk([7]))

    np.testing.assert_array_equal(second.process(chunk([7])).triggers, [7])
    np.testing.assert_array_equal(first.process(chunk([7])).triggers, [0])


def test_processing_a_read_only_view_does_not_change_its_backing_data():
    backing = chunk([0, 7, 7, 8, 8, 0]).raw_data
    original = backing.copy()
    view = backing[:, 1:5]
    view.flags.writeable = False
    source = SignalChunk(timestamp=5, sequence_id=3, raw_data=view)

    result = TriggerEdgePreprocessor().process(source)

    np.testing.assert_array_equal(result.triggers, [7, 0, 8, 0])
    np.testing.assert_array_equal(backing, original)
    assert not np.shares_memory(result.raw_data, backing)


def test_unchanged_input_is_reused():
    source = chunk([0, 0, 7, 0, 7, 8, 0])

    assert TriggerEdgePreprocessor().process(source) is source


@pytest.mark.parametrize("source_type,parameters,expected", [
    (Scan45DataReader, {}, True),
    (Curry8DataReader, {}, True),
    (FileReader, {"data_file_path": "recording.cnt"}, False),
])
def test_sources_declare_level_triggers_separately_from_discrete_file_events(
    source_type, parameters, expected,
):
    assert source_type(parameters=parameters, resource_root=Path(__file__).parent).trigger_mode == ("level" if expected else "events")


def test_trigger_logs_explain_suppression_across_packet_boundaries(caplog):
    processor = TriggerEdgePreprocessor()
    with caplog.at_level(logging.INFO, logger="omnibci.infrastructure.acquisition.preprocessing"):
        processor.process(chunk([7, 7, 8, 8], sequence=10))
        processor.process(chunk([8, 8, 0, 8], sequence=11))
    first, second = [record.getMessage() for record in caplog.records
                     if record.name == "omnibci.infrastructure.acquisition.preprocessing"]
    assert "sequence_id=10" in first
    assert "samples=4 input_nonzero=4 kept=2 suppressed=2" in first
    assert "previous_trigger=0 last_trigger=8" in first
    assert "kept_events=[(0, 7), (2, 8)]" in first
    assert "sequence_id=11" in second
    assert "samples=4 input_nonzero=3 kept=1 suppressed=2" in second
    assert "previous_trigger=8 last_trigger=8" in second
    assert "kept_events=[(3, 8)]" in second


def test_trigger_log_retained_events_are_bounded_and_exclude_signal_arrays(caplog):
    with caplog.at_level(logging.INFO, logger="omnibci.infrastructure.acquisition.preprocessing"):
        TriggerEdgePreprocessor().process(chunk(list(range(1, 101))))
    record, = [record for record in caplog.records
               if record.name == "omnibci.infrastructure.acquisition.preprocessing"]
    message = record.getMessage()
    assert "kept=100 suppressed=0" in message
    assert "kept_events=[(0, 1), (1, 2), (2, 3), (3, 4), (4, 5), (5, 6), (6, 7), (7, 8)]" in message
    assert "events_truncated=True" in message
    assert "(8, 9)" not in message
    assert len(message) < 500


def test_empty_and_zero_only_chunks_do_not_fill_the_log(caplog):
    processor = TriggerEdgePreprocessor()
    with caplog.at_level(logging.INFO, logger="omnibci.infrastructure.acquisition.preprocessing"):
        processor.process(chunk([]))
        processor.process(chunk([0] * 100))
    assert not [record for record in caplog.records
                if record.name == "omnibci.infrastructure.acquisition.preprocessing"]


class _LiveSource:
    trigger_mode = "level"
    resume_mode = "restart-only"

    def __init__(self):
        import asyncio

        self.is_connected = False
        self.is_streaming = False
        self.chunks = asyncio.Queue()

    async def connect(self):
        self.is_connected = True

    async def disconnect(self):
        self.is_connected = False

    async def start_stream(self):
        self.is_streaming = True

    async def stop_stream(self):
        self.is_streaming = False

    async def read_chunk(self):
        return await self.chunks.get()


def _preproc_runtime(source):
    from queue import Queue
    from omnibci.infrastructure.acquisition.acquisition import AcquisitionRuntime
    from omnibci.infrastructure.ipc.data_manager import DataManager

    data, frames = DataManager(Queue()), Queue()
    acquisition = AcquisitionRuntime(
        source, Queue(), data, source_id="source",
        generation="generation", acquisition_revision="revision", engine_revision="engine", preview_queue=frames,
    )
    return acquisition, data, frames


def _samples(triggers):
    return np.vstack((np.arange(len(triggers)), triggers)).astype(np.float32)


async def _receive(data):
    import asyncio

    async with asyncio.timeout(2):
        while data.empty():
            await asyncio.sleep(0)
    return data.get_data()


@pytest.mark.asyncio
async def test_level_triggers_are_normalized_across_chunks_before_delivery_and_preview():
    import pickle
    from omnibci.application.events import CommandRequest, StreamBarrier
    from omnibci.infrastructure.acquisition.acquisition import AcquisitionCommand

    source = _LiveSource()
    acquisition, data, frames = _preproc_runtime(source)
    original = _samples([0, 7, 7, 7, 8, 8, 0, 8])
    snapshot = original.copy()
    try:
        await acquisition._on_connect()
        await acquisition._on_start_stream()
        for start, end in ((0, 3), (3, 6), (6, 8)):
            await source.chunks.put(original[:, start:end])
        delivered = [await _receive(data) for _ in range(3)]
        normalized = np.concatenate([c.raw_data for c in delivered], axis=1)
        np.testing.assert_array_equal(normalized[-1], [0, 7, 0, 0, 8, 0, 0, 8])
        np.testing.assert_array_equal(normalized[:-1], snapshot[:-1])
        np.testing.assert_array_equal(original, snapshot)
        assert [c.sequence_id for c in delivered] == [0, 1, 2]
        for c in delivered:
            frame = pickle.loads(frames.get_nowait())
            np.testing.assert_array_equal(frame["values"], c.raw_data)
            assert frame["sequence_id"] == c.sequence_id
            assert frame["timestamp_ns"] == str(c.timestamp)
            assert c.raw_data.dtype == original.dtype

        await acquisition.execute((AcquisitionCommand.stop_and_drain, CommandRequest("drain")))
        barrier = data.get_data()
        assert isinstance(barrier, StreamBarrier)
        assert barrier.command_id == "drain" and barrier.last_sequence == 2
        assert data.empty()
    finally:
        await acquisition.shutdown()


@pytest.mark.asyncio
async def test_repeated_start_and_connect_while_running_do_not_reset_trigger_state():
    source = _LiveSource()
    acquisition, data, _ = _preproc_runtime(source)
    try:
        await acquisition._on_connect()
        await acquisition._on_start_stream()
        await source.chunks.put(_samples([7, 7]))
        np.testing.assert_array_equal((await _receive(data)).triggers, [7, 0])
        task, connection = acquisition.fetch_task, acquisition.connection_id

        await acquisition._on_connect()
        await acquisition._on_start_stream()
        await source.chunks.put(_samples([7, 8, 8]))

        np.testing.assert_array_equal((await _receive(data)).triggers, [0, 8, 0])
        assert acquisition.fetch_task is task and acquisition.connection_id == connection
    finally:
        await acquisition.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("restart", ["stop_start", "reconnect", "eof"])
async def test_a_new_stream_keeps_its_first_nonzero_trigger(restart):
    source = _LiveSource()
    acquisition, data, _ = _preproc_runtime(source)
    try:
        await acquisition._on_connect()
        await acquisition._on_start_stream()
        await source.chunks.put(_samples([7, 7]))
        np.testing.assert_array_equal((await _receive(data)).triggers, [7, 0])
        old_connection = acquisition.connection_id

        if restart == "reconnect":
            await acquisition._on_disconnect()
            await acquisition._on_connect()
            assert acquisition.connection_id != old_connection
        elif restart == "eof":
            await source.chunks.put(None)
            await acquisition.fetch_task
        else:
            await acquisition._on_stop_stream()

        await acquisition._on_start_stream()
        await source.chunks.put(_samples([7, 7]))
        c = await _receive(data)
        np.testing.assert_array_equal(c.triggers, [7, 0])
        assert c.sequence_id == 1
        if restart != "reconnect":
            assert acquisition.connection_id == old_connection
    finally:
        await acquisition.shutdown()


@pytest.mark.asyncio
async def test_discrete_file_events_and_cached_views_survive_replay_unchanged():
    import pickle
    from types import SimpleNamespace

    original = _samples([7, 7, 8, 8, 0, 7])
    snapshot = original.copy()
    source = FileReader(parameters={"data_file_path": "events.cnt", "file_reader_chunk_size": 2}, resource_root=Path(__file__).parent)

    async def read_chunk(*, start, end):
        return original[:, start:end] if start < original.shape[1] else None

    source.reader_adapter = SimpleNamespace(read_chunk=read_chunk)
    acquisition, data, frames = _preproc_runtime(source)
    await acquisition._on_connect()
    try:
        for _ in range(2):
            await acquisition._on_start_stream()
            await acquisition.fetch_task
            chunks = [data.get_data() for _ in range(3)]
            np.testing.assert_array_equal(np.concatenate([item.raw_data for item in chunks], axis=1), snapshot)
            for c in chunks:
                np.testing.assert_array_equal(pickle.loads(frames.get_nowait())["values"], c.raw_data)
            assert data.empty()
        np.testing.assert_array_equal(original, snapshot)
    finally:
        await acquisition._on_stop_stream()


@pytest.mark.asyncio
@pytest.mark.parametrize("declaration", [None, False, "true"])
async def test_sources_without_a_valid_trigger_mode_are_rejected(declaration):
    source = _LiveSource()
    source.trigger_mode = declaration
    with pytest.raises(ValueError, match="trigger_mode"):
        _preproc_runtime(source)


@pytest.mark.asyncio
async def test_additional_steps_run_in_order_before_both_consumers_and_reset_on_restart():
    from dataclasses import replace
    import pickle

    class SignalTransform:
        def __init__(self, scale, offset):
            self.scale, self.offset = scale, offset
            self.resets = 0

        def reset(self):
            self.resets += 1

        def process(self, c):
            raw = c.raw_data.copy()
            raw[:-1] = raw[:-1] * self.scale + self.offset
            return replace(c, raw_data=raw)

    source = _LiveSource()
    acquisition, data, frames = _preproc_runtime(source)
    add, scale = SignalTransform(1, 3), SignalTransform(2, 0)
    acquisition._preprocessors = (add, TriggerEdgePreprocessor(), scale)
    try:
        await acquisition._on_connect()
        for _ in range(2):
            resets_before_start = add.resets, scale.resets
            await acquisition._on_start_stream()
            original = _samples([7, 7])
            await source.chunks.put(original)
            delivered = await _receive(data)
            np.testing.assert_array_equal(delivered.data, [[6, 8]])
            np.testing.assert_array_equal(delivered.triggers, [7, 0])
            np.testing.assert_array_equal(original, _samples([7, 7]))
            np.testing.assert_array_equal(pickle.loads(frames.get_nowait())["values"], delivered.raw_data)
            assert (add.resets, scale.resets) == tuple(count + 1 for count in resets_before_start)
            await acquisition._on_stop_stream()
    finally:
        await acquisition.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("level_triggers,steps", [(True, "TriggerEdgePreprocessor"), (False, "passthrough")])
async def test_preprocessing_lifecycle_logs_report_resets_and_passthrough(caplog, level_triggers, steps):
    source = _LiveSource()
    source.trigger_mode = "level" if level_triggers else "events"
    acquisition, _, _ = _preproc_runtime(source)
    caplog.set_level(logging.INFO, logger="omnibci.infrastructure.acquisition")
    try:
        await acquisition._on_connect()
        connection = acquisition.connection_id
        await acquisition._on_start_stream()
        await acquisition._on_start_stream()
        await acquisition._on_stop_stream()
        await acquisition._on_start_stream()
        await acquisition._on_disconnect()
        await acquisition._on_connect()
        messages = [record.getMessage() for record in caplog.records
                    if record.getMessage().startswith("Acquisition preprocessing:")]
        assert len(messages) == 4
        for message, reason in zip(messages, ("connect", "stream_start", "stream_start", "connect"), strict=True):
            assert f"reason={reason}" in message
            assert "source_id=source generation=generation" in message
            assert f"steps={steps}" in message
        assert all(f"connection_id={connection}" in message for message in messages[:3])
        assert f"connection_id={acquisition.connection_id}" in messages[-1]
        assert acquisition.connection_id != connection
    finally:
        await acquisition.shutdown()
