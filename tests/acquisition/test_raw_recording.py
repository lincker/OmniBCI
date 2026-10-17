"""Raw recording durability, identity, and round trips through disk files."""

import json
from pathlib import Path
from uuid import UUID

import numpy as np
import pytest

from omnibci.domain.signal import SignalChunk
from omnibci.domain.signal_info import ChannelDescriptor, SignalInfo
from omnibci.infrastructure.acquisition import recording as module
from omnibci.infrastructure.acquisition.recording import LocalSignalRecorder, iter_recorded_chunks


def metadata(session_id="session-1", **overrides):
    return {
        "session_id": session_id, "subject_id": "subject-1", "paradigm": "test",
        "continuation_of": None, "config_revision": "revision-1", "generation": "generation-1",
        "signal_info": SignalInfo("source", 250, (ChannelDescriptor(0, "Cz"),), 1).to_dict(),
        **overrides,
    }


def chunk(sequence_id=1, dtype=np.float32):
    return SignalChunk(1_792_000_000_123_456_789 + sequence_id, sequence_id,
                       np.array([[1.25, -2.5, 3.0], [0, 7, 0]], dtype=dtype))


def read_metadata(info):
    return json.loads((Path(info["path"]) / "metadata.json").read_text())


def test_reopen_round_trip_preserves_raw_trigger_dtype_timestamps_and_offsets(tmp_path):
    recorder = LocalSignalRecorder(tmp_path)
    info = recorder.start(metadata())
    originals = [chunk(2, np.float32), chunk(5, np.float64), chunk(6, np.int16)]
    for value in originals:
        recorder.append(value, config_revision="revision-1")
    recorder.flush()
    assert read_metadata(info)["status"] == "recording"
    result = recorder.finish()
    assert result == {**info, "status": "completed", "chunk_count": 3, "sample_count": 9}
    del recorder

    loaded = list(iter_recorded_chunks(info["path"]))
    for expected, actual in zip(originals, loaded, strict=True):
        assert actual.sequence_id == expected.sequence_id
        assert actual.timestamp == expected.timestamp
        assert actual.raw_data.dtype == expected.raw_data.dtype
        np.testing.assert_array_equal(actual.raw_data, expected.raw_data)
        np.testing.assert_array_equal(actual.triggers, expected.triggers)
    entries = [json.loads(line) for line in (Path(info["path"]) / "chunks.jsonl").read_text().splitlines()]
    assert entries[0]["offset"] == 0
    assert entries[1]["offset"] > entries[0]["offset"]
    assert entries[2]["offset"] > entries[1]["offset"]
    assert {entry["config_revision"] for entry in entries} == {"revision-1"}
    document = read_metadata(info)
    assert document["signal_info"] == metadata()["signal_info"]
    assert document["subject_id"] == "subject-1"
    assert document["status"] == "completed"
    assert document["sample_count"] == 9
    assert info["format"] == document["format"] == "omnibci.numpy-chunks"
    assert info["format_version"] == document["format_version"] == 1


def test_empty_recording_and_session_lifecycle(tmp_path):
    recorder = LocalSignalRecorder(tmp_path)
    first = metadata()
    info = recorder.start(first)
    assert recorder.start(first) == info
    with pytest.raises(RuntimeError, match="already active"):
        recorder.start(metadata("another-session"))
    result = recorder.finish()
    assert result["chunk_count"] == result["sample_count"] == 0
    assert list(iter_recorded_chunks(info["path"])) == []
    assert recorder.finish() == result
    with pytest.raises(RuntimeError, match="cannot be reused"):
        recorder.start(first)
    with pytest.raises(RuntimeError, match="No recording is active"):
        recorder.append(chunk())
    second = recorder.start(metadata("another-session"))
    assert second["id"] != info["id"]
    recorder.finish()


def test_identity_is_metadata_and_cannot_escape_root(tmp_path):
    recorder = LocalSignalRecorder(tmp_path)
    identity = metadata("../../outside-session", subject_id="/outside-subject")
    info = recorder.start(identity)
    path = Path(info["path"])
    assert path.parent == tmp_path
    assert str(UUID(path.name)) == path.name == info["id"]
    assert read_metadata(info)["subject_id"] == "/outside-subject"
    assert read_metadata(info)["session_id"] == "../../outside-session"
    recorder.finish()


