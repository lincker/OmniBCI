"""Measure paced local recording and verify every persisted sample.

This exercises the writer and periodic durability checkpoints, not a hardware
driver or the signal-processing pipeline. Temporary recordings are removed
after verification. Example: --seconds 1800 --channels 128 --sample-rate 2048.
"""

import argparse
import json
from pathlib import Path
import platform
import sys
import tempfile
import time
import tracemalloc

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from omnibci.domain.signal import SignalChunk
from omnibci.infrastructure.acquisition.recording import LocalSignalRecorder, iter_recorded_chunks


def run(args):
    interval = args.chunk_samples / args.sample_rate
    count = int(args.seconds / interval)
    timings = np.empty(count)
    lateness = np.empty(count)
    memory = []
    with tempfile.TemporaryDirectory(prefix="omnibci-recording-benchmark-") as directory:
        recorder = LocalSignalRecorder(Path(directory))
        info = recorder.start({"session_id": "benchmark", "subject_id": None})
        base_timestamp = time.time_ns()
        tracemalloc.start()
        start = time.perf_counter()
        next_flush = start + 1
        next_memory = start
        for sequence in range(count):
            deadline = start + sequence * interval
            time.sleep(max(0, deadline - time.perf_counter()))
            before = time.perf_counter()
            lateness[sequence] = max(0, before - deadline)
            array = np.full((args.channels + 1, args.chunk_samples), sequence, dtype=np.float64)
            array[-1] = sequence % 256
            recorder.append(SignalChunk(base_timestamp + round(sequence * interval * 1e9), sequence, array))
            if time.perf_counter() >= next_flush:
                recorder.flush()
                next_flush = time.perf_counter() + 1
            timings[sequence] = time.perf_counter() - before
            if time.perf_counter() >= next_memory:
                memory.append({"seconds": round(time.perf_counter() - start, 2),
                               "current_bytes": tracemalloc.get_traced_memory()[0]})
                next_memory = time.perf_counter() + 60
        result = recorder.finish()
        elapsed = time.perf_counter() - start
        current, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        verified = 0
        for sequence, chunk in enumerate(iter_recorded_chunks(info["path"])):
            assert chunk.sequence_id == sequence
            assert chunk.timestamp == base_timestamp + round(sequence * interval * 1e9)
            assert chunk.raw_data.dtype == np.float64
            assert chunk.raw_data.shape == (args.channels + 1, args.chunk_samples)
            assert np.all(chunk.raw_data[:-1] == sequence)
            assert np.all(chunk.raw_data[-1] == sequence % 256)
            verified += 1
        assert verified == count == result["chunk_count"]
        assert result["sample_count"] == count * args.chunk_samples
        return {
            "platform": platform.platform(), "python": platform.python_version(), "numpy": np.__version__,
            "channels_excluding_trigger": args.channels, "sample_rate": args.sample_rate,
            "chunk_samples": args.chunk_samples, "requested_seconds": args.seconds,
            "elapsed_seconds": elapsed, "verified_chunks": verified,
            "verified_samples_per_channel": result["sample_count"], "status": result["status"],
            "payload_bytes": verified * (args.channels + 1) * args.chunk_samples * 8,
            "chunk_budget_ms": interval * 1000,
            "write_including_checkpoint_p99_ms": float(np.percentile(timings, 99)) * 1000,
            "write_including_checkpoint_max_ms": float(timings.max()) * 1000,
            "scheduling_lateness_max_ms": float(lateness.max()) * 1000,
            "writes_over_chunk_budget": int(np.count_nonzero(timings > interval)),
            "traced_current_bytes": current, "traced_peak_bytes": peak, "memory_samples": memory,
        }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seconds", type=float, default=1800)
    parser.add_argument("--channels", type=int, default=128)
    parser.add_argument("--sample-rate", type=int, default=2048)
    parser.add_argument("--chunk-samples", type=int, default=64)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if min(args.seconds, args.channels, args.sample_rate, args.chunk_samples) <= 0:
        parser.error("All workload dimensions must be positive")
    if args.seconds * args.sample_rate < args.chunk_samples:
        parser.error("Duration must include at least one chunk")
    report = run(args)
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))
