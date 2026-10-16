"""Tests for SignalProcessingWorker plugin loading and processor wiring."""
from __future__ import annotations

from queue import Queue
from unittest.mock import MagicMock, call, patch

import numpy as np
import pytest

import omnibci.conf as conf_module
import omnibci.infrastructure.neuroengine.signal_processing.worker as worker_module
from omnibci.domain.ports.algorithm import Decoder, EpochDetector, Filter, Prediction
from omnibci.domain.signal import SignalChunk
from omnibci.infrastructure.ipc.data_manager import DataManager
from omnibci.infrastructure.neuroengine.signal_processing.signal_processor import SignalProcessorCommand
from omnibci.infrastructure.neuroengine.signal_processing.worker import (
    SignalProcessingWorker,
)
from omnibci.infrastructure.plugins.algorithms.epoch.detector import DefaultEpochDetector
from omnibci.infrastructure.plugins.loader import PluginLoader
from omnibci.infrastructure.process_lifecycle import StartupReporter
from omnibci.shared.events import EventType


def _worker(**overrides) -> SignalProcessingWorker:
    defaults = {
        "command_queue": MagicMock(),
        "data_manager": MagicMock(),
        "event_bus": MagicMock(),
        "sample_rate": 250,
        "baseline_duration": 0.3,
    }
    defaults.update(overrides)
    return SignalProcessingWorker(**defaults)


class TestFilterLoading:
    @staticmethod
    def snapshot(count=1, *, missing=False):
        from dataclasses import replace
        from omnibci.application.plugin_profiles import ProfileBinding
        from omnibci.infrastructure.plugins.plugin_catalog import PluginCatalog
        from uuid import uuid4
        descriptor = PluginCatalog().resolve("filter", "BASELINE_CORRECTION")
        selection = replace(descriptor.selection, package_id="missing") if missing else descriptor.selection
        return {"PLUGINS.bindings": [ProfileBinding(
            f"processing.filter.{uuid4()}", "filter", None, selection, {},
        ).to_dict() for _ in range(count)]}

    def test_empty_configuration_returns_no_filters(self):
        assert _worker(settings_snapshot=self.snapshot(0))._load_filters() == []

    def test_unknown_filter_blocks_pipeline(self):
        from omnibci.infrastructure.plugins.plugin_catalog import PluginNotFoundError
        with pytest.raises(PluginNotFoundError):
            _worker(settings_snapshot=self.snapshot(missing=True))._load_filters()

    def test_repeated_filters_construct_separate_instances_in_order(self):
        filters = _worker(settings_snapshot=self.snapshot(2))._load_filters()
        assert len(filters) == 2
        assert filters[0] is not filters[1]
        assert all(item.plugin_type == "BASELINE_CORRECTION" for item in filters)


class TestAlgorithmLoading:
    def test_prepared_pipeline_uses_host_sample_rate_without_global_reads(self, monkeypatch):
        from omnibci.application.plugin_profiles import ProfileBinding
        from omnibci.domain.montage import ChannelDescriptor, ProcessingInput, SourceMetadata, compile_processing_plan
        from omnibci.infrastructure.neuroengine.signal_processing.signal_configuration import load_signal_pipeline
        from omnibci.infrastructure.plugins.plugin_catalog import PluginCatalog
        snapshot = {"ACQUISITION.sample_rate": 250, "CONFIG.revision": "test-revision"}
        bindings = []
        descriptors = []
        for slot, kind, alias, parameters in (
            ("processing.decoder", "decoder", "THUBCI_SSVEP_classic40CCA", {"window_duration": 1.5, "offset_time": 0.2}),
            ("processing.epoch_detector", "epoch_detector", "DEFAULT_EPOCH_DETECTOR", {}),
        ):
            descriptor = PluginCatalog().resolve(kind, alias)
            descriptors.append(descriptor)
            bindings.append(ProfileBinding(slot, kind, None, descriptor.selection, parameters,
                                           processing_input=ProcessingInput() if kind == "decoder" else None).to_dict())
        snapshot["PLUGINS.bindings"] = bindings
        source = SourceMetadata("file_reader", snapshot["ACQUISITION.sample_rate"],
                                (ChannelDescriptor(0, "O1", signal_type="eeg", unit="V"),), 1)
        plan = compile_processing_plan(source, ProcessingInput(), config_revision=snapshot["CONFIG.revision"])
        monkeypatch.setattr(conf_module.settings, "get", lambda *a, **k: pytest.fail("global settings read"))
        with patch.object(
            PluginLoader, "load_entrypoint", wraps=PluginLoader.load_entrypoint
        ) as load_entrypoint:
            decoder, detector, filters = load_signal_pipeline(snapshot, plan)
        assert load_entrypoint.call_args_list == [
            call(descriptors[0], Decoder),
            call(descriptors[1], EpochDetector),
        ]
        assert decoder.SAMPLE_COUNT == 375
        assert decoder.OFFSET_LENGTH == 50
        assert detector.plugin_type == "DEFAULT_EPOCH_DETECTOR"
        assert filters == []