def test_default_root_honors_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("OMNIBCI_RECORDING_DIR", str(tmp_path))
    assert LocalSignalRecorder().root == tmp_path


def test_relative_environment_root_is_rejected(monkeypatch):
    monkeypatch.setenv("OMNIBCI_RECORDING_DIR", "relative-recordings")
    with pytest.raises(ValueError, match="absolute path"):
        LocalSignalRecorder()


def test_default_root_uses_application_data_directory(tmp_path, monkeypatch):
    monkeypatch.delenv("OMNIBCI_RECORDING_DIR", raising=False)
    monkeypatch.setattr(module, "user_data_path", lambda *args, **kwargs: tmp_path)
    assert LocalSignalRecorder().root == tmp_path / "recordings"


@pytest.mark.parametrize("invalid", [
    SignalChunk(1, 1, np.array([["a"], ["b"]])),
    SignalChunk(1, 1, np.ones((3, 2))),
    SignalChunk(1, -1, np.ones((2, 2))),
    SignalChunk(float("nan"), 1, np.ones((2, 2))),
])
def test_invalid_chunks_make_recording_failed(tmp_path, invalid):
    recorder = LocalSignalRecorder(tmp_path)
    info = recorder.start(metadata())
    with pytest.raises(ValueError):
        recorder.append(invalid)
    with pytest.raises(RuntimeError, match="failed"):
        recorder.append(chunk())
    assert recorder.finish()["status"] == "failed"
    assert read_metadata(info)["status"] == "failed"


def test_sequence_cannot_repeat_and_legacy_shape_stays_consistent(tmp_path):
    recorder = LocalSignalRecorder(tmp_path)
    recorder.start(metadata(signal_info=None))
    recorder.append(chunk(2))
    with pytest.raises(ValueError, match="strictly increasing"):
        recorder.append(chunk(2))
    recorder.finish()
    recorder.start(metadata("second", signal_info=None))
    recorder.append(chunk())
    with pytest.raises(ValueError, match="rows"):
        recorder.append(SignalChunk(2, 2, np.ones((3, 3))))
    recorder.finish()


