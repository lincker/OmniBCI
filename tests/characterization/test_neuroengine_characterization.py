"""
Characterization tests — NeuroEngine 限界上下文.

锁定信号处理流水线的当前可观察行为，接缝选在 EpochProcessor —— 它是 epoch
缓冲 + 解码 + 领域对象构造的汇聚点，且可用 fake 解码器完全替代硬件与具体算法插件。

被固化的行为：
  - initialize_epoch(chunk, start_idx, event_id) 从给定 start_idx 切片建立缓冲。
    （边界检测已移到 SignalProcessor 的 EpochDetector；EpochProcessor 只收 start_idx。）
  - append_chunk() / append_until_index() 的拼接语义。
  - build_epoch() 产出不可变 Epoch 值对象（含防御性拷贝、只读）。
  - run_pipeline() 把解码器 legacy 的 (bool, int) 包装为 Classification，
    并在 force_recognize 时设置 forced=True。

fake 解码器不定义 plugin_type，因此不会进入插件注册表
（见 Registerable.__init_subclass__：plugin_type 为 None 时跳过注册）。
"""
from __future__ import annotations

from typing import Tuple

import numpy as np
import pytest

from omnibci.shared.signal import SignalChunk
from omnibci.domain.classification import Classification
from omnibci.infrastructure.plugins.algorithms.legacy_decoder import LegacyAnalyseDecoder
from omnibci.infrastructure.neuroengine.processing.epoch.epoch_processor import EpochProcessor


# ---------------------------------------------------------------------------
# Fake decoder (no plugin_type → not registered)
# ---------------------------------------------------------------------------

class FakeDecoder(LegacyAnalyseDecoder):
    """Returns a scripted (is_complete, target_id) and records its calls."""

    def __init__(self, complete: bool = True, target: int = 3):
        super().__init__()
        self._complete = complete
        self._target = target
        self.calls = []

    def analyse(self, all_data, current_packet_data, force_predict) -> Tuple[bool, int]:
        self.calls.append(force_predict)
        # Forced predictions always complete, mirroring real decoder contract.
        complete = True if force_predict else self._complete
        return complete, self._target


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
# Decoding pipeline — the (bool, int) → Classification wrapper
# ---------------------------------------------------------------------------

class TestRunPipeline:
    def test_empty_buffer_returns_incomplete(self):
        assert _manager().run_pipeline() == (False, None)

    def test_decoder_not_complete_returns_none(self):
        mgr = _manager(decoder=FakeDecoder(complete=False))
        mgr.initialize_epoch(_chunk(), start_idx=0, event_id=1)
        is_complete, clf = mgr.run_pipeline(force_recognize=False)
        assert is_complete is False and clf is None

    def test_complete_decode_wraps_into_classification(self):
        mgr = _manager(decoder=FakeDecoder(complete=True, target=7))
        mgr.initialize_epoch(_chunk(), start_idx=0, event_id=4)
        is_complete, clf = mgr.run_pipeline(force_recognize=False)
        assert is_complete is True
        assert isinstance(clf, Classification)
        assert clf.target_id == 7
        assert clf.event_id == 4
        assert clf.forced is False

    def test_marker_forced_path_stamps_forced_on_classification(self):
        # forced is an engine-side annotation now (decode no longer takes it):
        # run_pipeline(force_recognize=True) stamps Classification.forced=True.
        mgr = _manager(decoder=FakeDecoder(complete=True, target=2))
        mgr.initialize_epoch(_chunk(), start_idx=0, event_id=4)
        is_complete, clf = mgr.run_pipeline(force_recognize=True)
        assert is_complete is True
        assert clf.forced is True
        assert clf.target_id == 2


# ---------------------------------------------------------------------------
# Decoder.for_new_epoch — per-epoch decoder hook (Scheme A)
# ---------------------------------------------------------------------------

class TestForNewEpoch:
    def test_default_returns_self(self):
        # Stateless decoders share one instance across epochs (zero cost).
        dec = FakeDecoder()
        assert dec.for_new_epoch() is dec

    def test_stateful_override_returns_fresh_instance_sharing_model(self):
        class _Model:
            pass

        shared_model = _Model()

        class StatefulDecoder(LegacyAnalyseDecoder):
            def __init__(self, model):
                super().__init__()
                self.model = model           # heavy, shared, immutable
                self.acc = []                # per-epoch working state
            def analyse(self, all_data, current_packet_data, force_predict):
                self.acc.append(1)
                return True, 0
            def for_new_epoch(self):
                return StatefulDecoder(self.model)   # fresh state, same model

        d0 = StatefulDecoder(shared_model)
        d0.analyse(None, None, False)            # mutate d0's per-epoch state
        d1 = d0.for_new_epoch()
        assert d1 is not d0                       # fresh instance
        assert d1.model is shared_model           # shares the heavy model
        assert d1.acc == []                        # isolated working state
