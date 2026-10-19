"""Durable checkpoints and explicit recovery of inactive raw recordings."""

import json
from pathlib import Path
import subprocess
import sys
import time

import numpy as np
import pytest

from omnibci.domain.signal import SignalChunk
from omnibci.infrastructure.acquisition import recording as module
from omnibci.infrastructure.acquisition.recording import (
    LocalSignalRecorder, inspect_recording, iter_recorded_chunks,
    iter_valid_recorded_chunks, recover_recording,
)


def chunk(sequence=0):
    return SignalChunk(1000 + sequence, sequence, np.array([[1., 2.], [0., 9.]]))


def document(path):
    return json.loads((path / "metadata.json").read_text())


def update_document(path, **changes):
    value = document(path)
    value.update(changes)
    (path / "metadata.json").write_text(json.dumps(value))


def recording(tmp_path):
    writer = LocalSignalRecorder(tmp_path)
    info = writer.start({"session_id": "session-1"})
    return writer, Path(info["path"])


def test_checkpoint_updates_after_both_streams_and_before_flush_returns(tmp_path, monkeypatch):
    writer, path = recording(tmp_path)
    writer.append(chunk())
    assert document(path)["checkpoint"]["chunk_count"] == 0
    trace = []
    fsync = module.os.fsync
    replace = module.os.replace
    data_fd, index_fd = writer._data.fileno(), writer._index.fileno()

    def sync(fd):
        trace.append("data" if fd == data_fd else "index" if fd == index_fd else "metadata")
        fsync(fd)

    def replace_metadata(source, target):
        assert trace[:3] == ["data", "index", "metadata"]
        trace.append("replace")
        replace(source, target)

    with monkeypatch.context() as patch:
        patch.setattr(module.os, "fsync", sync)
        patch.setattr(module.os, "replace", replace_metadata)
        writer.flush()
    checkpoint = document(path)["checkpoint"]
    assert checkpoint == {"chunk_count": 1, "sample_count": 2,
                          "data_bytes": (path / "chunks.npy").stat().st_size,
                          "index_bytes": (path / "chunks.jsonl").stat().st_size}
    assert inspect_recording(path)["valid_prefix"] == checkpoint
    writer.finish()


@pytest.mark.parametrize("stage", ["data", "index", "metadata"])
def test_failed_flush_preserves_previous_checkpoint_and_sticky_failure(tmp_path, monkeypatch, stage):
    writer, path = recording(tmp_path)
    writer.append(chunk())
    writer.flush()
    previous = document(path)["checkpoint"]
    writer.append(chunk(1))
    fsync = module.os.fsync
    target_fd = writer._data.fileno() if stage == "data" else writer._index.fileno()

    def sync(fd):
        if fd == target_fd:
            raise OSError("checkpoint unavailable")
        fsync(fd)

    def replace(*args):
        raise OSError("checkpoint unavailable")

    with monkeypatch.context() as patch:
        patch.setattr(module.os, "replace", replace) if stage == "metadata" else patch.setattr(module.os, "fsync", sync)
        with pytest.raises(OSError, match="checkpoint unavailable"):
            writer.flush()
    assert document(path)["checkpoint"] == previous
    with pytest.raises(RuntimeError, match="failed"):
        writer.append(chunk(2))
    assert writer.finish()["status"] == "failed"
    assert recover_recording(path)["status"] == "failed"


def test_recovery_rejects_an_active_writer_and_preserves_completed_metadata(tmp_path):
    writer, path = recording(tmp_path)
    writer.append(chunk())
    writer.flush()
    with pytest.raises(RuntimeError, match="owned by another process"):
        recover_recording(path)
    writer.finish()
    before = (path / "metadata.json").read_bytes()
    first = recover_recording(path)
    assert first["status"] == "completed"
    assert recover_recording(path) == first
    assert (path / "metadata.json").read_bytes() == before


def test_failed_finish_releases_writer_lease_after_handles_close(tmp_path, monkeypatch):
    writer, path = recording(tmp_path)
    writer.append(chunk())

    def unavailable(*args):
        raise OSError("metadata unavailable")

    with monkeypatch.context() as patch:
        patch.setattr(module.os, "replace", unavailable)
        with pytest.raises(OSError, match="metadata unavailable"):
            writer.finish()
    assert recover_recording(path)["status"] == "interrupted"


