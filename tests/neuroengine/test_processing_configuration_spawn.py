"""Exercise trial-boundary replacement through real spawned-worker queues."""

from copy import deepcopy
import json
import multiprocessing as mp
import time

import numpy as np
import pytest

from omnibci.application.events import CommandRequest
from omnibci.application.plugin_profiles import ProfileBinding
from omnibci.domain.signal import SignalChunk
from omnibci.infrastructure.ipc.data_manager import DataManager
from omnibci.infrastructure.ipc.event_bus import EventBus
from omnibci.infrastructure.neuroengine.signal_processing.worker import SignalProcessingWorker
from omnibci.infrastructure.neuroengine.signal_processing.signal_processor import SignalProcessorCommand
from omnibci.infrastructure.plugins.plugin_catalog import PluginCatalog
from omnibci.infrastructure.process_lifecycle import StartupReporter, wait_for_startup_report
from omnibci.shared.events import EventType


def _wait_file(path):
    deadline = time.monotonic() + 10
    while not path.exists():
        if time.monotonic() >= deadline:
            pytest.fail(f"Worker did not reach {path.name}")
        time.sleep(0.01)


def _event(queue, kind, predicate=lambda event: True):
    deadline = time.monotonic() + 10
    while True:
        event = queue.get(timeout=max(0.01, deadline - time.monotonic()))
        if event.type is kind and predicate(event):
            return event.data


@pytest.mark.integration
def test_spawned_worker_preserves_old_trial_then_acknowledges_new_pipeline(tmp_path, monkeypatch):
    plugin = tmp_path / "algorithms" / "decoders" / "fixture"
    plugin.mkdir(parents=True)
    (plugin / "manifest.toml").write_text('''manifest_version = 2
[package]
id = "trial_fixture"
publisher = "Tests"
version = "1.0.0"
api_version = "2"
[[contributions]]
id = "default"
kind = "decoder"
entrypoint = "plugin:ValueDecoder"
[contributions.configuration]
schema = "schema.json"
schema_version = 1
''')
    (plugin / "schema.json").write_text(json.dumps({
        "type": "object", "additionalProperties": False,
        "properties": {"target": {"type": "integer", "minimum": 1, "x-apply": "trial"}},
        "required": ["target"],
    }))
    (plugin / "plugin.py").write_text('''from pathlib import Path
from omnibci.sdk import Decoder, Prediction
class ValueDecoder(Decoder):
    def __init__(self, *, parameters, context):
        self.target = parameters["target"]
        self.root = Path(context.resource_root)
        (self.root / f"constructed-{self.target}").write_text("ready")
    def should_decode(self, data):
        return False
    def decode(self, data, latest):
        return Prediction(self.target)
    def for_new_epoch(self):
        (self.root / f"epoch-{self.target}").write_text("ready")
        return self
''')
    monkeypatch.setenv("OMNIBCI_PLUGIN_DIR", str(tmp_path))
    catalog = PluginCatalog()
    decoder = next(item for item in catalog.list_contributions("decoder")
                   if item.package.identifier == "Tests.trial_fixture")
    detector = catalog.resolve_legacy("epoch_detector", "DEFAULT_EPOCH_DETECTOR")
    snapshot = {"CONFIG.revision": "old", "CONFIG.generation": "run",
                "ACQUISITION.sample_rate": 250, "ALGORITHM.baseline_duration": 0.0,
                "PLUGINS.bindings": [
                    ProfileBinding("processing.decoder", "decoder", None, decoder.selection,
                                   {"target": 1}).to_dict(),
                    ProfileBinding("processing.epoch_detector", "epoch_detector", None,
                                   detector.selection, {}).to_dict(),
                ]}
    replacement = deepcopy(snapshot)
    replacement["CONFIG.revision"] = "new"
    replacement["PLUGINS.bindings"][0]["parameters"]["target"] = 2
    context = mp.get_context("spawn")
    commands, data, events = (context.Queue() for _ in range(3))
    receiver, sender = context.Pipe(duplex=False)
    reporter = StartupReporter(sender, component="Signal Processing", config_revision="old", generation="run")
    worker = SignalProcessingWorker(commands, DataManager(data), EventBus(events), sample_rate=250,
                                    settings_snapshot=snapshot, startup_reporter=reporter)
    process = context.Process(target=worker.run)
    try:
        process.start()
        reporter.close()
        wait_for_startup_report(receiver, process, component="Signal Processing", timeout_s=10,
                                expected_revision="old", expected_generation="run")
        commands.put((SignalProcessorCommand.start_session, CommandRequest("start", {"session_id": "session"})))
        _event(events, EventType.CommandResult, lambda event: event.data.command_id == "start")
        data.put(SignalChunk(1.0, 1, np.array([[1., 1., 1.], [7., 0., 0.]])))
        _wait_file(plugin / "epoch-1")
        commands.put((SignalProcessorCommand.stage_configuration, CommandRequest("update", {
            "settings_snapshot": replacement, "expected_revision": "old", "generation": "run",
        })))
        _wait_file(plugin / "constructed-2")
        data.put(SignalChunk(2.0, 2, np.array([[1., 1., 1.], [0., 9., 0.]])))
        old_trial = _event(events, EventType.TrialCompleted)
        assert old_trial.config_revision == "old" and old_trial.classification.target_id == 1
        receipt = _event(events, EventType.CommandResult, lambda event: event.data.command_id == "update")
        assert receipt.ok and receipt.data["applied_revision"] == "new"
        data.put(SignalChunk(3.0, 3, np.array([[1., 1., 1.], [0., 11., 0.]])))
        new_trial = _event(events, EventType.TrialCompleted)
        assert new_trial.config_revision == "new" and new_trial.classification.target_id == 2
        commands.put(SignalProcessorCommand.should_stop)
        process.join(timeout=10)
        assert process.exitcode == 0
    finally:
        if process.is_alive():
            process.terminate()
            process.join(timeout=5)
        process.close()
        receiver.close()
        reporter.close()
        for queue in (commands, data, events):
            queue.close()
            queue.join_thread()
