"""Validate boundaries without exposing host transport or trial metadata."""
import pytest

from omnibci.domain.ports import algorithm


def boundary(**changes):
    cls = getattr(algorithm, "EpochBoundary", None)
    assert cls is not None, "The batch detector requires EpochBoundary"
    return cls(**{"offset": 0, "kind": "start", "event_id": 7, **changes})


def test_start_and_end_have_only_position_kind_and_optional_start_event():
    start = boundary()
    end = boundary(kind="end", event_id=None)
    assert start.offset == end.offset == 0
    assert start.event_id == 7 and end.event_id is None
    assert set(start.__dataclass_fields__) == {"offset", "kind", "event_id"}


@pytest.mark.parametrize("changes", [
    {"offset": True}, {"offset": -1}, {"offset": 1.5}, {"kind": "unknown"},
    {"event_id": None}, {"event_id": 0}, {"event_id": -1},
    {"event_id": True}, {"event_id": 1.5}, {"kind": "end", "event_id": 7},
])
def test_invalid_boundary_values_fail(changes):
    with pytest.raises(ValueError):
        boundary(**changes)
