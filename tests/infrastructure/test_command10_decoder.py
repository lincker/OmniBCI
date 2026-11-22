"""Command10 protocol alignment, numerical classification and decoder lifecycle."""
from pathlib import Path

import numpy as np
import pytest

from omnibci.infrastructure.plugins.algorithms.decoders.command10_fbcca.decoder import (
    Command10FBCCA, _basis, _reference_signals,
)
from omnibci.infrastructure.plugins.algorithms.decoders.command10_fbcca.targets import TARGETS
from omnibci.sdk import ChannelDescriptor, SignalInfo


ROOT = Path(__file__).resolve().parents[2]
PACKAGE = ROOT / 'omnibci/infrastructure/plugins/algorithms/decoders/command10_fbcca'
DEFAULTS = {'window_duration': 3.0, 'offset_time': .14, 'min_score': .3, 'min_margin': .05}
PROTOCOL = ((8.0, 0), (8.8, .5), (9.6, 1), (10.4, 1.5), (11.2, 0),
            (12.0, .5), (12.8, 1), (13.6, 1.5), (14.4, 0), (15.2, .5))


def signal_info(rate=250, channels=3, signal_type='eeg'):
    return SignalInfo(sample_rate=rate, channels=tuple(
        ChannelDescriptor(i, f'EEG{i}', channel_id=f'channel-{i}', signal_type=signal_type, unit='uV')
        for i in range(channels)
    ))


def synthetic(target_id, rate=250, *, channels=3, delay=.11, noise=.2):
    """Independent ground truth with harmonics, spatial phase variation and noise."""
    frequency, phase_pi = PROTOCOL[target_id - 1]
    t = np.arange(4 * rate) / rate - delay
    rng = np.random.default_rng(1000 + target_id)
    return np.array([
        sum(np.sin(h * (2 * np.pi * frequency * t + phase_pi * np.pi) + .2 * channel) / h
            for h in range(1, 6)) + noise * rng.normal(size=t.size)
        for channel in range(channels)
    ])


def make_decoder(rate=250, channels=3, **parameters):
    return Command10FBCCA(parameters={**DEFAULTS, **parameters}, resource_root=PACKAGE,
                          input_signal=signal_info(rate, channels))


@pytest.fixture
def decoder():
    instance = make_decoder()
    instance.setup()
    instance.begin_epoch()
    yield instance
    instance.teardown()


def test_protocol_is_identical_in_both_independent_packages():
    paradigm = ROOT / 'omnibci/infrastructure/plugins/paradigms/command10SSVEP/targets.py'
    assert (PACKAGE / 'targets.py').read_bytes() == paradigm.read_bytes()
    assert [target.target_id for target in TARGETS] == list(range(1, 11))
    for target, (frequency, phase_pi) in zip(TARGETS, PROTOCOL):
        assert target.frequency_hz == frequency
        assert target.phase_rad == pytest.approx(phase_pi * np.pi)


@pytest.mark.parametrize('target_id', range(1, 11))
@pytest.mark.parametrize('rate', [200, 250, 500, 1000])
def test_all_ten_noisy_delayed_targets_decode_to_matching_ids(target_id, rate):
    instance = make_decoder(rate)
    try:
        instance.setup()
        instance.begin_epoch()
        data = synthetic(target_id, rate)
        assert instance.should_decode(data)
        result = instance.decode(data, latest_raw_segment=data[:, -23:])
        assert result is not None
        assert result.target_id == target_id
        assert .3 <= result.confidence <= 1
    finally:
        instance.teardown()


@pytest.mark.parametrize('target', TARGETS)
def test_reference_harmonics_include_the_protocol_phase_and_sample_offset(target):
    actual = _reference_signals(target, 250, 40, 35)
    time = np.arange(35, 75) / 250
    for harmonic in range(1, 6):
        phase = harmonic * (2 * np.pi * target.frequency_hz * time + target.phase_rad)
        np.testing.assert_allclose(actual[2 * harmonic - 2], np.sin(phase))
        np.testing.assert_allclose(actual[2 * harmonic - 1], np.cos(phase))


def test_exact_readiness_and_forced_short_windows(decoder):
    data = synthetic(10)
    end = 35 + 750
    assert not decoder.should_decode(data[:, :end - 1])
    assert decoder.decode(data[:, :end - 1], latest_raw_segment=data[:, :0]) is None
    assert decoder.should_decode(data[:, :end])
    assert decoder.decode(data[:, :end], latest_raw_segment=data[:, :0]).target_id == 10


@pytest.mark.parametrize('target_id', range(1, 11))
def test_minimum_window_and_single_eeg_channel(target_id):
    instance = make_decoder(200, 1, window_duration=1, offset_time=0)
    try:
        instance.setup()
        instance.begin_epoch()
        data = synthetic(target_id, 200, channels=1)[:, :200]
        assert instance.decode(data, latest_raw_segment=data[:, :0]).target_id == target_id
    finally:
        instance.teardown()


def test_offset_and_fixed_window_ignore_excluded_samples(decoder):
    data = synthetic(6)
    data[:, :35] = 1e6
    data[:, 785:] = synthetic(1)[:, 785:]
    assert decoder.decode(data, latest_raw_segment=data[:, -10:]).target_id == 6


