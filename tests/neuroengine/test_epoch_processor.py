"""Unit tests for EpochProcessor buffering, epoch construction, and decoding."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import numpy as np
import pytest

from omnibci.domain.ports.algorithm import Decoder, Filter, Prediction
from omnibci.domain.signal import SignalChunk
from omnibci.domain.classification import Classification
from omnibci.infrastructure.signal_processing.epoch.epoch_processor import EpochProcessor


# ---------------------------------------------------------------------------
# Fake decoder (no plugin_type → not registered)
# ---------------------------------------------------------------------------

class FakeDecoder(Decoder):
    """Returns a scripted Prediction (or None) and records decode calls."""

    def __init__(
        self,
        complete: bool = True,
        target: int = 3,
        ready: bool = True,
    ):
        super().__init__()
        self._complete = complete
        self._target = target
        self._ready = ready
        self.calls = []
        self.readiness_windows = []

    def should_decode(self, window) -> bool:
        self.readiness_windows.append(window.copy())
        return self._ready

    def decode(self, window, latest_samples):
        self.calls.append((window, latest_samples))
        return Prediction(target_id=self._target) if self._complete else None


def _chunk(channels: int = 3, samples: int = 8) -> SignalChunk:
    # raw_data last row is the trigger channel; .data drops it → `channels` rows
    raw = np.ones((channels + 1, samples))
    return SignalChunk(timestamp=0.0, sequence_id=0, raw_data=raw)


def _data_chunk(data: np.ndarray) -> SignalChunk:
    return SignalChunk(0.0, 0, np.vstack((data, np.zeros((1, data.shape[1]), dtype=data.dtype))))


def _manager(decoder=None, sample_rate=1000) -> EpochProcessor:
    return EpochProcessor(
        decoder=decoder or FakeDecoder(),
        sample_rate=sample_rate,
    )


# ---------------------------------------------------------------------------
# Buffer construction
# ---------------------------------------------------------------------------

class TestEpochBuffering:
    @pytest.mark.parametrize("samples", [0, 1])
    def test_channel_mismatch_does_not_modify_buffer_or_latest_samples(self, samples):
        mgr = _manager()
        mgr.initialize_epoch(_chunk(channels=2), start_idx=0, event_id=1)
        before = mgr._epoch_data
        latest = mgr._current_chunk_data

        with pytest.raises(ValueError):
            mgr.append_chunk(_chunk(channels=1, samples=samples))

        assert mgr._epoch_data is before
        assert mgr._current_chunk_data is latest

    def test_appends_match_numpy_and_preserve_borrowed_windows(self):
        decoder = FakeDecoder(complete=False)
        decoder.should_decode = lambda window: decoder.readiness_windows.append(window) or True
        mgr = _manager(decoder=decoder)
        raw = np.arange(21, dtype=np.int16).reshape(3, 7)[:, ::-1]
        initial = SignalChunk(0.0, 0, raw)
        mgr.initialize_epoch(initial, start_idx=2, event_id=7)
        expected = initial.data[:, 2:]
        assert np.shares_memory(mgr._epoch_data, initial.raw_data)
        mgr.decode_if_ready()
        snapshots = [expected.copy()]
        fragments = [
            (np.full((2, 3), 20, dtype=np.int16), None),
            (np.full((2, 1), 30, dtype=np.int16), None),
            (np.full((2, 1), 0.5, dtype=np.float32), None),
            (np.full((2, 4), 40, dtype=np.float64), 0),
            (np.full((2, 4), 50, dtype=np.int16), 2),
            (np.empty((2, 0), dtype=np.float64), None),
        ]
        for data, end in fragments:
            chunk = _data_chunk(data)
            fragment = chunk.data if end is None else chunk.data[:, :end]
            expected = np.append(expected, fragment, axis=1)
            if end is None:
                mgr.process_chunk(chunk)
            else:
                mgr.append_until_index(chunk, end)
                mgr.decode_if_ready()
            snapshots.append(expected.copy())
            window, latest = decoder.calls[-1]
            np.testing.assert_array_equal(window, expected)
            assert window.dtype == expected.dtype
            np.testing.assert_array_equal(latest, fragment)
            assert latest.dtype == fragment.dtype
            if fragment.size:
                assert np.shares_memory(latest, chunk.raw_data)
            for (retained, _), ready_window, snapshot in zip(
                decoder.calls, decoder.readiness_windows, snapshots,
            ):
                assert retained is ready_window
                np.testing.assert_array_equal(retained, snapshot)
                assert retained.dtype == snapshot.dtype

    def test_empty_append_accepts_read_only_initial_storage(self):
        chunk = _chunk(channels=2, samples=3)
        chunk.raw_data.flags.writeable = False
        mgr = _manager()
        mgr.initialize_epoch(chunk, start_idx=0, event_id=1)

        mgr.append_until_index(chunk, 0)

        np.testing.assert_array_equal(mgr._epoch_data, chunk.data)
        assert mgr._current_chunk_data.shape == (2, 0)

    def test_empty_initial_slice_and_oversized_end_keep_numpy_slice_semantics(self):
        mgr = _manager()
        mgr.initialize_epoch(_chunk(channels=2, samples=3), start_idx=5, event_id=1)
        chunk = _data_chunk(np.arange(8, dtype=np.float32).reshape(2, 4))

        mgr.append_until_index(chunk, 8)

        np.testing.assert_array_equal(mgr._epoch_data, chunk.data)
        assert mgr._epoch_data.dtype == np.dtype(np.float64)

    def test_initialize_slices_from_start_index(self):
        mgr = _manager()
        mgr.initialize_epoch(_chunk(channels=3, samples=8), start_idx=2, event_id=5)
        # data has 3 channels, sliced from index 2 → 6 samples remain
        epoch = mgr.build_epoch()
        assert epoch.data.shape == (3, 6)
        assert epoch.event_id == 5

    def test_initialize_rejects_negative_start_index(self):
        mgr = _manager()
        with pytest.raises(ValueError):
            mgr.initialize_epoch(_chunk(), start_idx=-1, event_id=1)

    def test_append_chunk_grows_buffer(self):
        mgr = _manager()
        mgr.initialize_epoch(_chunk(channels=3, samples=4), start_idx=0, event_id=1)
        mgr.append_chunk(_chunk(channels=3, samples=6))
        assert mgr.build_epoch().data.shape == (3, 10)

    def test_append_until_index_truncates(self):
        mgr = _manager()
        mgr.initialize_epoch(_chunk(channels=3, samples=4), start_idx=0, event_id=1)
        mgr.append_until_index(_chunk(channels=3, samples=6), index=2)
        assert mgr.build_epoch().data.shape == (3, 6)

    def test_append_until_negative_index_rejected(self):
        mgr = _manager()
        mgr.initialize_epoch(_chunk(channels=3, samples=4), start_idx=0, event_id=1)
        with pytest.raises(ValueError):
            mgr.append_until_index(_chunk(), index=-1)


# ---------------------------------------------------------------------------
# Epoch value-object construction
# ---------------------------------------------------------------------------

class TestBuildEpoch:
    def test_completed_epoch_owns_pretrigger_and_window_across_growth_and_reinitialization(self):
        mgr = _manager()
        pre_trigger = np.full((2, 2), -1.0)
        first = _data_chunk(np.arange(8, dtype=np.float64).reshape(2, 4))
        mgr.initialize_epoch(first, start_idx=1, event_id=7, pre_trigger_data=pre_trigger)
        mgr.append_chunk(_data_chunk(np.full((2, 2), 8.0)))
        retained = mgr._epoch_data
        before = retained.copy()
        epoch = mgr.build_epoch()
        expected = np.concatenate((pre_trigger, before), axis=1)

        mgr.append_chunk(_data_chunk(np.full((2, 20), 9.0)))
        mgr.initialize_epoch(_chunk(channels=3, samples=3), start_idx=0, event_id=8)
        mgr.append_chunk(_chunk(channels=3, samples=2))
        pre_trigger[:] = 100

        np.testing.assert_array_equal(retained, before)
        np.testing.assert_array_equal(epoch.data, expected)
        assert epoch.trigger_offset == 2
        assert not epoch.data.flags.writeable
        assert not np.shares_memory(epoch.data, retained)
        assert not np.shares_memory(epoch.data, pre_trigger)
        assert mgr.build_epoch().window.shape == (3, 5)

    def test_build_requires_active_epoch(self):
        with pytest.raises(RuntimeError):
            _manager().build_epoch()

    def test_built_epoch_is_immutable(self):
        mgr = _manager()
        mgr.initialize_epoch(_chunk(channels=2, samples=5), start_idx=0, event_id=1)
        epoch = mgr.build_epoch()
        with pytest.raises(ValueError):
            epoch.data[0, 0] = 9.0  # read-only array

    def test_built_epoch_carries_sample_rate(self):
        mgr = _manager(sample_rate=250)
        mgr.initialize_epoch(_chunk(channels=2, samples=5), start_idx=0, event_id=1)
        assert mgr.build_epoch().sample_rate == 250


# ---------------------------------------------------------------------------
# Decoding — Prediction → Classification wrapper
# ---------------------------------------------------------------------------

class TestDecoding:
    @pytest.mark.parametrize(
        "target",
        [True, np.bool_(True), 1.0, 1.9, np.float64(1.0), "1", np.array(1), np.nan],
        ids=["bool", "numpy-bool", "float-integer", "float-fraction", "numpy-float", "string", "array-scalar", "nan"],
    )
    def test_rejects_non_integer_targets_before_coercion(self, target):
        decoder = FakeDecoder(target=target)
        mgr = EpochProcessor(decoder=decoder, config_revision="revision-1")
        mgr.initialize_epoch(_chunk(), start_idx=0, event_id=4)

        with pytest.raises(TypeError) as error:
            mgr.process_chunk(_chunk())

        message = str(error.value)
        assert f"{FakeDecoder.__module__}.{FakeDecoder.__qualname__}" in message
        assert "event_id=4" in message
        assert "revision-1" in message
        assert "target_id" in message
        assert isinstance(error.value.__cause__, TypeError)

    @pytest.mark.parametrize("target", [0, 7, np.int32(7), np.int64(7), np.uint64(2**63)])
    def test_accepts_python_and_numpy_integer_targets(self, target):
        mgr = _manager(decoder=FakeDecoder(target=target))
        mgr.initialize_epoch(_chunk(), start_idx=0, event_id=4)

        complete, classification = mgr.process_chunk(_chunk())

        assert complete
        assert classification.target_id == target
        assert type(classification.target_id) is int

    @pytest.mark.parametrize("result", [3, SimpleNamespace(target_id=1, confidence=None)])
    def test_rejects_results_other_than_prediction_or_none(self, result, monkeypatch):
        decoder = FakeDecoder()
        monkeypatch.setattr(decoder, "decode", MagicMock(return_value=result))
        mgr = EpochProcessor(decoder=decoder, config_revision="revision-1")
        mgr.initialize_epoch(_chunk(), start_idx=0, event_id=4)

        with pytest.raises(TypeError) as error:
            mgr.process_chunk(_chunk())

        assert "Prediction or None" in str(error.value)
        assert f"{FakeDecoder.__module__}.{FakeDecoder.__qualname__}" in str(error.value)
        assert "event_id=4" in str(error.value)
        assert "revision-1" in str(error.value)
        assert isinstance(error.value.__cause__, TypeError)

    @pytest.mark.parametrize("prediction,error_type", [
        (Prediction(-1), ValueError),
        (Prediction(1, confidence=-0.1), ValueError),
        (Prediction(1, confidence=1.1), ValueError),
        (Prediction(1, confidence=np.nan), ValueError),
        (Prediction(1, confidence="0.5"), TypeError),
    ])
    def test_classification_validation_preserves_error_type_and_adds_context(self, prediction, error_type, monkeypatch):
        decoder = FakeDecoder()
        monkeypatch.setattr(decoder, "decode", MagicMock(return_value=prediction))
        mgr = EpochProcessor(decoder=decoder, config_revision="revision-1")
        mgr.initialize_epoch(_chunk(), start_idx=0, event_id=4)

        with pytest.raises(error_type) as error:
            mgr.process_chunk(_chunk())

        assert type(error.value) is error_type
        assert type(error.value.__cause__) is error_type
        assert str(error.value.__cause__) in str(error.value)
        assert f"{FakeDecoder.__module__}.{FakeDecoder.__qualname__}" in str(error.value)
        assert "event_id=4" in str(error.value)
        assert "revision-1" in str(error.value)

    @pytest.mark.parametrize("cause", [TypeError("algorithm type failure"), ValueError("algorithm value failure")])
    def test_algorithm_errors_propagate_without_prediction_validation_wrapper(self, cause, monkeypatch):
        decoder = FakeDecoder()
        monkeypatch.setattr(decoder, "decode", MagicMock(side_effect=cause))
        mgr = _manager(decoder=decoder)
        mgr.initialize_epoch(_chunk(), start_idx=0, event_id=4)

        with pytest.raises(type(cause)) as error:
            mgr.process_chunk(_chunk())

        assert error.value is cause
        assert error.value.__cause__ is None

    def test_empty_buffer_returns_incomplete(self):
        assert _manager().force_decode_at_boundary() == (False, None)

    def test_decoder_not_complete_returns_none(self):
        mgr = _manager(decoder=FakeDecoder(complete=False))
        mgr.initialize_epoch(_chunk(), start_idx=0, event_id=1)
        is_complete, clf = mgr.process_chunk(_chunk())
        assert is_complete is False and clf is None

    def test_complete_decode_wraps_into_classification(self):
        mgr = _manager(decoder=FakeDecoder(complete=True, target=7))
        mgr.initialize_epoch(_chunk(), start_idx=0, event_id=4)
        is_complete, clf = mgr.process_chunk(_chunk())
        assert is_complete is True
        assert isinstance(clf, Classification)
        assert clf.target_id == 7
        assert clf.event_id == 4
        assert clf.forced is False

    def test_latest_samples_are_only_the_most_recent_append(self):
        decoder = FakeDecoder(ready=False)
        mgr = _manager(decoder=decoder)
        mgr.initialize_epoch(_chunk(samples=3), start_idx=0, event_id=4)
        assert mgr.process_chunk(_chunk(samples=2)) == (False, None)
        assert decoder.calls == []

        decoder._ready = True
        newest_chunk = _chunk(samples=4)
        mgr.process_chunk(newest_chunk)

        window, latest_samples = decoder.calls[0]
        assert window.shape == (3, 9)
        np.testing.assert_array_equal(latest_samples, newest_chunk.data)
        assert latest_samples.shape == (3, 4)

    def test_readiness_sees_accumulated_window_before_filters(self):
        class AddFilter(Filter):
            def apply(self, data, ctx):
                return data + 10

        decoder = FakeDecoder()
        mgr = EpochProcessor(decoder=decoder, filters=[AddFilter()])
        mgr.initialize_epoch(_chunk(samples=3), start_idx=0, event_id=4)
        mgr.process_chunk(_chunk(samples=2))

        assert len(decoder.readiness_windows) == 1
        np.testing.assert_array_equal(decoder.readiness_windows[0], np.ones((3, 5)))
        np.testing.assert_array_equal(decoder.calls[0][0], np.full((3, 5), 11))

    def test_marker_forced_path_stamps_forced_on_classification(self):
        # A marker bypasses the decoder's normal readiness gate. The decoder is
        # still invoked with the partial window and the engine stamps forced=True.
        decoder = FakeDecoder(complete=True, target=2, ready=False)
        mgr = _manager(decoder=decoder)
        mgr.initialize_epoch(_chunk(), start_idx=0, event_id=4)
        assert mgr.should_decode() is False

        is_complete, clf = mgr.force_decode_at_boundary()
        assert is_complete is True
        assert clf.forced is True
        assert clf.target_id == 2
        assert len(decoder.calls) == 1


# ---------------------------------------------------------------------------
# Decoder.for_new_epoch — per-epoch decoder hook (Scheme A)
# ---------------------------------------------------------------------------

class TestForNewEpoch:
    def test_default_returns_self(self):
        # Stateless decoders share one instance across epochs (zero cost).
        dec = FakeDecoder()
        assert dec.for_new_epoch() is dec


@pytest.mark.parametrize("algorithm", ["cca", "fbcca"])
def test_classic40_prediction_matches_numpy_append_reference(algorithm):
    from omnibci.infrastructure.plugins.algorithms.decoders.classic40_cca.classic_40_cca import Classic40CCA
    from omnibci.infrastructure.plugins.algorithms.decoders.classic40_fbcca.classic_40_fbcca import Classic40FBCCA
    from omnibci.sdk import PluginContext

    decoder_class = Classic40CCA if algorithm == "cca" else Classic40FBCCA
    context = PluginContext(sample_rate=250)
    decoder = decoder_class(parameters={}, context=context)
    reference_decoder = decoder_class(parameters={}, context=context)
    times = np.arange(800) / 250
    signal = np.vstack((
        np.sin(2 * np.pi * 12 * times),
        np.cos(2 * np.pi * 12 * times),
        np.sin(2 * np.pi * 12 * times) + 0.3 * np.sin(2 * np.pi * 24 * times),
    )) + np.random.default_rng(12).normal(0, 0.05, (3, 800))
    chunks = [_data_chunk(signal[:, start:start + 64]) for start in range(0, 800, 64)]
    mgr = _manager(decoder=decoder, sample_rate=250)
    mgr.initialize_epoch(chunks[0], start_idx=0, event_id=7)
    reference = chunks[0].data
    for chunk in chunks[1:]:
        mgr.append_chunk(chunk)
        reference = np.append(reference, chunk.data, axis=1)

    expected = reference_decoder.decode(reference, chunks[-1].data)
    complete, classification = mgr.force_decode_at_boundary()

    np.testing.assert_array_equal(mgr._epoch_data, reference)
    assert complete
    assert classification.target_id == expected.target_id
    assert classification.confidence == expected.confidence
