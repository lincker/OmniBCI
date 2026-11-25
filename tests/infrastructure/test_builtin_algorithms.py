"""Built-in Classic40 SSVEP decoders, epoch detector, and filter parameter contracts."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from queue import Queue
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np
import pytest

from omnibci.domain.signal import SignalChunk
from omnibci.infrastructure.ipc.data_manager import DataManager
from omnibci.infrastructure.plugins.algorithms.decoders.classic40_cca import classic_40_cca
from omnibci.infrastructure.plugins.algorithms.decoders.classic40_cca.classic_40_cca import Classic40CCA
from omnibci.infrastructure.plugins.algorithms.decoders.classic40_fbcca import classic_40_fbcca
from omnibci.infrastructure.plugins.algorithms.decoders.classic40_fbcca.classic_40_fbcca import Classic40FBCCA
from omnibci.infrastructure.plugins.algorithms.epoch.detector import DefaultEpochDetector
from omnibci.infrastructure.plugins.algorithms.filters.baseline_correction import BaselineCorrection
from omnibci.sdk import EpochBoundary
from omnibci.shared.events import EventType
from tests.processing_helpers import make_processor, prepared_signal

DECODERS = (classic_40_cca.Classic40CCA, classic_40_fbcca.Classic40FBCCA)


@pytest.mark.parametrize("decoder_class,usable_samples", [
    (Classic40CCA, 0),
    (Classic40CCA, 1),
    (Classic40FBCCA, 0),
    (Classic40FBCCA, 1),
    (Classic40FBCCA, 27),
])
def test_forced_window_declines_below_numerical_minimum(decoder_class, usable_samples):
    decoder = decoder_class(parameters={}, input_signal=prepared_signal(500), resource_root=Path(__file__).parent)
    decoder.setup()
    times = np.arange(decoder.OFFSET_LENGTH + usable_samples) / decoder.SAMPLE_RATE
    window = np.sin(2 * np.pi * 8 * times)[None, :]

    assert not decoder.should_decode(window)
    assert decoder.decode(window, latest_raw_segment=window) is None


@pytest.mark.parametrize("decoder_class,usable_samples", [
    (Classic40CCA, 2),
    (Classic40FBCCA, 28),
])
def test_forced_window_at_numerical_minimum_still_decodes(decoder_class, usable_samples):
    decoder = decoder_class(parameters={}, input_signal=prepared_signal(500), resource_root=Path(__file__).parent)
    decoder.setup()
    times = np.arange(decoder.OFFSET_LENGTH + usable_samples) / decoder.SAMPLE_RATE
    window = np.sin(2 * np.pi * 8 * times)[None, :]

    assert not decoder.should_decode(window)
    assert 1 <= decoder.decode(window, latest_raw_segment=window).target_id <= 40


@pytest.mark.asyncio
@pytest.mark.parametrize("decoder_class", [Classic40CCA, Classic40FBCCA])
@pytest.mark.parametrize("first_chunk_size", [1, 64])
@pytest.mark.parametrize("second_event", [7, 9])
async def test_early_marker_keeps_processor_running_for_next_valid_epoch(decoder_class, first_chunk_size, second_event):
    decoder = decoder_class(parameters={"window_duration": 1.0}, input_signal=prepared_signal(500), resource_root=Path(__file__).parent)
    events = []
    processor = make_processor(
        detector=DefaultEpochDetector(resource_root=Path(__file__).parent, parameters={}, input_signal=prepared_signal(250)),
        decoder=decoder,
        command_queue=Queue(),
        data_manager=DataManager(buffer=Queue()),
        event_bus=SimpleNamespace(put_event=events.append),
        sample_rate=500,
    )
    times = np.arange(640) / 500
    raw = np.vstack((np.sin(2 * np.pi * 12 * times), np.zeros(times.size)))
    raw[-1, 0] = 7
    raw[-1, 1] = second_event

    for sequence, (start, end) in enumerate(((0, first_chunk_size), (first_chunk_size, 128), (128, 640))):
        processor.data_manager.put_data(SignalChunk(0, sequence, raw[:, start:end]))
        await processor._current_state.run()

    trials = [event.data for event in events if event.type is EventType.TrialCompleted]
    assert len(trials) == 1
    assert trials[0].classification.event_id == second_event
    assert trials[0].classification.target_id == 5
    assert not trials[0].classification.forced
    assert processor.current_epoch is None


@pytest.mark.parametrize("filter_length", [3, 11])
def test_fbcca_short_window_limit_follows_filter_coefficients(monkeypatch, filter_length):
    decoder = Classic40FBCCA(parameters={}, input_signal=prepared_signal(500), resource_root=Path(__file__).parent)
    coefficients = np.zeros(filter_length)
    coefficients[0] = 1
    monkeypatch.setattr(decoder, "get_filter_bank", lambda: [(coefficients, np.ones(1))])
    decoder.setup()
    times = np.arange(decoder.OFFSET_LENGTH + 3 * filter_length + 1) / decoder.SAMPLE_RATE
    window = np.sin(2 * np.pi * 12 * times)[None, :]

    assert decoder.decode(window[:, :-1], latest_raw_segment=window[:, :-1]) is None
    assert 1 <= decoder.decode(window, latest_raw_segment=window).target_id <= 40


@pytest.mark.parametrize("decoder_class", DECODERS)
def test_window_requires_at_least_two_host_samples(decoder_class):
    with pytest.raises(ValueError, match="at least two samples"):
        decoder_class(parameters={"window_duration": 0.001}, input_signal=prepared_signal(1), resource_root=Path(__file__).parent)


@pytest.mark.parametrize("decoder_class", DECODERS)
@pytest.mark.parametrize("input_signal", [None, {}])
def test_explicit_decoder_requires_prepared_input(decoder_class, input_signal):
    with pytest.raises(ValueError, match=r"requires input_signal"):
        decoder_class(parameters={}, input_signal=input_signal, resource_root=Path(__file__).parent)


@pytest.mark.parametrize("decoder_class", DECODERS)
def test_decoder_defaults_use_host_sample_rate(decoder_class):
    decoder = decoder_class(parameters={}, input_signal=prepared_signal(250), resource_root=Path(__file__).parent)
    decoder.setup()
    higher_rate_decoder = decoder_class(parameters={}, input_signal=prepared_signal(500), resource_root=Path(__file__).parent)
    higher_rate_decoder.setup()

    assert (decoder.SAMPLE_RATE, decoder.SAMPLE_COUNT, decoder.OFFSET_LENGTH) == (250, 750, 35)
    assert higher_rate_decoder.SAMPLE_COUNT == 1500
    assert higher_rate_decoder.OFFSET_LENGTH == 70


@pytest.mark.parametrize("decoder_class", DECODERS)
def test_decoder_instances_keep_independent_windows(decoder_class):
    parameters = {"window_duration": 0.5, "offset_time": 0.1}
    first = decoder_class(parameters=parameters, input_signal=prepared_signal(200), resource_root=Path(__file__).parent)
    first.setup()
    parameters["window_duration"] = 1.0
    second = decoder_class(parameters=parameters, input_signal=prepared_signal(200), resource_root=Path(__file__).parent)
    second.setup()
    window = np.zeros((2, 121))

    assert first.should_decode(window)
    assert not second.should_decode(window)
    assert (first.SAMPLE_COUNT, second.SAMPLE_COUNT) == (100, 200)
    assert first.OFFSET_LENGTH == second.OFFSET_LENGTH == 20


@pytest.mark.parametrize("decoder_class", DECODERS)
def test_decoder_uses_explicit_window_slice(monkeypatch, decoder_class):
    decoder = decoder_class(parameters={"window_duration": 0.5, "offset_time": 0.1}, input_signal=prepared_signal(200), resource_root=Path(__file__).parent)
    decoder.setup()
    window = np.arange(300).reshape(2, 150)
    if isinstance(decoder, classic_40_cca.Classic40CCA):
        classify = Mock(return_value=7)
        monkeypatch.setattr(decoder, "predict", classify)
    else:
        classify = Mock(return_value=(7, 14.0, np.zeros(40)))
        monkeypatch.setattr(decoder, "classify", classify)

    prediction = decoder.decode(window, latest_raw_segment=window[:, -10:])

    assert prediction.target_id == 7
    np.testing.assert_array_equal(classify.call_args.args[0], window[:, 20:120])


@pytest.mark.parametrize("decoder_class", DECODERS)
def test_implicit_decoder_configuration_is_rejected(decoder_class):
    with pytest.raises(TypeError, match="parameters"):
        decoder_class()


def test_detector_accepts_explicit_empty_configuration():
    detector = DefaultEpochDetector(parameters={}, input_signal=prepared_signal(250), resource_root=Path(__file__).parent)
    chunk = SignalChunk(
        timestamp=0, sequence_id=0,
        raw_data=np.array([[1.0, 2.0, 3.0], [0.0, 9.0, 0.0]]),
    )

    assert detector.detect(chunk.raw_data) == (EpochBoundary(offset=1, kind="start", event_id=9),)


def test_parallel_filter_instances_do_not_share_inputs_or_runtime_state():
    first = BaselineCorrection(parameters={}, input_signal=prepared_signal(250), resource_root=Path(__file__).parent)
    second = BaselineCorrection(parameters={}, input_signal=prepared_signal(500), resource_root=Path(__file__).parent)
    data = np.array([[10.0, 20.0], [30.0, 40.0]])
    before = data.copy()
    contexts = (
        np.array([[1.0, 3.0], [4.0, 6.0]]),
        np.array([[5.0, 7.0], [8.0, 10.0]]),
    )
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda pair: pair[0].apply(data, pre_trigger_data=pair[1]),
                                zip((first, second), contexts)))

    assert first is not second
    np.testing.assert_array_equal(results[0], [[8.0, 18.0], [25.0, 35.0]])
    np.testing.assert_array_equal(results[1], [[4.0, 14.0], [21.0, 31.0]])
    np.testing.assert_array_equal(data, before)
