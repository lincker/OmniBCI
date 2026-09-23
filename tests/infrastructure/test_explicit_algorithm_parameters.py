"""Algorithm parameters and host metadata remain independent of global settings."""

from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np
import pytest

from omnibci.infrastructure.plugins.algorithms.decoders.classic40_cca import classic_40_cca
from omnibci.infrastructure.plugins.algorithms.decoders.classic40_fbcca import classic_40_fbcca
from omnibci.infrastructure.plugins.algorithms.epoch.detector import DefaultEpochDetector
from omnibci.infrastructure.plugins.algorithms.filters.baseline_correction import BaselineCorrection
from omnibci.sdk import FilterContext, PluginContext, SignalChunk


DECODERS = (classic_40_cca.Classic40CCA, classic_40_fbcca.Classic40FBCCA)


@pytest.mark.parametrize("decoder_class", DECODERS)
def test_window_requires_at_least_two_host_samples(decoder_class):
    with pytest.raises(ValueError, match="at least two samples"):
        decoder_class(parameters={"window_duration": 0.001}, context=PluginContext(sample_rate=1))


@pytest.fixture(autouse=True)
def forbid_settings_reads(monkeypatch):
    getter = Mock(side_effect=AssertionError("Explicit plugins must not read settings"))
    for module in (classic_40_cca, classic_40_fbcca):
        monkeypatch.setattr(module, "settings", SimpleNamespace(get=getter))
    return getter


@pytest.mark.parametrize("decoder_class", DECODERS)
@pytest.mark.parametrize("context", [None, PluginContext()])
def test_explicit_decoder_requires_host_sample_rate(decoder_class, context):
    with pytest.raises(ValueError, match=r"requires context\.sample_rate"):
        decoder_class(parameters={}, context=context)


@pytest.mark.parametrize("decoder_class", DECODERS)
def test_decoder_defaults_use_host_sample_rate(decoder_class):
    decoder = decoder_class(parameters={}, context=PluginContext(sample_rate=250))
    context_only_decoder = decoder_class(context=PluginContext(sample_rate=500))

    assert (decoder.SAMPLE_RATE, decoder.SAMPLE_COUNT, decoder.OFFSET_LENGTH) == (250, 750, 35)
    assert context_only_decoder.SAMPLE_COUNT == 1500
    assert context_only_decoder.OFFSET_LENGTH == 70


@pytest.mark.parametrize("decoder_class", DECODERS)
def test_decoder_instances_keep_independent_windows(decoder_class):
    parameters = {"window_duration": 0.5, "offset_time": 0.1}
    first = decoder_class(parameters=parameters, context=PluginContext(sample_rate=200))
    parameters["window_duration"] = 1.0
    second = decoder_class(parameters=parameters, context=PluginContext(sample_rate=200))
    window = np.zeros((2, 121))

    assert first.should_decode(window)
    assert not second.should_decode(window)
    assert (first.SAMPLE_COUNT, second.SAMPLE_COUNT) == (100, 200)
    assert first.OFFSET_LENGTH == second.OFFSET_LENGTH == 20


@pytest.mark.parametrize("decoder_class", DECODERS)
def test_decoder_uses_explicit_window_slice(monkeypatch, decoder_class):
    decoder = decoder_class(
        parameters={"window_duration": 0.5, "offset_time": 0.1},
        context=PluginContext(sample_rate=200),
    )
    window = np.arange(300).reshape(2, 150)
    if isinstance(decoder, classic_40_cca.Classic40CCA):
        classify = Mock(return_value=7)
        monkeypatch.setattr(decoder, "predict", classify)
    else:
        classify = Mock(return_value=(7, 14.0, np.zeros(40)))
        monkeypatch.setattr(decoder, "classify", classify)

    prediction = decoder.decode(window, window[:, -10:])

    assert prediction.target_id == 7
    np.testing.assert_array_equal(classify.call_args.args[0], window[:, 20:120])


@pytest.mark.parametrize(
    ("module", "decoder_class", "window_key", "offset_key"),
    [
        (classic_40_cca, classic_40_cca.Classic40CCA, "ssvep.duration", "ssvep.offset_time"),
        (classic_40_fbcca, classic_40_fbcca.Classic40FBCCA,
         "ALGORITHM.window_duration", "ALGORITHM.offset_time"),
    ],
)
def test_legacy_values_and_explicit_values_produce_same_prediction(
    monkeypatch, module, decoder_class, window_key, offset_key,
):
    values = {
        "ACQUISITION.sample_rate": 250,
        "ssvep.duration": 2.0,
        "ssvep.offset_time": 0.2,
        "ALGORITHM.window_duration": 1.5,
        "ALGORITHM.offset_time": 0.3,
        window_key: 0.5,
        offset_key: 0.1,
    }
    getter = Mock(side_effect=lambda key, default=None: values.get(key, default))
    monkeypatch.setattr(module, "settings", SimpleNamespace(get=getter))
    legacy = decoder_class()
    getter.side_effect = AssertionError("Explicit construction read legacy settings")
    explicit = decoder_class(
        parameters={"window_duration": 0.5, "offset_time": 0.1},
        context=PluginContext(sample_rate=250),
    )
    window = np.random.default_rng(8).normal(size=(3, 160))

    assert legacy.SAMPLE_COUNT == explicit.SAMPLE_COUNT == 125
    assert legacy.OFFSET_LENGTH == explicit.OFFSET_LENGTH == 25
    assert legacy.decode(window, window) == explicit.decode(window, window)


def test_detector_accepts_explicit_empty_configuration():
    detector = DefaultEpochDetector(parameters={}, context=PluginContext(sample_rate=250))
    chunk = SignalChunk(
        timestamp=0, sequence_id=0,
        raw_data=np.array([[1.0, 2.0, 3.0], [0.0, 9.0, 0.0]]),
    )

    assert detector.detect_start(chunk) == (1, 9)
    assert detector.detect_end(chunk) == (-1, 0)


def test_parallel_filter_instances_do_not_share_inputs_or_runtime_state():
    first = BaselineCorrection(parameters={}, context=PluginContext(sample_rate=250))
    second = BaselineCorrection(parameters={}, context=PluginContext(sample_rate=500))
    data = np.array([[10.0, 20.0], [30.0, 40.0]])
    before = data.copy()
    contexts = (
        FilterContext(sample_rate=250, baseline=np.array([[1.0, 3.0], [4.0, 6.0]])),
        FilterContext(sample_rate=500, baseline=np.array([[5.0, 7.0], [8.0, 10.0]])),
    )
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda pair: pair[0].apply(data, pair[1]),
                                zip((first, second), contexts)))

    assert first is not second
    np.testing.assert_array_equal(results[0], [[8.0, 18.0], [25.0, 35.0]])
    np.testing.assert_array_equal(results[1], [[4.0, 14.0], [21.0, 31.0]])
    np.testing.assert_array_equal(data, before)
