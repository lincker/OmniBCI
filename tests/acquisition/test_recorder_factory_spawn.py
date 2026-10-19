"""Recording factories cross spawn boundaries without opening storage in the parent."""

from functools import partial
import multiprocessing as mp
import os

import pytest

from omnibci.infrastructure.acquisition.acquisition import AcquisitionCommand
from omnibci.infrastructure.acquisition.recording import LocalSignalRecorder
from omnibci.infrastructure.acquisition.worker import AcquisitionWorker


class StoppedSource:
    is_connected = False
    is_streaming = False


def create_recorder(marker, root):
    with marker.open("x", encoding="utf-8") as stream:
        stream.write(str(os.getpid()))
    return LocalSignalRecorder(root)


@pytest.mark.integration
def test_spawn_constructs_recorder_only_inside_acquisition_child(tmp_path):
    context = mp.get_context("spawn")
    commands = context.Queue()
    marker = tmp_path / "recorder-pid"
    worker = AcquisitionWorker(
        commands, settings_snapshot={},
        recorder_factory=partial(create_recorder, marker, tmp_path / "recordings"),
    )
    worker.data_source = StoppedSource()
    commands.put(AcquisitionCommand.stop_acquisition)
    process = context.Process(target=worker._run)
    try:
        assert not marker.exists()
        process.start()
        process.join(timeout=10)
        assert process.exitcode == 0
        assert int(marker.read_text(encoding="utf-8")) == process.pid
        assert process.pid != os.getpid()
    finally:
        if process.is_alive():
            process.terminate()
            process.join(timeout=5)
        process.close()
        commands.close()
        commands.join_thread()