@pytest.mark.parametrize('kind', ['zero', 'constant', 'noise', 'nan', 'inf', 'shared_harmonic'])
def test_unusable_or_ambiguous_windows_abstain(decoder, kind):
    data = synthetic(2)
    if kind == 'zero':
        data[:] = 0
    elif kind == 'constant':
        data[:] = np.array([1, 200, -10])[:, None]
    elif kind == 'noise':
        data = np.random.default_rng(42).normal(size=data.shape)
    elif kind in ('nan', 'inf'):
        data[0, 200] = float(kind)
    else:
        # 24 Hz is both target 1's third and target 6's second harmonic.
        data[:] = np.sin(2 * np.pi * 24 * np.arange(data.shape[1]) / 250)
    assert decoder.decode(data, latest_raw_segment=data[:, :0]) is None
    assert not decoder.should_decode(data)


@pytest.mark.parametrize('parameter', ['min_score', 'min_margin'])
def test_explicit_rejection_thresholds_are_applied(parameter):
    instance = make_decoder(**{parameter: 1})
    try:
        instance.setup()
        instance.begin_epoch()
        data = synthetic(7)
        assert instance.decode(data, latest_raw_segment=data[:, :0]) is None
    finally:
        instance.teardown()


def test_duplicate_and_flat_channels_do_not_create_spurious_cca_dimensions(decoder):
    signal = synthetic(4, channels=1)[0]
    data = np.vstack((signal, signal, np.full(signal.size, 123.0)))
    assert _basis(data).shape[1] == 1
    assert _basis(np.zeros_like(data)).shape[1] == 0
    assert decoder.decode(data, latest_raw_segment=data[:, :0]).target_id == 4


def test_readonly_inputs_are_unchanged_and_epoch_reset_reuses_resources(decoder):
    data = synthetic(9)
    before = data.copy()
    data.setflags(write=False)
    references, filters = decoder._references, decoder._filters
    for _ in range(2):
        decoder.begin_epoch()
        assert decoder.decode(data, latest_raw_segment=data[:, -5:]).target_id == 9
        assert not decoder.should_decode(data)
        assert decoder.decode(data, latest_raw_segment=data[:, :0]) is None
    np.testing.assert_array_equal(data, before)
    assert decoder._references is references and decoder._filters is filters


@pytest.mark.parametrize('scale', [1e-6, 1e6])
def test_channel_units_and_order_do_not_change_prediction(decoder, scale):
    data = synthetic(8)[::-1] * scale
    assert decoder.decode(data, latest_raw_segment=data[:, :0]).target_id == 8


@pytest.mark.parametrize('parameters', [
    {'window_duration': 0}, {'window_duration': .5}, {'window_duration': 4.1},
    {'window_duration': True}, {'window_duration': float('nan')}, {'window_duration': '3'},
    {'offset_time': -1}, {'offset_time': float('inf')}, {'offset_time': True},
    {'window_duration': 4, 'offset_time': .14}, {'window_duration': 3.5, 'offset_time': .6},
    {'min_score': -1}, {'min_score': 1.1}, {'min_margin': -1}, {'min_margin': True},
    {'frequencies': [8]},
])
def test_invalid_parameters_fail_before_setup(parameters):
    with pytest.raises(ValueError):
        make_decoder(**parameters)


@pytest.mark.parametrize('metadata', [None, signal_info(199), signal_info(signal_type='eog'),
                                     signal_info(signal_type='unknown')])
def test_incompatible_input_metadata_is_rejected(metadata):
    with pytest.raises(ValueError):
        Command10FBCCA(parameters=DEFAULTS, resource_root=PACKAGE, input_signal=metadata)


@pytest.mark.parametrize('data', [np.zeros(1000), np.zeros((2, 1000)), np.zeros((4, 1000)),
                                np.zeros((3, 1000), dtype=complex), np.zeros((3, 1000), dtype=bool)])
def test_invalid_window_shape_or_dtype_is_rejected(decoder, data):
    with pytest.raises(ValueError):
        decoder.should_decode(data)
    with pytest.raises(ValueError):
        decoder.decode(data, latest_raw_segment=np.zeros((3, 0)))


def test_latest_segment_requires_the_sdk_matrix_contract(decoder):
    data = synthetic(1)
    with pytest.raises(ValueError):
        decoder.decode(data, latest_raw_segment=None)


def test_setup_teardown_and_reinitialization():
    instance = make_decoder()
    with pytest.raises(RuntimeError, match='setup'):
        instance.begin_epoch()
    instance.setup()
    with pytest.raises(RuntimeError, match='initialized'):
        instance.setup()
    instance.teardown()
    instance.teardown()
    assert instance._filters is None and instance._references is None
    with pytest.raises(RuntimeError, match='setup'):
        instance.should_decode(synthetic(1))
    instance.setup()
    instance.begin_epoch()
    data = synthetic(1)
    assert instance.decode(data, latest_raw_segment=data[:, :0]).target_id == 1
    instance.teardown()


def test_rejected_epoch_can_be_followed_by_a_valid_epoch(decoder):
    assert decoder.decode(np.zeros((3, 1000)), latest_raw_segment=np.zeros((3, 0))) is None
    decoder.begin_epoch()
    data = synthetic(3)
    assert decoder.decode(data, latest_raw_segment=data[:, :0]).target_id == 3


def test_partial_setup_failure_can_be_cleaned_up_and_retried(monkeypatch):
    from omnibci.infrastructure.plugins.algorithms.decoders.command10_fbcca import decoder as module
    instance = make_decoder()
    original = module._reference_signals
    def broken(*args):
        raise RuntimeError('reference preparation failed')
    monkeypatch.setattr(module, '_reference_signals', broken)
    with pytest.raises(RuntimeError, match='preparation'):
        instance.setup()
    instance.teardown()
    monkeypatch.setattr(module, '_reference_signals', original)
    instance.setup()
    instance.teardown()
