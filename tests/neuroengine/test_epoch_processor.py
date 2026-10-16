"""Unit tests for EpochProcessor buffering, epoch construction, and decoding."""
from __future__ import annotations

import numpy as np
import pytest

from omnibci.domain.ports.algorithm import Decoder, Prediction
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

    def should_decode(self, window) -> bool:
        return self._ready

    def decode(self, window, latest):
        self.calls.append((window, latest))
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
    def test_empty_buffer_returns_incomplete(self):
        assert _manager().force_recognize() == (False, None)

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

    def test_marker_forced_path_stamps_forced_on_classification(self):
        # A marker bypasses the decoder's normal readiness gate. The decoder is
        # still invoked with the partial window and the engine stamps forced=True.
        decoder = FakeDecoder(complete=True, target=2, ready=False)
        mgr = _manager(decoder=decoder)
        mgr.initialize_epoch(_chunk(), start_idx=0, event_id=4)
        assert mgr.should_decode() is False

        is_complete, clf = mgr.force_recognize()
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
