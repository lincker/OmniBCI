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
from omnibci.infrastructure.neuroengine.processing.pipeline_configuration import load_filters
from omnibci.sdk import PluginContext


def _worker(**overrides) -> SignalProcessingWorker:
    defaults = {
        "command_queue": MagicMock(),
        "data_manager": MagicMock(),
        "event_bus": MagicMock(),
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
        assert load_filters(self.snapshot(0), PluginContext()) == []

    def test_unknown_filter_blocks_pipeline(self):
        from omnibci.infrastructure.plugins.plugin_catalog import PluginNotFoundError
        with pytest.raises(PluginNotFoundError):
            load_filters(self.snapshot(missing=True), PluginContext())

    def test_repeated_filters_construct_separate_instances_in_order(self):
        filters = load_filters(self.snapshot(2), PluginContext())
        assert len(filters) == 2
        assert filters[0] is not filters[1]
        assert all(item.plugin_type == "BASELINE_CORRECTION" for item in filters)


class TestAlgorithmLoading:
    def test_prepared_pipeline_uses_observed_sample_rate_without_global_reads(self, monkeypatch):
        from omnibci.application.plugin_profiles import ProfileBinding
        from omnibci.domain.channel_mapping import ChannelDescriptor, ProcessingInput, SignalInfo, compile_processing_plan
        from omnibci.infrastructure.neuroengine.processing.signal_configuration import load_signal_pipeline
        from omnibci.infrastructure.plugins.plugin_catalog import PluginCatalog
        snapshot = {"ACQUISITION.sample_rate": 1000, "CONFIG.revision": "test-revision"}
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
        source = SignalInfo("file_reader", 250,
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
    def test_constructs_standby_processor_and_runs_it(self, monkeypatch):
        processor = MagicMock()
        processor_type = MagicMock(return_value=processor)
        monkeypatch.setattr(worker_module, "SignalProcessor", processor_type)

        worker = _worker()
        worker._run_processor()

        processor_type.assert_called_once_with(
            decoder=None,
            detector=None,
            command_queue=worker.command_queue,
            data_manager=worker.data_manager,
            event_bus=worker.event_bus,
            session=None,
            baseline_duration=0.3,
            filters=[],
            auto_start_session=False,
            generation=None,
            config_revision=None,
            settings_snapshot=worker._snapshot(),
        )
        assert worker.signal_processor is processor
        processor.run.assert_called_once_with()

    def test_run_waits_for_source_information_before_constructing_plugins(self):
        worker = _worker(settings_snapshot={"ACQUISITION.sample_rate": 512})
        worker._run_processor = MagicMock()
        with patch.object(PluginLoader, "load_entrypoint") as load:
            worker.run()
        load.assert_not_called()
        worker._run_processor.assert_called_once_with()
