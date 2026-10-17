"""Compare epoch buffering with the original np.append implementation.

Run from a checkout with its dependencies installed:
    venv/bin/python scripts/benchmark_epoch_buffer.py --repeats 3

Inputs are generated before measurement. Wall time and peak tracemalloc memory
are measured in separate runs. This is a buffer microbenchmark: acquisition,
queue dwell, state-loop diagnostics, filtering, actual decoding, and completed
Epoch construction are excluded. Results depend on hardware and NumPy version.
"""

import argparse
import gc
import json
import logging
from pathlib import Path
import platform
import statistics
import sys
import time
import tracemalloc

import numpy as np

_root = str(Path(__file__).resolve().parents[1])
if _root not in sys.path:
    sys.path.insert(0, _root)

from omnibci.domain.ports.algorithm import Decoder
from omnibci.domain.signal import SignalChunk
from omnibci.infrastructure.signal_processing.epoch.epoch_processor import EpochProcessor


class WaitingDecoder(Decoder):
    def should_decode(self, window):
        return False

    def decode(self, window, latest_samples):
        return None


class AppendReference(EpochProcessor):
    """Original append methods; all other processor behavior stays identical."""

    def append_chunk(self, chunk):
        self._current_chunk_data = chunk.data
        self._epoch_data = np.append(self._epoch_data, chunk.data, axis=1)
        self._pending_decode = True

    def append_until_index(self, chunk, index):
        if index < 0:
            raise ValueError(f"Invalid slice index: {index}")
        data_slice = chunk.data[:, :index]
        self._current_chunk_data = data_slice
        self._epoch_data = np.append(self._epoch_data, data_slice, axis=1)
        self._pending_decode = True


def run(processor_class, chunks):
    processor = processor_class(decoder=WaitingDecoder())
    processor.initialize_epoch(chunks[0], start_idx=0, event_id=7)
    for chunk in chunks[1:]:
        processor.process_chunk(chunk)
    return processor


def measure(processor_class, chunks, repeats):
    processor = run(processor_class, chunks)
    del processor
    durations = []
    for _ in range(repeats):
        gc.collect()
        started = time.perf_counter()
        processor = run(processor_class, chunks)
        durations.append(time.perf_counter() - started)
        del processor

    # No processor or borrowed windows from earlier runs remain alive here.
    gc.collect()
    tracemalloc.start()
    try:
        processor = run(processor_class, chunks)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    result = {
        "median_ms": statistics.median(durations) * 1000,
        "peak_traced_mib": peak / 1024**2,
        "window_c_contiguous": bool(processor._epoch_data.flags.c_contiguous),
    }
    del processor
    return result


def benchmark(channels, total_samples, chunk_samples, repeats):
    raw = np.random.default_rng(12).normal(size=(channels + 1, total_samples))
    raw[-1] = 0
    chunks = [
        SignalChunk(0.0, index, raw[:, start:start + chunk_samples].copy())
        for index, start in enumerate(range(0, total_samples, chunk_samples))
    ]
    del raw

    # Correctness checks and their temporary arrays are outside both measurements.
    reference = run(AppendReference, chunks)
    capacity = run(EpochProcessor, chunks)
    np.testing.assert_array_equal(capacity._epoch_data, reference._epoch_data)
    assert capacity._epoch_data.dtype == reference._epoch_data.dtype
    del reference, capacity

    reference_result = measure(AppendReference, chunks, repeats)
    capacity_result = measure(EpochProcessor, chunks, repeats)
    print(json.dumps({
        "channels": channels,
        "samples": total_samples,
        "chunk_samples": chunk_samples,
        "chunks": len(chunks),
        "final_data_mib": channels * total_samples * np.dtype(np.float64).itemsize / 1024**2,
        "np_append": reference_result,
        "capacity": capacity_result,
        "speedup": reference_result["median_ms"] / capacity_result["median_ms"],
    }), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repeats", type=int, default=3, help="Wall-time measurements per implementation (default: 3)")
    parser.add_argument("--long", action="store_true", help="Also measure 64 channels, 120000 samples, 100-sample chunks")
    args = parser.parse_args()
    if args.repeats < 1:
        parser.error("--repeats must be positive")
    logging.disable(logging.CRITICAL)
    print(json.dumps({
        "python": platform.python_version(),
        "numpy": np.__version__,
        "platform": platform.platform(),
        "repeats": args.repeats,
        "seed": 12,
    }), flush=True)
    specs = [(8, 3000, 100), (64, 30000, 100), (64, 30000, 10)]
    if args.long:
        specs.append((64, 120000, 100))
    for spec in specs:
        benchmark(*spec, repeats=args.repeats)


if __name__ == "__main__":
    main()
