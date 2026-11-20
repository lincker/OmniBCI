"""Direct result construction agrees with the decoder execution contract."""
from datetime import datetime, timedelta, timezone, tzinfo

import numpy as np
import pytest

from omnibci.domain.classification import Classification
from omnibci.domain.ports.algorithm import Prediction


@pytest.fixture(params=[Prediction, Classification])
def result_factory(request):
    def construct(**fields):
        if request.param is Classification:
            fields.setdefault("event_id", 1)
        return request.param(**fields)
    return construct


@pytest.mark.parametrize("target", [True, np.bool_(True), 1.0, 1.5, np.float64(1), np.nan, np.inf, "1", np.array(1)])
def test_results_reject_non_integer_targets(result_factory, target):
    with pytest.raises(TypeError, match="target_id"):
        result_factory(target_id=target)


@pytest.mark.parametrize("target", [-1, np.int64(-1)])
def test_results_reject_negative_targets(result_factory, target):
    with pytest.raises(ValueError, match="target_id"):
        result_factory(target_id=target)


@pytest.mark.parametrize("target", [0, 7, np.int32(7), np.int64(7), np.uint64(2**63)])
def test_results_normalize_integer_targets_without_overflow(result_factory, target):
    result = result_factory(target_id=target)

    assert result.target_id == target
    assert type(result.target_id) is int
    assert result.confidence is None


@pytest.mark.parametrize("confidence", [True, np.bool_(False), "0.5", 0.5j, np.array(0.5)])
def test_results_reject_non_real_confidence(result_factory, confidence):
    with pytest.raises(TypeError, match="confidence"):
        result_factory(target_id=1, confidence=confidence)


@pytest.mark.parametrize("confidence", [-0.1, 1.1, np.nan, np.inf, -np.inf])
def test_results_reject_nonfinite_or_out_of_range_confidence(result_factory, confidence):
    with pytest.raises(ValueError, match="confidence"):
        result_factory(target_id=1, confidence=confidence)


@pytest.mark.parametrize("confidence", [0, 1, 0.5, np.float32(0.5), np.float64(0.5)])
def test_results_normalize_real_confidence(result_factory, confidence):
    result = result_factory(target_id=1, confidence=confidence)

    assert result.confidence == float(confidence)
    assert type(result.confidence) is float


@pytest.mark.parametrize("event_id", [True, np.bool_(True), 0, -1, 1.5, np.nan, "1"])
def test_classification_requires_a_positive_integer_event(event_id):
    with pytest.raises(ValueError, match="event_id"):
        Classification(target_id=1, event_id=event_id)


@pytest.mark.parametrize("forced", [0, 1, "false", None, np.bool_(False)])
def test_classification_requires_a_boolean_forced_flag(forced):
    with pytest.raises(TypeError, match="forced"):
        Classification(target_id=1, event_id=1, forced=forced)


@pytest.mark.parametrize("produced_at", [None, "2026-01-01", 0])
def test_classification_requires_a_datetime(produced_at):
    with pytest.raises(TypeError, match="produced_at"):
        Classification(target_id=1, event_id=1, produced_at=produced_at)


class MissingOffset(tzinfo):
    def utcoffset(self, dt):
        return None


@pytest.mark.parametrize("produced_at", [datetime(2026, 1, 1), datetime(2026, 1, 1, tzinfo=MissingOffset())])
def test_classification_rejects_naive_datetimes(produced_at):
    with pytest.raises(ValueError, match="produced_at"):
        Classification(target_id=1, event_id=1, produced_at=produced_at)


def test_classification_preserves_aware_time_and_defaults_to_utc():
    supplied = datetime(2026, 1, 1, tzinfo=timezone(timedelta(hours=8)))
    result = Classification(target_id=0, event_id=1, forced=True, produced_at=supplied)
    default = Classification(target_id=0, event_id=1)

    assert result.produced_at is supplied
    assert result.forced is True
    assert default.forced is False
    assert default.produced_at.tzinfo is timezone.utc
