"""Replay an immutable recording into a distinct session through real acquisition."""

import asyncio
import json
from pathlib import Path
from queue import Queue
from types import SimpleNamespace

import numpy as np
import pytest

from omnibci.application.events import CommandRequest
from omnibci.domain.signal import SignalChunk
from omnibci.domain.signal_info import ChannelDescriptor, SignalInfo
from omnibci.infrastructure.acquisition.acquisition import AcquisitionCommand, AcquisitionRuntime
from omnibci.infrastructure.acquisition.recording import LocalSignalRecorder, iter_recorded_chunks
from omnibci.infrastructure.ipc.data_manager import DataManager
from omnibci.infrastructure.plugins.acquisition.recording_replay.reader import RecordingReplay
from omnibci.shared.events import EventType


@pytest.mark.asyncio
@pytest.mark.parametrize("dtype", [np.int16, np.float32])
async def test_replay_records_distinct_session_with_source_provenance(tmp_path, dtype):
    descriptor = SignalInfo("original-device", 1000, (
        ChannelDescriptor(0, "O2", signal_type="eeg", unit="uV", channel_id="right"),
        ChannelDescriptor(1, "O1", signal_type="eeg", unit="uV", channel_id="left"),
    ), 2, connection_id="original-connection")
    original = LocalSignalRecorder(tmp_path / "original")
    source_info = original.start({"session_id": "original-session", "subject_id": "subject-1",
                                  "signal_info": descriptor.to_dict()})
    arrays = [np.array([[1, 2], [3, 4], [7, 0]], dtype=dtype),
              np.array([[5, 6, 7], [8, 9, 10], [0, 255, 0]], dtype=dtype)]
    for sequence, array in enumerate(arrays, start=17):
        original.append(SignalChunk(1000 + sequence, sequence, array), config_revision="original-revision")
    original.finish()
    source_path = Path(source_info["path"])
    original_bytes = {name: (source_path / name).read_bytes()
                      for name in ("metadata.json", "chunks.npy", "chunks.jsonl")}

    replay = RecordingReplay(parameters={"recording_path": str(source_path / "metadata.json")})
    events = []
    sink = DataManager(Queue())
    runtime = AcquisitionRuntime(
        replay, Queue(), sink, SimpleNamespace(put_event=events.append),
        source_id=replay.plugin_type, preparation_required=False,
        generation="replay-generation", config_revision="replay-revision",
        recorder=LocalSignalRecorder(tmp_path / "replayed"),
    )

    async def command(operation, command_id, payload=None):
        await runtime.execute((operation, CommandRequest(command_id, payload)))
        result = next(event.data for event in reversed(events)
                      if event.type is EventType.CommandResult and event.data.command_id == command_id)
        assert result.ok, result.message
        return result.data

    try:
        await command(AcquisitionCommand.connect, "connect")
        observed = await command(AcquisitionCommand.get_metadata, "metadata")
        assert observed["descriptor"]["channels"] == descriptor.to_dict()["channels"]
        assert observed["descriptor"]["trigger_row_index"] == 2
        assert observed["descriptor"]["connection_id"] != "original-connection"
        provenance = replay.recording_provenance

        started = await command(AcquisitionCommand.begin_recording, "begin", {
            "session_id": "replay-session", "subject_id": "subject-1",
        })
        await command(AcquisitionCommand.start_stream, "stream")
        await asyncio.wait_for(asyncio.shield(runtime.fetch_task), timeout=2)
        assert not replay.is_streaming
        await command(AcquisitionCommand.stop_stream, "stop")
        completed = await command(AcquisitionCommand.end_recording, "end", {"session_id": "replay-session"})
        assert completed["status"] == "completed"
        assert completed["id"] == started["id"] != source_info["id"]
        assert completed["session_id"] == "replay-session" != source_info["session_id"]
        assert completed["source_recording"] == provenance
        assert completed["source_recording"]["recording_id"] == source_info["id"]
        assert completed["source_recording"]["session_id"] == "original-session"
        assert completed["source_recording"]["partial"] is False
        assert completed["sample_count"] == sum(array.shape[1] for array in arrays)

        replayed_path = Path(completed["path"])
        document = json.loads((replayed_path / "metadata.json").read_text())
        assert document["source_recording"] == provenance
        assert document["subject_id"] == "subject-1"
        assert document["session_id"] == "replay-session"
        assert document["signal_info"]["source_id"] == "recording_replay"
        assert document["signal_info"]["sample_rate"] == descriptor.sample_rate
        assert document["signal_info"]["channels"] == descriptor.to_dict()["channels"]
        stored = list(iter_recorded_chunks(replayed_path))
        assert len(stored) == len(arrays)
        for chunk, expected in zip(stored, arrays, strict=True):
            np.testing.assert_array_equal(chunk.raw_data, expected)
            np.testing.assert_array_equal(chunk.triggers, expected[-1])
            assert chunk.raw_data.dtype == expected.dtype
            np.testing.assert_array_equal(sink.get_data(timeout=0).raw_data, expected)
        assert sink.get_data(timeout=0) is None
        assert all((source_path / name).read_bytes() == contents for name, contents in original_bytes.items())
        terminal = next(event.data for event in reversed(events) if event.type is EventType.RawRecordingState)
        assert terminal["source_recording"] == provenance
        assert terminal["status"] == "completed"
    finally:
        await runtime.shutdown()