@pytest.mark.skipif(module.os.name == "nt", reason="Directory fsync is a POSIX operation")
def test_failed_directory_sync_cannot_leave_a_completed_manifest(tmp_path, monkeypatch):
    writer, path = recording(tmp_path)
    writer.append(chunk())
    fsync = module.os.fsync
    calls = 0

    def fail_once(fd):
        nonlocal calls
        calls += 1
        if calls == 4:
            raise OSError("directory sync failed")
        fsync(fd)

    monkeypatch.setattr(module.os, "fsync", fail_once)
    with pytest.raises(OSError, match="directory sync failed"):
        writer.finish()
    assert writer.finish()["status"] == document(path)["status"] == "failed"


def test_unindexed_array_is_preserved_without_inventing_chunk_identity(tmp_path, monkeypatch):
    writer, path = recording(tmp_path)
    writer.append(chunk())
    writer.flush()

    def unavailable(*args):
        raise OSError("index unavailable")

    monkeypatch.setattr(writer._index, "write", unavailable)
    with pytest.raises(OSError, match="index unavailable"):
        writer.append(chunk(1))
    writer.finish()
    before = {name: (path / name).read_bytes() for name in ("chunks.npy", "chunks.jsonl")}
    report = inspect_recording(path)
    assert report["valid_prefix"]["chunk_count"] == report["checkpoint"]["chunk_count"] == 1
    assert report["unindexed_data_bytes"] > 0
    assert [value.sequence_id for value in iter_valid_recorded_chunks(path)] == [0]
    assert recover_recording(path)["status"] == "failed"
    assert before == {name: (path / name).read_bytes() for name in before}


@pytest.mark.parametrize("tail", [b'{"sequence_id": 1', b'{}\n', b'[]\n'])
def test_invalid_unconfirmed_tail_requires_explicit_prefix_reader(tmp_path, tail):
    writer, path = recording(tmp_path)
    writer.append(chunk())
    writer.finish(status="interrupted")
    with (path / "chunks.jsonl").open("ab") as stream:
        stream.write(tail)
    with pytest.raises(ValueError, match="Invalid recorded chunk 2"):
        list(iter_recorded_chunks(path))
    report = inspect_recording(path)
    assert report["invalid_index_bytes"] == len(tail)
    assert report["tail_error"]
    assert len(list(iter_valid_recorded_chunks(path))) == 1


@pytest.mark.parametrize("field,value", [
    ("offset", 1), ("offset", True), ("samples", 999), ("samples", "2"),
    ("sequence_id", -1), ("sequence_id", True), ("timestamp", float("nan")),
    ("config_revision", 2),
])
def test_corruption_inside_checkpoint_cannot_be_recovered(tmp_path, field, value):
    writer, path = recording(tmp_path)
    writer.append(chunk())
    writer.finish(status="interrupted")
    entry = json.loads((path / "chunks.jsonl").read_text())
    entry[field] = value
    (path / "chunks.jsonl").write_text(json.dumps(entry) + "\n")
    before = (path / "metadata.json").read_bytes()
    with pytest.raises(ValueError, match="Corruption inside"):
        recover_recording(path)
    assert (path / "metadata.json").read_bytes() == before


def test_truncated_confirmed_array_and_false_checkpoint_boundaries_are_errors(tmp_path):
    writer, path = recording(tmp_path)
    writer.append(chunk())
    writer.finish(status="interrupted")
    original = (path / "chunks.npy").read_bytes()
    (path / "chunks.npy").write_bytes(original[:-1])
    with pytest.raises(ValueError, match="Corruption inside"):
        inspect_recording(path)
    (path / "chunks.npy").write_bytes(original)
    checkpoint = document(path)["checkpoint"]
    checkpoint["data_bytes"] -= 1
    update_document(path, checkpoint=checkpoint)
    with pytest.raises(ValueError, match="Corruption inside"):
        inspect_recording(path)