class TestProcessorWiring:
    def test_invalid_prediction_after_ready_propagates_from_real_processor(self, monkeypatch):
        reporter = MagicMock(spec=StartupReporter)

        class InvalidTargetDecoder(Decoder):
            def should_decode(self, window):
                return False

            def decode(self, window, latest_samples):
                reporter.ready.assert_called_once_with()
                return Prediction(1.9)

        commands = Queue()
        commands.put((SignalProcessorCommand.start_session, {"session_id": "test-session"}))
        # If validation regresses, the loop exits instead of hanging the test.
        commands.put(SignalProcessorCommand.should_stop)
        data_manager = DataManager(buffer=Queue())
        data_manager.put_data(SignalChunk(1.0, 1, np.array([[1., 2., 3.], [7., 0., 9.]])))
        worker = _worker(
            command_queue=commands, data_manager=data_manager, startup_reporter=reporter,
            settings_snapshot={"CONFIG.revision": "test-revision", "CONFIG.generation": "test-run"},
        )
        monkeypatch.setattr(conf_module.settings, "use_snapshot", lambda snapshot: None)
        monkeypatch.setattr(worker, "_load_algorithm", lambda: (InvalidTargetDecoder(), DefaultEpochDetector(), []))

        with pytest.raises(TypeError) as raised:
            worker.run()

        message = str(raised.value)
        assert "InvalidTargetDecoder" in message
        assert "event_id=7" in message
        assert "test-revision" in message
        reporter.failed.assert_called_once_with("run", raised.value)
        reporter.close.assert_called_once_with()
        assert worker.signal_processor.session.trial_count == 0
        assert all(item.args[0].type is not EventType.TrialCompleted
                   for item in worker.event_bus.put_event.call_args_list)

    def test_constructs_standby_processor_and_runs_it(self, monkeypatch):
        processor = MagicMock()
        processor_type = MagicMock(return_value=processor)
        monkeypatch.setattr(worker_module, "SignalProcessor", processor_type)

        worker = _worker()
        decoder = MagicMock(spec=Decoder)
        detector = MagicMock(spec=EpochDetector)
        filters = [MagicMock(spec=Filter)]

        worker._run_processor(decoder, detector, filters, sample_rate=250)

        processor_type.assert_called_once_with(
            decoder=decoder,
            detector=detector,
            command_queue=worker.command_queue,
            data_manager=worker.data_manager,
            event_bus=worker.event_bus,
            session=None,
            sample_rate=250,
            baseline_duration=0.3,
            filters=filters,
            auto_start_session=False,
            generation=None,
            config_revision=None,
            settings_snapshot=worker._snapshot(),
        )
        assert worker.signal_processor is processor
        processor.run.assert_called_once_with()

    def test_run_passes_loaded_algorithm_to_processor(self):
        worker = _worker(sample_rate=512)
        decoder = MagicMock(spec=Decoder)
        detector = MagicMock(spec=EpochDetector)
        filters = [MagicMock(spec=Filter)]
        worker._load_algorithm = MagicMock(
            return_value=(decoder, detector, filters)
        )
        worker._run_processor = MagicMock()

        worker.run()

        worker._load_algorithm.assert_called_once_with()
        worker._run_processor.assert_called_once_with(
            decoder,
            detector,
            filters,
            512,
        )
