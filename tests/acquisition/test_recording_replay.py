"""Raw recording replay preserves samples and resumes only undelivered chunks."""

import asyncio
import json
from pathlib import Path

import numpy as np
import pytest

from omnibci.domain.signal import SignalChunk
from omnibci.domain.signal_info import ChannelDescriptor, SignalInfo
from omnibci.infrastructure.acquisition.recording import LocalSignalRecorder, recover_recording
from omnibci.infrastructure.acquisition.recording_reader import open_recording_reader
from omnibci.infrastructure.plugins.acquisition.recording_replay import reader as module
from omnibci.infrastructure.plugins.acquisition.recording_replay.reader import RecordingReplay
from omnibci.infrastructure.storage_lock import StorageInUseError, StorageLease


def saved_recording(tmp_path, *, status="completed", dtype=np.int16):
    descriptor = SignalInfo("original-device", 250, (
        ChannelDescriptor(0, "O2", signal_type="eeg", unit="uV", channel_id="right"),
        ChannelDescriptor(1, "O1", signal_type="eeg", unit="uV", channel_id="left"),
    ), 2, connection_id="old-connection")
    writer = LocalSignalRecorder(tmp_path)
    info = writer.start({"session_id": "original-session", "subject_id": "subject",
                         "signal_info": descriptor.to_dict()})
    arrays = [np.array([[1, 2], [3, 4], [7, 0]], dtype=dtype),
              np.array([[5, 6, 7], [8, 9, 10], [0, 0, 255]], dtype=dtype)]
    for i, array in enumerate(arrays):
        writer.append(SignalChunk(1000 + i, i, array), config_revision=f"revision-{i}")
    writer.finish(status)
    return Path(info["path"]), arrays, descriptor


def replay(path, **parameters):
    return RecordingReplay(parameters={"recording_path": str(path), **parameters})


def metadata(path, **changes):
    value = json.loads((path / "metadata.json").read_text())
    value.update(changes)
    (path / "metadata.json").write_text(json.dumps(value))
    return value


@pytest.mark.asyncio
@pytest.mark.parametrize("file_path", [False, True])
@pytest.mark.parametrize("dtype", [np.int16, np.float32, np.float64])
async def test_round_trip_pacing_descriptors_and_provenance(tmp_path, monkeypatch, file_path, dtype):
    path, arrays, descriptor = saved_recording(tmp_path, dtype=dtype)
    delays = []

    async def pace(delay):
        delays.append(delay)

    monkeypatch.setattr(module.asyncio, "sleep", pace)
    source = replay(path / "metadata.json" if file_path else path)
    await source.connect()
    observed = source.describe_output(source_id="recording_replay", connection_id="new-connection")
    assert observed.channels == descriptor.channels
    assert observed.native_event == descriptor.native_event
    assert observed.sample_rate == descriptor.sample_rate
    assert observed.trigger_row_index == descriptor.trigger_row_index
    assert observed.connection_id == "new-connection"
    assert observed.source_id == "recording_replay"
    assert observed.provenance == "file-reported"
    provenance = source.recording_provenance
    assert provenance["recording_id"] == path.name
    assert provenance["session_id"] == "original-session"
    assert provenance["partial"] is False
    provenance["recording_id"] = "changed"
    assert source.recording_provenance["recording_id"] == path.name
    assert json.loads(await source.get_metadata())["source_recording"] == source.recording_provenance

    await source.start_stream()
    for expected in arrays:
        actual = await source.read_chunk()
        np.testing.assert_array_equal(actual, expected)
        assert actual.dtype == expected.dtype
    assert await source.read_chunk() is None
    assert source.is_streaming is False
    assert delays == [array.shape[1] / 250 for array in arrays]
    assert source.checkpoint()["next_chunk"] == 2
    await source.disconnect()


@pytest.mark.asyncio
async def test_pause_restore_and_reconnect_continue_without_duplicate_chunks(tmp_path):
    path, arrays, _ = saved_recording(tmp_path)
    source = replay(path)
    await source.connect()
    await source.start_stream()
    np.testing.assert_array_equal(await source.read_chunk(), arrays[0])
    await source.stop_stream()
    checkpoint = source.checkpoint()
    await source.disconnect()

    source = replay(path)
    await source.connect()
    source.restore_checkpoint(checkpoint)
    checkpoint["next_chunk"] = 0
    await source.start_stream()
    np.testing.assert_array_equal(await source.read_chunk(), arrays[1])
    assert await source.read_chunk() is None
    # An explicit fresh start replays from the beginning.
    await source.start_stream()
    np.testing.assert_array_equal(await source.read_chunk(), arrays[0])
    await source.stop_stream()
    await source.disconnect()


@pytest.mark.asyncio
async def test_cancel_during_pacing_does_not_skip_pending_chunk(tmp_path, monkeypatch):
    path, arrays, _ = saved_recording(tmp_path)
    source = replay(path)
    await source.connect()
    await source.start_stream()
    started = asyncio.Event()

    async def wait_for_cancel(delay):
        started.set()
        await asyncio.Future()

    with monkeypatch.context() as patch:
        patch.setattr(module.asyncio, "sleep", wait_for_cancel)
        task = asyncio.create_task(source.read_chunk())
        await started.wait()
        await source.stop_stream()
        with pytest.raises(RuntimeError, match="Stop and join"):
            source.checkpoint()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    checkpoint = source.checkpoint()
    assert checkpoint["next_chunk"] == 0
    source.restore_checkpoint(checkpoint)
    await source.start_stream()
    np.testing.assert_array_equal(await source.read_chunk(), arrays[0])
    await source.stop_stream()
    await source.disconnect()


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value", [("next_chunk", -1), ("next_chunk", 3), ("next_chunk", True),
                                         ("source", "different"), ("format_version", True)])