def test_disk_append_failure_preserves_prior_data_and_is_sticky(tmp_path, monkeypatch):
    recorder = LocalSignalRecorder(tmp_path)
    info = recorder.start(metadata())
    recorder.append(chunk())

    def disk_full(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(module.np, "save", disk_full)
    with pytest.raises(OSError, match="disk full"):
        recorder.append(chunk(2))
    assert recorder.finish()["status"] == "failed"
    assert read_metadata(info)["status"] == "failed"
    restored = list(iter_recorded_chunks(info["path"]))
    assert len(restored) == 1
    np.testing.assert_array_equal(restored[0].raw_data, chunk().raw_data)


def test_index_write_failure_retains_raw_bytes_and_prior_indexed_chunks(tmp_path, monkeypatch):
    recorder = LocalSignalRecorder(tmp_path)
    info = recorder.start(metadata())
    recorder.append(chunk())
    recorder.flush()
    old_size = (Path(info["path"]) / "chunks.npy").stat().st_size

    def cannot_index(*args):
        raise OSError("index full")

    monkeypatch.setattr(recorder._index, "write", cannot_index)
    with pytest.raises(OSError, match="index full"):
        recorder.append(chunk(2))
    result = recorder.finish()
    assert result["status"] == "failed"
    assert result["chunk_count"] == 1
    assert (Path(info["path"]) / "chunks.npy").stat().st_size > old_size
    assert len(list(iter_recorded_chunks(info["path"]))) == 1


def test_finish_flush_failure_never_reports_completed(tmp_path, monkeypatch):
    recorder = LocalSignalRecorder(tmp_path)
    info = recorder.start(metadata())
    recorder.append(chunk())
    original = module.os.fsync
    failed_once = False

    def fail_once(fd):
        nonlocal failed_once
        if not failed_once:
            failed_once = True
            raise OSError("flush failed")
        return original(fd)

    monkeypatch.setattr(module.os, "fsync", fail_once)
    with pytest.raises(OSError, match="flush failed"):
        recorder.finish()
    assert recorder.finish()["status"] == "failed"
    assert read_metadata(info)["status"] == "failed"
    assert len(list(iter_recorded_chunks(info["path"]))) == 1


def test_metadata_failure_leaves_recording_unfinished_on_disk(tmp_path, monkeypatch):
    recorder = LocalSignalRecorder(tmp_path)
    info = recorder.start(metadata())
    recorder.append(chunk())

    def cannot_replace(*args):
        raise OSError("metadata unavailable")

    monkeypatch.setattr(module.os, "replace", cannot_replace)
    with pytest.raises(OSError, match="metadata unavailable"):
        recorder.finish()
    assert read_metadata(info)["status"] == "recording"
    assert recorder.finish()["status"] == "failed"
    assert len(list(iter_recorded_chunks(info["path"]))) == 1


def test_close_failure_is_reported_and_cannot_mark_completed(tmp_path, monkeypatch):
    recorder = LocalSignalRecorder(tmp_path)
    info = recorder.start(metadata())
    recorder.append(chunk())
    close = recorder._index.close

    def failed_close():
        close()
        raise OSError("close failed")

    monkeypatch.setattr(recorder._index, "close", failed_close)
    with pytest.raises(OSError, match="close failed"):
        recorder.finish()
    assert recorder.finish()["status"] == "failed"
    assert read_metadata(info)["status"] == "failed"


def test_explicit_failure_is_not_later_completed(tmp_path):
    recorder = LocalSignalRecorder(tmp_path)
    info = recorder.start(metadata())
    result = recorder.finish(error="source disconnected")
    assert result["status"] == "failed"
    assert recorder.finish()["status"] == "failed"
    assert read_metadata(info)["error"] == "source disconnected"


def test_interrupted_recording_preserves_per_chunk_revisions(tmp_path):
    recorder = LocalSignalRecorder(tmp_path)
    info = recorder.start(metadata())
    recorder.append(chunk(), config_revision="old")
    recorder.append(chunk(2), config_revision="new")
    recorder.append(chunk(3))
    assert recorder.finish(status="interrupted")["status"] == "interrupted"
    assert read_metadata(info)["status"] == "interrupted"
    assert recorder.finish()["status"] == "interrupted"
    entries = [json.loads(line) for line in (Path(info["path"]) / "chunks.jsonl").read_text().splitlines()]
    assert [entry["config_revision"] for entry in entries] == ["old", "new", None]


def test_reader_preserves_legacy_v1_files_without_format_identifier(tmp_path):
    recorder = LocalSignalRecorder(tmp_path)
    info = recorder.start(metadata())
    original = chunk()
    recorder.append(original)
    recorder.finish()
    document = read_metadata(info)
    del document["format"]
    (Path(info["path"]) / "metadata.json").write_text(json.dumps(document))

    loaded, = iter_recorded_chunks(info["path"])
    np.testing.assert_array_equal(loaded.raw_data, original.raw_data)
    assert loaded.timestamp == original.timestamp
    assert loaded.sequence_id == original.sequence_id


@pytest.mark.parametrize("document", [
    {"format": "another-format", "format_version": 1},
    {"format": "omnibci.numpy-chunks", "format_version": 2},
    {"format_version": 2},
    {"format_version": True},
    {"format_version": "1"},
    {"format_version": 1.0},
    {"format": None, "format_version": 1},
    {},
    [],
])
def test_reader_rejects_unknown_format_or_version_before_opening_data(tmp_path, document):
    (tmp_path / "metadata.json").write_text(json.dumps(document))
    # No arrays or index exist: incompatible metadata must fail before decoding.
    with pytest.raises(ValueError, match="Unsupported recording|metadata must be an object"):
        list(iter_recorded_chunks(tmp_path))
