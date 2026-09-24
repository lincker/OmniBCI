"""Tests for SignalProcessingWorker plugin loading and processor wiring."""
from __future__ import annotations

from unittest.mock import MagicMock, call, patch

import pytest

import omnibci.conf as conf_module
import omnibci.infrastructure.neuroengine.processing.worker as worker_module
from omnibci.domain.ports.algorithm import Decoder, EpochDetector, Filter
from omnibci.infrastructure.neuroengine.processing.worker import (
    SignalProcessingWorker,
)
from omnibci.infrastructure.plugins.loader import PluginLoader


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
    def test_empty_configuration_returns_no_filters(self):
        assert _worker(settings_snapshot={"ALGORITHM.filters": []})._load_filters() == []

    def test_unknown_filter_blocks_pipeline(self):
        from omnibci.infrastructure.plugins.plugin_catalog import PluginNotFoundError
        with pytest.raises(PluginNotFoundError):
            _worker(settings_snapshot={"ALGORITHM.filters": ["MISSING"]})._load_filters()

    def test_repeated_filters_construct_separate_instances_in_order(self):
        filters = _worker(settings_snapshot={"ALGORITHM.filters": ["BASELINE_CORRECTION"] * 2})._load_filters()
        assert len(filters) == 2
        assert filters[0] is not filters[1]
        assert all(item.plugin_type == "BASELINE_CORRECTION" for item in filters)


class TestAlgorithmLoading:
    def test_explicit_pipeline_uses_host_sample_rate_without_global_reads(self, monkeypatch):
        from omnibci.application.plugin_profiles import ProfileBinding
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
            bindings.append(ProfileBinding(slot, kind, None, descriptor.selection, parameters).to_dict())
        snapshot["PLUGINS.bindings"] = bindings
        monkeypatch.setattr(conf_module.settings, "get", lambda *a, **k: pytest.fail("global settings read"))
        with patch.object(
            PluginLoader, "load_entrypoint", wraps=PluginLoader.load_entrypoint
        ) as load_entrypoint:
            decoder, detector, filters = _worker(settings_snapshot=snapshot)._load_algorithm()
        assert load_entrypoint.call_args_list == [
            call(descriptors[0], Decoder),
            call(descriptors[1], EpochDetector),
        ]
        assert decoder.SAMPLE_COUNT == 375
        assert decoder.OFFSET_LENGTH == 50
        assert detector.plugin_type == "DEFAULT_EPOCH_DETECTOR"
        assert filters == []


class TestProcessorWiring:
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
