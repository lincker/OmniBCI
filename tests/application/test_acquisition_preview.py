"""Acquisition preview selections reject invalid or unbounded requests."""
import json

import pytest

from omnibci.application.acquisition_preview import PreviewSelection, limits


def test_defaults_round_trip_as_json():
    selection = PreviewSelection.from_value({})
    assert selection == PreviewSelection()
    assert selection.to_dict() == {
        "rows": [0], "sample_start": 0, "sample_count": 128,
        "include_trigger": True, "chunk_count": 32,
    }
    assert PreviewSelection.from_value(json.loads(json.dumps(selection.to_dict()))) == selection


def test_rows_preserve_order_and_do_not_share_mutable_input_or_output():
    value = {
        "rows": [9, 2, 1000], "sample_start": 2048, "sample_count": 3,
        "include_trigger": False, "chunk_count": 2,
    }
    selection = PreviewSelection.from_value(value)
    value["rows"].append(4)
    exported = selection.to_dict()
    exported["rows"].reverse()
    assert selection.rows == (9, 2, 1000)
    assert selection.sample_start == 2048
    assert selection.sample_count == 3
    assert selection.include_trigger is False
    assert selection.chunk_count == 2


@pytest.mark.parametrize("value", [None, [], (), "", 1, True])
def test_selection_requires_an_object(value):
    with pytest.raises(ValueError):
        PreviewSelection.from_value(value)


@pytest.mark.parametrize("value", [
    {"unknown": 1}, {"rows": None}, {"rows": "0"}, {"rows": (0,)},
    {"rows": [0, 0]}, {"rows": [-1]}, {"rows": [True]}, {"rows": [1.0]},
    {"rows": ["1"]}, {"rows": [[]]}, {"rows": list(range(17))},
    {"rows": [], "include_trigger": False},
    {"sample_start": -1}, {"sample_start": False}, {"sample_start": 0.0},
    {"sample_start": "0"}, {"sample_start": None},
    {"sample_count": 0}, {"sample_count": 513}, {"sample_count": True},
    {"sample_count": 1.0}, {"sample_count": "1"}, {"sample_count": None},
    {"include_trigger": 0}, {"include_trigger": 1}, {"include_trigger": "true"},
    {"include_trigger": None},
    {"chunk_count": 0}, {"chunk_count": 33}, {"chunk_count": True},
    {"chunk_count": 1.0}, {"chunk_count": "1"}, {"chunk_count": None},
    {"rows": list(range(16)), "sample_count": 512, "include_trigger": True},
])
def test_invalid_fields_are_rejected_without_coercion(value):
    with pytest.raises(ValueError):
        PreviewSelection.from_value(value)


@pytest.mark.parametrize("value", [
    {"rows": list(range(16)), "sample_count": 512, "include_trigger": False},
    {"rows": list(range(15)), "sample_count": 512, "include_trigger": True},
    {"rows": [], "sample_count": 512, "include_trigger": True, "chunk_count": 1},
])
def test_value_budget_includes_the_trigger_and_accepts_exact_boundaries(value):
    selection = PreviewSelection.from_value(value)
    assert selection.to_dict() == {**PreviewSelection().to_dict(), **value}


@pytest.mark.parametrize("fields", [
    {"rows": [0]}, {"rows": (0, 0)}, {"rows": (False,)},
    {"rows": (), "include_trigger": False}, {"sample_count": 0},
    {"chunk_count": 33}, {"include_trigger": 1},
])
def test_direct_construction_enforces_the_same_constraints(fields):
    with pytest.raises(ValueError):
        PreviewSelection(**fields)


def test_limits_are_json_values_and_do_not_share_mutable_state():
    expected = {
        "max_chunks": 32, "max_rows": 16, "max_samples": 512, "max_values": 8192,
        "max_frame_bytes": 128 * 1024, "max_cache_bytes": 4 * 1024 * 1024,
        "preview_queue_size": 8, "capture_timeout_seconds": 10.0,
    }
    actual = limits()
    assert json.loads(json.dumps(actual)) == expected
    actual["max_chunks"] = 0
    assert limits() == expected