def test_completed_recording_requires_all_chunks_confirmed(tmp_path):
    writer, path = recording(tmp_path)
    writer.append(chunk())
    writer.flush()
    previous = document(path)["checkpoint"]
    writer.append(chunk(1))
    writer.finish()
    update_document(path, checkpoint=previous)
    with pytest.raises(ValueError, match="Completed recording"):
        inspect_recording(path)


def test_legacy_without_checkpoint_reports_best_effort_and_keeps_original_files(tmp_path):
    writer, path = recording(tmp_path)
    writer.append(chunk())
    writer.finish()
    value = document(path)
    value.pop("checkpoint")
    value.pop("format")
    value["status"] = "recording"
    (path / "metadata.json").write_text(json.dumps(value))
    with (path / "chunks.jsonl").open("ab") as stream:
        stream.write(b'{"sequence_id":')
    before = {name: (path / name).read_bytes() for name in ("chunks.npy", "chunks.jsonl")}
    recovered = recover_recording(path)
    assert recovered["status"] == "interrupted"
    assert recovered["recovery"]["legacy"]
    assert recovered["recovery"]["checkpoint"] is None
    assert recovered["chunk_count"] == 1
    assert recovered == recover_recording(path)
    assert before == {name: (path / name).read_bytes() for name in before}


def test_empty_crash_before_data_files_created_can_be_reported(tmp_path):
    writer, path = recording(tmp_path)
    writer.finish(status="interrupted")
    (path / "chunks.npy").unlink()
    (path / "chunks.jsonl").unlink()
    update_document(path, status="recording")
    assert recover_recording(path)["chunk_count"] == 0
    assert list(iter_valid_recorded_chunks(path)) == []


CRASH_SCRIPT = '''
import os, sys, time
from pathlib import Path
import numpy as np
from omnibci.domain.signal import SignalChunk
from omnibci.infrastructure.acquisition import recording as module
root, stage = Path(sys.argv[1]), sys.argv[2]
writer = module.LocalSignalRecorder(root)
info = writer.start({"session_id": "crash-session"})
def chunk(number):
    return SignalChunk(1000 + number, number, np.array([[1., 2.], [0., 9.]]))
writer.append(chunk(0), config_revision="before")
writer.flush()
def pause():
    (root / "ready").write_text(info["path"])
    while True:
        time.sleep(1)
if stage == "data":
    save = module.np.save
    def save_and_pause(stream, *args, **kwargs):
        save(stream, *args, **kwargs)
        stream.flush()
        os.fsync(stream.fileno())
        pause()
    module.np.save = save_and_pause
elif stage in {"before_replace", "after_replace"}:
    replace = module.os.replace
    def replace_and_pause(source, target):
        if stage == "before_replace":
            pause()
        replace(source, target)
        pause()
    module.os.replace = replace_and_pause
writer.append(chunk(1), config_revision="after")
if stage == "index":
    for stream in (writer._data, writer._index):
        stream.flush()
        os.fsync(stream.fileno())
    pause()
writer.flush()
pause()
'''


@pytest.mark.parametrize("stage,valid,confirmed", [
    ("data", 1, 1), ("index", 2, 1), ("before_replace", 2, 1), ("after_replace", 2, 2),
])
def test_killed_writer_recovers_only_verifiable_prefix(tmp_path, stage, valid, confirmed):
    process = subprocess.Popen([sys.executable, "-c", CRASH_SCRIPT, str(tmp_path), stage],
                               stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    try:
        deadline = time.monotonic() + 15
        while not (tmp_path / "ready").exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.02)
        assert (tmp_path / "ready").exists(), "Recording child did not reach the crash stage"
        path = Path((tmp_path / "ready").read_text())
        with pytest.raises(RuntimeError, match="owned by another process"):
            recover_recording(path)
        process.kill()
        process.communicate(timeout=5)
        before = {name: (path / name).read_bytes() for name in ("chunks.npy", "chunks.jsonl")}
        recovered = recover_recording(path)
        assert recovered["status"] == "interrupted"
        assert recovered["chunk_count"] == valid
        assert recovered["recovery"]["checkpoint"]["chunk_count"] == confirmed
        assert [value.sequence_id for value in iter_valid_recorded_chunks(path)] == list(range(valid))
        assert recovered == recover_recording(path)
        assert before == {name: (path / name).read_bytes() for name in before}
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=5)
