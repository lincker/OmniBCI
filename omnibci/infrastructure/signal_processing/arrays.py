"""Borrow arrays across processing plugin calls without copying signal storage."""
import numpy as np


def readonly(data: np.ndarray) -> np.ndarray:
    if not data.flags.writeable:
        return data
    view = data.view()
    view.flags.writeable = False
    return view


def matrix(data, name: str, *, channels=None, shape=None) -> np.ndarray:
    if not isinstance(data, np.ndarray) or data.ndim != 2 or data.dtype.kind not in "iuf":
        raise TypeError(f"{name} must be a two-dimensional real numeric array")
    if (channels is not None and data.shape[0] != channels
            or shape is not None and data.shape != shape):
        raise ValueError(f"{name} shape {data.shape} does not match the prepared window")
    return readonly(data)
