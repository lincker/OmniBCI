"""Bounded selections for inspecting normalized acquisition output."""
from dataclasses import dataclass


MAX_CHUNKS = 32
MAX_ROWS = 16
MAX_SAMPLES = 512
MAX_VALUES = 8192
MAX_FRAME_BYTES = 128 * 1024
MAX_CACHE_BYTES = 4 * 1024 * 1024
PREVIEW_QUEUE_SIZE = 8
CAPTURE_TIMEOUT_SECONDS = 10.0


def limits() -> dict:
    return {
        "max_chunks": MAX_CHUNKS,
        "max_rows": MAX_ROWS,
        "max_samples": MAX_SAMPLES,
        "max_values": MAX_VALUES,
        "max_frame_bytes": MAX_FRAME_BYTES,
        "max_cache_bytes": MAX_CACHE_BYTES,
        "preview_queue_size": PREVIEW_QUEUE_SIZE,
        "capture_timeout_seconds": CAPTURE_TIMEOUT_SECONDS,
    }


@dataclass(frozen=True)
class PreviewSelection:
    rows: tuple[int, ...] = (0,)
    sample_start: int = 0
    sample_count: int = 128
    include_trigger: bool = True
    chunk_count: int = MAX_CHUNKS

    def __post_init__(self) -> None:
        if (type(self.rows) is not tuple or len(self.rows) > MAX_ROWS
                or any(type(row) is not int or row < 0 for row in self.rows)):
            raise ValueError(f"rows must contain at most {MAX_ROWS} non-negative integers")
        if len(set(self.rows)) != len(self.rows):
            raise ValueError("rows must not contain duplicates")
        if type(self.include_trigger) is not bool:
            raise ValueError("include_trigger must be a boolean")
        if not self.rows and not self.include_trigger:
            raise ValueError("Select at least one signal row or the trigger row")
        if type(self.sample_start) is not int or self.sample_start < 0:
            raise ValueError("sample_start must be a non-negative integer")
        for name, maximum in (("sample_count", MAX_SAMPLES), ("chunk_count", MAX_CHUNKS)):
            value = getattr(self, name)
            if type(value) is not int or not 1 <= value <= maximum:
                raise ValueError(f"{name} must be an integer from 1 to {maximum}")
        if (len(self.rows) + self.include_trigger) * self.sample_count > MAX_VALUES:
            raise ValueError(f"A preview chunk must contain at most {MAX_VALUES} selected values")

    @classmethod
    def from_value(cls, value: dict) -> "PreviewSelection":
        if not isinstance(value, dict) or set(value) - {
            "rows", "sample_start", "sample_count", "include_trigger", "chunk_count",
        }:
            raise ValueError("Preview selection must be an object with known fields")
        rows = value.get("rows", [0])
        if type(rows) is not list:
            raise ValueError("rows must be an array")
        return cls(**{**value, "rows": tuple(rows)})

    def to_dict(self) -> dict:
        return {
            "rows": list(self.rows),
            "sample_start": self.sample_start,
            "sample_count": self.sample_count,
            "include_trigger": self.include_trigger,
            "chunk_count": self.chunk_count,
        }
