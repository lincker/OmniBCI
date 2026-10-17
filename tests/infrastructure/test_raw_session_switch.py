"""Live session switching through real acquisition, processing, and app services."""

import asyncio
import json
from pathlib import Path
import queue
from types import SimpleNamespace

import numpy as np
import pytest

from tests.processing_helpers import make_processor
from omnibci.application.commands.acquisition_commands import StartStreamCommand
from omnibci.application.commands.session_commands import CreateSessionCommand, EndSessionCommand
from omnibci.application.services.acquisition_service import AcquisitionAppService
from omnibci.application.services.command_result_service import CommandResultBroker
from omnibci.application.services.session_service import SessionAppService
from omnibci.domain.ports.algorithm import Decoder, EpochDetector, Prediction
from omnibci.domain.repositories import SessionRecordState
from omnibci.domain.signal_info import ChannelDescriptor, SignalInfo
from omnibci.infrastructure.acquisition.acquisition import AcquisitionCommand, AcquisitionRuntime
from omnibci.infrastructure.acquisition.recording import LocalSignalRecorder, iter_recorded_chunks
from omnibci.infrastructure.ipc.data_manager import DataManager
from omnibci.infrastructure.ipc.event_bus import EventBus
from omnibci.infrastructure.repositories.session_repository import InMemorySessionRepository
from omnibci.infrastructure.signal_processing.signal_processor import SignalProcessorCommand
from omnibci.shared.events import EventType


class ControlledSource:
    is_connected = True
    is_streaming = False

    def __init__(self):
        self.chunks = asyncio.Queue()

    async def get_metadata(self):
        return "controlled signal source"

    def describe_output(self, *, source_id, connection_id):
        return SignalInfo(source_id, 250, (ChannelDescriptor(0, "Cz"),), 1,
                          connection_id=connection_id)

    async def start_stream(self):
        self.is_streaming = True

    async def stop_stream(self):
        self.is_streaming = False

    async def disconnect(self):
        self.is_connected = False

    async def read_chunk(self):
        return await self.chunks.get()


class Detector(EpochDetector):
    def __init__(self):
        self.processed = [asyncio.Event() for _ in range(3)]

    def detect_start(self, chunk):
        self.processed[chunk.sequence_id].set()
        starts = np.flatnonzero(chunk.triggers == 1)
        return (int(starts[0]), 1) if len(starts) else (-1, 0)

    def detect_end(self, chunk):
        return -1, 0


class FourSampleDecoder(Decoder):
    def __init__(self):
        self.windows = []

    def should_decode(self, data):
        return data.shape[1] >= 4

    def decode(self, data, latest_samples):
        self.windows.append(data.copy())
        return Prediction(2)


