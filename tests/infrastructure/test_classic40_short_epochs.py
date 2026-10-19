"""Forced short epochs must not interrupt subsequent SSVEP decoding."""

from queue import Queue
from types import SimpleNamespace

import numpy as np
import pytest

from omnibci.domain.signal import SignalChunk
from omnibci.infrastructure.ipc.data_manager import DataManager
from omnibci.infrastructure.plugins.algorithms.decoders.classic40_cca.classic_40_cca import Classic40CCA
from omnibci.infrastructure.plugins.algorithms.decoders.classic40_fbcca.classic_40_fbcca import Classic40FBCCA
from omnibci.infrastructure.plugins.algorithms.epoch.detector import DefaultEpochDetector
from omnibci.sdk import PluginContext
from tests.processing_helpers import make_processor


@pytest.mark.parametrize("decoder_class,usable_samples", [
    (Classic40CCA, 0),
    (Classic40CCA, 1),
    (Classic40FBCCA, 0),
    (Classic40FBCCA, 1),
    (Classic40FBCCA, 27),
])
def test_forced_window_declines_below_numerical_minimum(decoder_class, usable_samples):
    decoder = decoder_class(parameters={}, context=PluginContext(sample_rate=500))
    times = np.arange(decoder.OFFSET_LENGTH + usable_samples) / decoder.SAMPLE_RATE
    window = np.sin(2 * np.pi * 8 * times)[None, :]

    assert not decoder.should_decode(window)
    assert decoder.decode(window, window) is None


@pytest.mark.parametrize("decoder_class,usable_samples", [
    (Classic40CCA, 2),
    (Classic40FBCCA, 28),
])
def test_forced_window_at_numerical_minimum_still_decodes(decoder_class, usable_samples):
    decoder = decoder_class(parameters={}, context=PluginContext(sample_rate=500))
    times = np.arange(decoder.OFFSET_LENGTH + usable_samples) / decoder.SAMPLE_RATE
    window = np.sin(2 * np.pi * 8 * times)[None, :]

    assert not decoder.should_decode(window)
    assert 1 <= decoder.decode(window, window).target_id <= 40


@pytest.mark.asyncio
@pytest.mark.parametrize("decoder_class", [Classic40CCA, Classic40FBCCA])
@pytest.mark.parametrize("first_chunk_size", [1, 64])
@pytest.mark.parametrize("second_event", [7, 9])
async def test_early_marker_keeps_processor_running_for_next_valid_epoch(decoder_class, first_chunk_size, second_event):
    decoder = decoder_class(parameters={"window_duration": 1.0}, context=PluginContext(sample_rate=500))
    processor = make_processor(
        detector=DefaultEpochDetector(), decoder=decoder, command_queue=Queue(),
        data_manager=DataManager(buffer=Queue()), event_bus=SimpleNamespace(put_event=lambda event: None),
        sample_rate=500,
    )
    times = np.arange(640) / 500
    raw = np.vstack((np.sin(2 * np.pi * 12 * times), np.zeros(times.size)))
    raw[-1, 0] = 7
    raw[-1, 1] = second_event

    for sequence, (start, end) in enumerate(((0, first_chunk_size), (first_chunk_size, 128), (128, 640))):
        processor.data_manager.put_data(SignalChunk(0, sequence, raw[:, start:end]))
        await processor._current_state.run()

    trials = processor.session_runtime.session.trials
    assert len(trials) == 1
    assert trials[0].classification.event_id == second_event
    assert trials[0].classification.target_id == 5
    assert not trials[0].classification.forced
    assert processor.current_epoch is None


@pytest.mark.parametrize("filter_length", [3, 11])
def test_fbcca_short_window_limit_follows_filter_coefficients(monkeypatch, filter_length):
    decoder = Classic40FBCCA(parameters={}, context=PluginContext(sample_rate=500))
    coefficients = np.zeros(filter_length)
    coefficients[0] = 1
    monkeypatch.setattr(decoder, "get_filter_bank", lambda: [(coefficients, np.ones(1))])
    times = np.arange(decoder.OFFSET_LENGTH + 3 * filter_length + 1) / decoder.SAMPLE_RATE
    window = np.sin(2 * np.pi * 12 * times)[None, :]

    assert decoder.decode(window[:, :-1], window[:, :-1]) is None
    assert 1 <= decoder.decode(window, window).target_id <= 40
