"""Runtime processes use detached inputs without accessing configuration storage."""
import builtins
import multiprocessing as mp
from unittest.mock import MagicMock, patch

import pytest


@pytest.mark.parametrize("worker_type", ["acquisition", "processing"])
def test_worker_configuration_is_detached_and_required(worker_type):
    if worker_type == "acquisition":
        from omnibci.infrastructure.acquisition.worker import AcquisitionWorker as Worker
    else:
        from omnibci.infrastructure.signal_processing.worker import SignalProcessingWorker as Worker
    snapshot = {"PLUGINS.bindings": [{"parameters": {"sample_rate": 250}}]}
    worker = Worker(command_queue=None, settings_snapshot=snapshot)
    snapshot["PLUGINS.bindings"][0]["parameters"]["sample_rate"] = 500
    assert worker.settings_snapshot["PLUGINS.bindings"][0]["parameters"]["sample_rate"] == 250
    with pytest.raises(TypeError, match="settings_snapshot"):
        Worker(command_queue=None)


def test_builder_passes_shared_source_context_with_owned_module_revisions():
    from omnibci.infrastructure.neuroengine.builder import NeuroEngineBuilder

    snapshot = {"ACQUISITION.sample_rate": 250, "ACQUISITION.DATA_PROVIDER_PORT": 4455}
    with patch("omnibci.infrastructure.neuroengine.builder.mp.Queue", return_value=MagicMock()):
        builder = NeuroEngineBuilder(settings_snapshot=snapshot)
        snapshot["ACQUISITION.sample_rate"] = 1000
        engine = builder.build()
    channels = [engine._worker_factory(module, builder._settings_snapshot)
                for module in ("acquisition", "processing")]
    acquisition, processing = [channel.process for channel in channels]
    for channel in channels:
        channel.receiver.close()
        channel.reporter.close()
    assert set(acquisition.settings_snapshot["CONFIG.module_revisions"]) == {"acquisition"}
    assert set(processing.settings_snapshot["CONFIG.module_revisions"]) == {"acquisition", "processing"}
    assert processing.settings_snapshot["ACQUISITION.sample_rate"] == 250
    assert not hasattr(processing, "sample_rate")
    assert acquisition.settings_snapshot["ACQUISITION.sample_rate"] == 250
    assert processing.baseline_duration == 0.0


def _capture_worker_settings(worker_type, snapshot, output):
    original_import = builtins.__import__

    def import_runtime(name, *args, **kwargs):
        if name == "omnibci.conf" or name.startswith("omnibci.conf.") or name == (
            "omnibci.infrastructure.repositories.configuration_repository"
        ):
            raise AssertionError("Runtime worker must not load configuration storage")
        return original_import(name, *args, **kwargs)

    builtins.__import__ = import_runtime
    try:
        if worker_type == "acquisition":
            from omnibci.infrastructure.acquisition.worker import AcquisitionWorker
            worker = AcquisitionWorker(command_queue=None, settings_snapshot=snapshot)
            worker._load_data_source = lambda: output.put(worker.settings_snapshot)
            worker._run = lambda: None
        elif worker_type == "processing":
            from omnibci.infrastructure.signal_processing.worker import SignalProcessingWorker
            worker = SignalProcessingWorker(command_queue=None, settings_snapshot=snapshot)
            worker._run_processor = lambda: output.put(worker.settings_snapshot)
        else:
            if worker_type == "stimulation":
                from omnibci.infrastructure.stimulation.application import Stimulation as Application
            else:
                from omnibci.infrastructure.actuation.application import Actuation as Application
            app = Application(settings_snapshot=snapshot)
            try:
                output.put(app._runtime_snapshot)
            finally:
                app._close_event_queue()
            return
        worker.run()
    finally:
        builtins.__import__ = original_import


@pytest.mark.integration
def test_spawned_workers_receive_snapshot_without_loading_configuration_storage():
    snapshot = {"ACQUISITION.signal_source": "neuroscan_scan45_reader",
                "ACQUISITION.sample_rate": 250, "ACQUISITION.DATA_PROVIDER_PORT": 9876,
                "ALGORITHM.filters": ["BASELINE_CORRECTION"]}
    context = mp.get_context("spawn")
    output = context.Queue()
    processes = [context.Process(target=_capture_worker_settings, args=(kind, snapshot, output))
                 for kind in ("acquisition", "processing", "stimulation", "actuation")]
    try:
        for process in processes:
            process.start()
        for _ in processes:
            assert output.get(timeout=15) == snapshot
        for process in processes:
            process.join(timeout=15)
            assert process.exitcode == 0
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join(timeout=5)
            process.close()
        output.close()
        output.join_thread()