async def test_invalid_checkpoints_are_rejected(tmp_path, field, value):
    path, _, _ = saved_recording(tmp_path)
    source = replay(path)
    await source.connect()
    checkpoint = source.checkpoint()
    checkpoint[field] = value
    with pytest.raises(ValueError, match="Invalid checkpoint"):
        source.restore_checkpoint(checkpoint)
    await source.disconnect()


@pytest.mark.asyncio
@pytest.mark.parametrize("filename", ["metadata.json", "chunks.npy", "chunks.jsonl"])
async def test_changed_files_invalidate_checkpoint_and_current_stream(tmp_path, filename):
    path, _, _ = saved_recording(tmp_path)
    source = replay(path)
    await source.connect()
    checkpoint = source.checkpoint()
    target = path / filename
    target.write_bytes(target.read_bytes() + b" ")
    with pytest.raises(ValueError, match="changed"):
        source.restore_checkpoint(checkpoint)
    with pytest.raises(ValueError, match="changed"):
        await source.start_stream()
    await source.disconnect()


@pytest.mark.parametrize("status", ["recording", "interrupted", "failed"])
def test_default_rejects_incomplete_recording(tmp_path, status):
    path, _, _ = saved_recording(tmp_path)
    metadata(path, status=status)
    with pytest.raises(ValueError, match="completed recording"):
        open_recording_reader(path)


def test_abandoned_recording_requires_recovery_before_partial_replay(tmp_path):
    path, arrays, _ = saved_recording(tmp_path)
    metadata(path, status="recording")
    with pytest.raises(ValueError, match="completed recording"):
        open_recording_reader(path, allow_partial=True)
    recover_recording(path)
    reader = open_recording_reader(path, allow_partial=True)
    assert reader.provenance["partial"] is True
    np.testing.assert_array_equal(next(reader.chunks()).raw_data, arrays[0])
    reader.close()


@pytest.mark.parametrize("parameters", [{"recording_path": ""}, {"recording_path": None},
                                        {"recording_path": ".", "allow_partial": "yes"}])
def test_invalid_source_parameters_fail_before_opening_files(parameters):
    with pytest.raises(ValueError):
        RecordingReplay(parameters=parameters)


def test_data_array_path_is_not_accepted_as_recording_metadata(tmp_path):
    path, _, _ = saved_recording(tmp_path)
    with pytest.raises(ValueError, match="metadata.json"):
        open_recording_reader(path / "chunks.npy")


@pytest.mark.asyncio
async def test_explicit_partial_replay_uses_valid_prefix_and_leaves_original_files(tmp_path):
    path, arrays, _ = saved_recording(tmp_path, status="interrupted")
    with (path / "chunks.npy").open("ab") as stream:
        stream.write(b"unindexed tail")
    with (path / "chunks.jsonl").open("ab") as stream:
        stream.write(b'{"offset":')
    original = {name: (path / name).read_bytes() for name in ("chunks.npy", "chunks.jsonl", "metadata.json")}
    source = replay(path, allow_partial=True)
    await source.connect()
    assert source.recording_provenance["partial"] is True
    await source.start_stream()
    for array in arrays:
        np.testing.assert_array_equal(await source.read_chunk(), array)
    assert await source.read_chunk() is None
    await source.disconnect()
    assert all((path / name).read_bytes() == value for name, value in original.items())


def test_active_writer_is_rejected_even_with_partial_permission(tmp_path):
    path, _, _ = saved_recording(tmp_path)
    with StorageLease(path / ".writer.lock"):
        with pytest.raises(StorageInUseError):
            open_recording_reader(path, allow_partial=True)


def test_reader_holds_lease_against_recovery_until_closed(tmp_path):
    path, _, _ = saved_recording(tmp_path)
    reader = open_recording_reader(path)
    with pytest.raises(StorageInUseError):
        recover_recording(path)
    reader.close()
    assert recover_recording(path)["status"] == "completed"


@pytest.mark.parametrize("change", [{"format": "future.format"}, {"format_version": 2}, {"format_version": True}])
def test_unsupported_format_rejected_before_data_decode(tmp_path, change):
    path, _, _ = saved_recording(tmp_path)
    metadata(path, **change)
    (path / "chunks.npy").unlink()
    with pytest.raises(ValueError, match="Unsupported recording"):
        open_recording_reader(path)


@pytest.mark.parametrize("corruption", ["truncated_array", "wrong_count", "unindexed_tail", "bad_descriptor"])
def test_corrupt_completed_recording_cannot_replay_even_with_partial_permission(tmp_path, corruption):
    path, _, _ = saved_recording(tmp_path)
    if corruption == "truncated_array":
        target = path / "chunks.npy"
        target.write_bytes(target.read_bytes()[:-3])
    elif corruption == "unindexed_tail":
        with (path / "chunks.npy").open("ab") as stream:
            stream.write(b"tail")
    elif corruption == "wrong_count":
        metadata(path, sample_count=6)
    else:
        metadata(path, signal_info=None)
    with pytest.raises(ValueError):
        open_recording_reader(path, allow_partial=True)


def test_legacy_v1_without_format_identifier_can_replay(tmp_path):
    path, arrays, _ = saved_recording(tmp_path)
    document = json.loads((path / "metadata.json").read_text())
    document.pop("format")
    (path / "metadata.json").write_text(json.dumps(document))
    reader = open_recording_reader(path)
    np.testing.assert_array_equal(next(reader.chunks()).raw_data, arrays[0])
    reader.close()
