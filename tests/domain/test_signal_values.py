"""Signal metadata fails early without changing sample ownership or snapshots."""
import numpy as np
import pytest

from omnibci.domain.signal import SignalChunk


@pytest.mark.parametrize("field", ["timestamp", "sequence_id"])
@pytest.mark.parametrize("value", [True, np.bool_(False), -1, 0.0, 1.5, np.nan, np.inf, "1", np.array(1)])
def test_chunk_requires_nonnegative_integer_metadata(field, value):
    fields = {"timestamp": 0, "sequence_id": 0, "raw_data": np.zeros((2, 3))}
    fields[field] = value

    with pytest.raises(ValueError, match=field):
        SignalChunk(**fields)


@pytest.mark.parametrize("data", [
    [[1, 2], [3, 4]],
    np.zeros((2, 3), dtype=bool),
    np.zeros((2, 3), dtype=complex),
    np.zeros((2, 3), dtype=object),
    np.full((2, 3), "1"),
])
def test_signal_values_require_real_numeric_arrays(data):
    with pytest.raises(TypeError, match="array|dtype"):
        SignalChunk(0, 0, data)


@pytest.mark.parametrize("shape", [(3,), (2, 2, 2)])
def test_signal_values_reject_non_matrix_shapes(shape):
    data = np.zeros(shape)
    with pytest.raises(ValueError, match="2-D"):
        SignalChunk(0, 0, data)


def test_chunk_requires_a_signal_row_and_an_event_row():
    with pytest.raises(ValueError, match="2 rows"):
        SignalChunk(0, 0, np.zeros((1, 3)))


@pytest.mark.parametrize("dtype", [np.int16, np.uint32, np.float32, np.float64])
def test_chunk_keeps_borrowed_storage_and_views(dtype):
    backing = np.arange(12, dtype=dtype).reshape(3, 4)
    raw = backing[:, ::2]
    raw.flags.writeable = False

    chunk = SignalChunk(1_700_000_000_000_000_001, 0, raw)

    assert chunk.timestamp == 1_700_000_000_000_000_001
    assert chunk.raw_data is raw
    assert np.shares_memory(chunk.data, backing)
    assert np.shares_memory(chunk.triggers, backing)
    assert not chunk.raw_data.flags.writeable


def test_chunk_allows_nan_samples_and_empty_internal_slices():
    raw = np.array([[np.nan, 1.0], [0.0, 7.0]])
    chunk = SignalChunk(0, 0, raw)
    empty = SignalChunk(0, 0, raw[:, :0])

    assert np.isnan(chunk.data[0, 0])
    assert empty.raw_data.shape == (2, 0)
