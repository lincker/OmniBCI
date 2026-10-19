"""Resolved settings cross spawn boundaries before any plugin is loaded."""
import multiprocessing as mp
from unittest.mock import MagicMock, patch

import pytest

from omnibci.conf import OmniBCISettings
from omnibci.infrastructure.neuroengine.builder import NeuroEngineBuilder


def test_settings_snapshot_is_isolated_and_does_not_fall_back_to_disk(tmp_path):
    path = tmp_path / "settings.toml"
    path.write_text('[ACQUISITION]\nsample_rate = 1000\nchannel_number = 10\n')
    settings = OmniBCISettings(path)
    snapshot = {"ACQUISITION.sample_rate": 250, "ALGORITHM.filters": ["example"]}
    settings.use_snapshot(snapshot)
    snapshot["ACQUISITION.sample_rate"] = 500
    assert settings.get("ACQUISITION.sample_rate") == 250
    assert settings.get("ACQUISITION.channel_number", None) is None
    settings.get("ALGORITHM.filters").append("other")
    assert settings.get("ALGORITHM.filters") == ["example"]
    assert OmniBCISettings(path).get("ACQUISITION.sample_rate") == 1000


def test_builder_passes_one_resolved_configuration_to_both_workers():
    snapshot = {"ACQUISITION.sample_rate": 250, "ACQUISITION.DATA_PROVIDER_PORT": 4455}
    with patch("omnibci.infrastructure.neuroengine.builder.mp.Queue", return_value=MagicMock()):
        builder = NeuroEngineBuilder(settings_snapshot=snapshot)
        snapshot["ACQUISITION.sample_rate"] = 1000
        engine = builder.build()
    acquisition = engine.acquisition_worker
    processing = engine.signal_processing_worker
    assert acquisition.settings_snapshot == processing.settings_snapshot
    assert not hasattr(processing, "sample_rate")
    assert acquisition.settings_snapshot["ACQUISITION.sample_rate"] == 250


def _capture_worker_settings(worker_type, snapshot, output):
    from omnibci.conf import settings
    if worker_type == "acquisition":
        from omnibci.infrastructure.acquisition.worker import AcquisitionWorker
        worker = AcquisitionWorker(command_queue=None, settings_snapshot=snapshot)
        worker._load_data_source = lambda: output.put(settings.snapshot())
        worker._run = lambda: None
    else:
        from omnibci.infrastructure.signal_processing.worker import SignalProcessingWorker
        worker = SignalProcessingWorker(command_queue=None, settings_snapshot=snapshot)
        worker._run_processor = lambda: output.put(settings.snapshot())
    worker.run()


@pytest.mark.integration
def test_spawned_workers_install_snapshot_before_loading_plugins():
    snapshot = {"ACQUISITION.signal_source": "neuroscan_scan45_reader",
                "ACQUISITION.sample_rate": 250, "ACQUISITION.DATA_PROVIDER_PORT": 9876,
                "ALGORITHM.filters": ["BASELINE_CORRECTION"]}
    context = mp.get_context("spawn")
    output = context.Queue()
    processes = [context.Process(target=_capture_worker_settings, args=(kind, snapshot, output))
                 for kind in ("acquisition", "processing")]
    try:
        for process in processes:
            process.start()
        assert output.get(timeout=15) == snapshot
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
