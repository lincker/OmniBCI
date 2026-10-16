"""Unit tests for EpochProcessor buffering, epoch construction, and decoding."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import numpy as np
import pytest

from omnibci.domain.ports.algorithm import Decoder, Filter, Prediction
from omnibci.domain.signal import SignalChunk
from omnibci.domain.classification import Classification
from omnibci.infrastructure.neuroengine.signal_processing.epoch.epoch_processor import EpochProcessor


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


def _manager(decoder=None, sample_rate=1000) -> EpochProcessor:
    return EpochProcessor(
        decoder=decoder or FakeDecoder(),
        sample_rate=sample_rate,
    )


# ---------------------------------------------------------------------------
# Buffer construction
# ---------------------------------------------------------------------------

class TestEpochBuffering:
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
