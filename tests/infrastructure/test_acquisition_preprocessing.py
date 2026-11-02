"""Level triggers become single-sample events without changing source data."""

import logging

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
    return SignalChunk(timestamp=123.5, sequence_id=sequence, raw_data=raw)


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
    assert source_type(parameters=parameters).trigger_is_level is expected


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