@pytest.mark.asyncio
async def test_live_switch_drains_old_epoch_splits_raw_files_and_resumes_stream(tmp_path):
    bus = EventBus(queue.Queue())
    data = DataManager(queue.Queue())
    acquisition_commands, processor_commands = queue.Queue(), queue.Queue()
    source, detector, decoder = ControlledSource(), Detector(), FourSampleDecoder()
    acquisition = AcquisitionRuntime(
        source, acquisition_commands, data, bus, source_id="controlled-source",
        preparation_required=False, generation="generation", config_revision="revision",
        recorder=LocalSignalRecorder(tmp_path),
    )
    processor = make_processor(
        detector, decoder, processor_commands, data, bus, auto_start_session=False,
        sample_rate=250, baseline_duration=0.01, generation="generation", config_revision="revision",
    )
    routes = {
        EventType.GetSourceMetadata: (acquisition_commands, AcquisitionCommand.get_metadata),
        EventType.StartStream: (acquisition_commands, AcquisitionCommand.start_stream),
        EventType.StopAndDrain: (acquisition_commands, AcquisitionCommand.stop_and_drain),
        EventType.BeginRawRecording: (acquisition_commands, AcquisitionCommand.begin_recording),
        EventType.EndRawRecording: (acquisition_commands, AcquisitionCommand.end_recording),
        EventType.StartSession: (processor_commands, SignalProcessorCommand.start_session),
        EventType.EndSession: (processor_commands, SignalProcessorCommand.end_session),
    }

    def dispatch(event):
        target, command = routes[event.type]
        target.put((command, event.data))

    engine = SimpleNamespace(dispatch=dispatch, is_engine_ready=True,
                             applied_snapshot={"CONFIG.revision": "revision"})
    broker = CommandResultBroker(engine, timeout_s=3)
    repository = InMemorySessionRepository()
    sessions = SessionAppService(repository, engine, broker)
    acquisition_service = AcquisitionAppService(broker)
    sessions.set_recording_controller(acquisition_service)
    acquisition_service.set_recording_validator(sessions.validate_recording_before_stream)
    results = []
    for event_type, handler in (
        (EventType.CommandResult, broker.on_result),
        (EventType.CommandResult, lambda event: results.append(event.data)),
        (EventType.AcquisitionState, acquisition_service.on_acquisition_state),
        (EventType.RawRecordingState, sessions.on_raw_recording_state),
        (EventType.SessionStarted, sessions.on_session_started),
        (EventType.SessionEnded, sessions.on_session_ended),
        (EventType.TrialCompleted, sessions.on_trial_completed),
    ):
        bus.register_event_listener(event_type, handler)
    await bus.start()
    tasks = [asyncio.create_task(acquisition.serve()), asyncio.create_task(processor.serve())]
    try:
        await asyncio.to_thread(acquisition_service.get_metadata)
        old_id = await asyncio.to_thread(sessions.handle_create_session,
                                        CreateSessionCommand(subject_id="subject-old"))
        await asyncio.to_thread(acquisition_service.handle_start_stream, StartStreamCommand())
        old_raw = np.array([[11., 11.], [1., 0.]], dtype=np.float32)
        source.chunks.put_nowait(old_raw)
        await asyncio.wait_for(detector.processed[0].wait(), timeout=2)
        assert processor.current_epoch is not None
        assert processor.session_runtime.session.trial_count == 0
        assert processor.pretrigger._buf is not None

        new_id = await asyncio.to_thread(sessions.handle_create_session,
                                        CreateSessionCommand(subject_id="subject-new"))
        assert new_id != old_id
        assert acquisition_service.is_streaming and source.is_streaming
        assert processor.current_epoch is None
        assert processor.pretrigger._buf is None
        old_drain = next(result for result in results if result.command == "StopAndDrain"
                         and result.ok and result.data["session_id"] == old_id)
        assert old_drain.data["interrupted_epoch"] is True
        assert old_drain.data["completed_trials"] == 0
        old_record = repository.find_by_id(old_id)
        assert old_record.state is SessionRecordState.ENDED
        assert old_record.recording["status"] == "completed"

        # A new-session tail without a trigger must not extend the old epoch.
        tail = np.array([[22., 22.], [0., 0.]], dtype=np.float32)
        source.chunks.put_nowait(tail)
        await asyncio.wait_for(detector.processed[1].wait(), timeout=2)
        assert processor.current_epoch is None
        assert decoder.windows == []
        new_raw = np.array([[22., 22., 22., 22.], [1., 0., 0., 0.]], dtype=np.float32)
        source.chunks.put_nowait(new_raw)
        await asyncio.wait_for(detector.processed[2].wait(), timeout=2)
        await asyncio.to_thread(sessions.handle_end_session, EndSessionCommand(new_id))
        assert not acquisition_service.is_streaming
        assert sessions.active_session_id is None
        new_record = repository.find_by_id(new_id)
        assert new_record.state is SessionRecordState.ENDED
        assert new_record.recording["status"] == "completed"
        assert len(new_record.trials) == 1
        assert len(decoder.windows) == 1
        np.testing.assert_array_equal(decoder.windows[0], new_raw[:-1])

        old_path, new_path = Path(old_record.recording["path"]), Path(new_record.recording["path"])
        assert old_path != new_path
        old_chunks, new_chunks = list(iter_recorded_chunks(old_path)), list(iter_recorded_chunks(new_path))
        assert [item.sequence_id for item in old_chunks] == [0]
        assert [item.sequence_id for item in new_chunks] == [1, 2]
        np.testing.assert_array_equal(old_chunks[0].raw_data, old_raw)
        np.testing.assert_array_equal(new_chunks[0].raw_data, tail)
        np.testing.assert_array_equal(new_chunks[1].raw_data, new_raw)
        for record, subject, expected_samples in (
            (old_record, "subject-old", 2), (new_record, "subject-new", 6),
        ):
            document = json.loads((Path(record.recording["path"]) / "metadata.json").read_text())
            assert record.subject_id == document["subject_id"] == subject
            assert document["session_id"] == record.session_id
            assert document["status"] == "completed"
            assert document["sample_count"] == expected_samples
    finally:
        acquisition.should_exit = True
        processor._should_stop = True
        await asyncio.wait_for(asyncio.gather(*tasks), timeout=2)
        await bus.stop()
