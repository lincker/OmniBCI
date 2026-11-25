"""Tests for SignalProcessingWorker plugin loading and processor wiring."""
from __future__ import annotations
from pathlib import Path
from tests.plugin_configuration_helpers import builtin_selection, prepared_snapshot

from queue import Queue
from traceback import format_exception
from unittest.mock import MagicMock, call, patch

import numpy as np
from tests.processing_helpers import prepared_signal

import pytest

import omnibci.infrastructure.signal_processing.worker as worker_module
from omnibci.application.events import SignalPreparationReceipt, CommandRequest, ProcessingInputStart
from omnibci.application.plugin_management.identity import PluginSelection
from omnibci.application.configuration.models import ProfileBinding
from omnibci.domain.channel_mapping import ProcessingInput, compile_processing_plan
from omnibci.domain.ports.algorithm import Decoder, EpochDetector, Filter, Prediction
from omnibci.domain.signal import SignalChunk
from omnibci.domain.signal_info import SignalInfo, ChannelDescriptor
from omnibci.infrastructure.ipc.data_manager import DataManager
from omnibci.infrastructure.signal_processing import signal_configuration
from omnibci.infrastructure.signal_processing.signal_processor import SignalProcessorCommand
from omnibci.infrastructure.signal_processing.worker import (
    SignalProcessingWorker,
)
from omnibci.infrastructure.plugins.algorithms.epoch.detector import DefaultEpochDetector
from omnibci.infrastructure.plugin_management.loader import PluginLoader, PluginLoadError
from omnibci.infrastructure.signal_processing.pipeline_configuration import load_filters
from omnibci.infrastructure.process_lifecycle import StartupReporter
from omnibci.shared.events import EventType


def _worker(**overrides) -> SignalProcessingWorker:
    defaults = {
        "command_queue": MagicMock(),
        "data_manager": MagicMock(),
        "event_bus": MagicMock(),
        "baseline_duration": 0.3,
        "settings_snapshot": {},
    }
    defaults.update(overrides)
    return SignalProcessingWorker(**defaults)


class TestFilterLoading:
    @staticmethod
    def snapshot(count=1, *, missing=False):
        from dataclasses import replace
        from omnibci.application.configuration.models import ProfileBinding
        from omnibci.infrastructure.plugin_management.catalog import PluginCatalog
        from uuid import uuid4
        descriptor = PluginCatalog().resolve_selection("filter", builtin_selection("filter", "BASELINE_CORRECTION"))
        selection = replace(descriptor.selection, package_id="missing") if missing else descriptor.selection
        return prepared_snapshot({"PLUGINS.bindings": [ProfileBinding(
            f"processing.filter.{uuid4()}", "filter", None, selection, {},
        ).to_dict() for _ in range(count)]})

    def test_empty_configuration_returns_no_filters(self):
        assert load_filters(self.snapshot(0), prepared_signal(250)) == []

    def test_unknown_filter_blocks_pipeline(self):
        with pytest.raises(PluginLoadError, match="Unknown"):
            load_filters(self.snapshot(missing=True), prepared_signal(250))

    def test_repeated_filters_construct_separate_instances_in_order(self):
        filters = load_filters(self.snapshot(2), prepared_signal(250))
        assert len(filters) == 2
        assert filters[0] is not filters[1]
        assert all(type(item).__name__ == "BaselineCorrection" for item in filters)


class TestAlgorithmLoading:
    def test_prepared_pipeline_uses_observed_sample_rate_without_global_reads(self):
        from omnibci.application.configuration.models import ProfileBinding
        from omnibci.domain.channel_mapping import ChannelDescriptor, ProcessingInput, SignalInfo, compile_processing_plan
        from omnibci.infrastructure.signal_processing.signal_configuration import load_signal_pipeline
        from omnibci.infrastructure.plugin_management.catalog import PluginCatalog
        snapshot = {"ACQUISITION.sample_rate": 1000, "CONFIG.module_revisions": {"acquisition": "test-revision", "processing": "test-revision", "stimulation": "test-revision", "actuation": "test-revision"}, "CONFIG.engine_revision": "engine"}
        bindings = []
        descriptors = []
        for slot, kind, alias, parameters in (
            ("processing.decoder", "decoder", "THUBCI_SSVEP_classic40CCA", {"window_duration": 1.5, "offset_time": 0.2}),
            ("processing.epoch_detector", "epoch_detector", "DEFAULT_EPOCH_DETECTOR", {}),
        ):
            descriptor = PluginCatalog().resolve_selection(kind, builtin_selection(kind, alias))
            descriptors.append(descriptor)
            bindings.append(ProfileBinding(slot, kind, None, descriptor.selection, parameters,
                                           processing_input=ProcessingInput() if kind == "decoder" else None).to_dict())
        snapshot["PLUGINS.bindings"] = bindings
        snapshot = prepared_snapshot(snapshot)
        source = SignalInfo(sample_rate=250, channels=(ChannelDescriptor(0, "O1", signal_type="eeg", unit="V", channel_id="channel-0"),))
        plan = compile_processing_plan(source, ProcessingInput(), acquisition_revision=snapshot["CONFIG.module_revisions"]["processing"])
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
        assert type(detector).__name__ == "DefaultEpochDetector"
        assert filters == []


class TestProcessorWiring:
    @pytest.mark.parametrize("bindings", [
        [],
        [{"slot": "processing.decoder", "processing_input": {"schema_version": 1, "outputs": ["C3"]}}],
        [{"slot": "processing.decoder", "processing_input": {"schema_version": 2, "outputs": "invalid"}}],
    ])
    def test_standby_readiness_does_not_require_decoder_configuration(self, bindings):
        reporter = MagicMock(spec=StartupReporter)
        commands = Queue()
        commands.put((SignalProcessorCommand.should_stop, None))
        worker = _worker(command_queue=commands, startup_reporter=reporter,
                         settings_snapshot={"PLUGINS.bindings": bindings})
        with patch.object(PluginLoader, "load_entrypoint") as load:
            worker.run()
        reporter.ready.assert_called_once_with()
        reporter.failed.assert_not_called()
        load.assert_not_called()
        assert worker.signal_processor._signal_receipt is None

    def test_invalid_prediction_after_ready_propagates_from_real_processor(self, monkeypatch):
        reporter = MagicMock(spec=StartupReporter)

        class InvalidTargetDecoder(Decoder):
            def setup(self):
                pass

            def begin_epoch(self):
                pass

            def teardown(self):
                pass

            def should_decode(self, window):
                return False

            def decode(self, window, *, latest_raw_segment):
                reporter.ready.assert_called_once_with()
                return Prediction(1.9)

        source = SignalInfo(sample_rate=250, channels=(ChannelDescriptor(0, "O1", channel_id="channel-0"),))
        inputs = ProcessingInput()
        plan = compile_processing_plan(source, inputs, acquisition_revision="test-revision")
        receipt = {
            "generation": "test-run", "acquisition_revision": "test-revision", "processing_revision": "test-revision", "engine_revision": "engine",
            "connection_id": "test-connection",
            "source_fingerprint": "f" * 64, "plan_fingerprint": plan.fingerprint, "input_id": "test-input",
        }
        snapshot = {
            "CONFIG.module_revisions": {"acquisition": "test-revision", "processing": "test-revision", "stimulation": "test-revision", "actuation": "test-revision"}, "CONFIG.engine_revision": "engine", "CONFIG.generation": "test-run",
            "ACQUISITION.signal_source": "test-source",
            "PLUGINS.bindings": [ProfileBinding(
                "processing.decoder", "decoder", None,
                PluginSelection("test.decoder", "default", "1"), {}, processing_input=inputs,
            ).to_dict()],
        }
        commands = Queue()
        commands.put((SignalProcessorCommand.prepare_signal, CommandRequest("prepare", {
            "plan": plan.to_dict(), "receipt": receipt,
        })))
        # If validation regresses, the loop exits instead of hanging the test.
        data_manager = DataManager(buffer=Queue())
        worker = _worker(
            command_queue=commands, data_manager=data_manager, startup_reporter=reporter,
            settings_snapshot=snapshot,
        )
        def feed_after_preparation(event):
            if event.type is EventType.CommandResult:
                assert event.data.ok, event.data.message
                if event.data.command_id == "prepare":
                    data_manager.put_data(ProcessingInputStart("attach", SignalPreparationReceipt.from_value(receipt), 0))
                elif event.data.command_id == "attach":
                    data_manager.put_data(SignalChunk(1, 1, np.array([[1., 2., 3.], [7., 0., 9.]])))

        worker.event_bus.put_event.side_effect = feed_after_preparation
        next_data = data_manager.get_data
        def stop_after_sample(*args, **kwargs):
            item = next_data(*args, **kwargs)
            if isinstance(item, SignalChunk):
                commands.put((SignalProcessorCommand.should_stop, None))
            return item
        data_manager.get_data = stop_after_sample
        monkeypatch.setattr(signal_configuration, "load_signal_pipeline", lambda snapshot, plan: (
            InvalidTargetDecoder(), DefaultEpochDetector(parameters={}, input_signal=prepared_signal(250), resource_root=Path(__file__).parent), [],
        ))

        with pytest.raises(TypeError) as raised:
            worker.run()

        message = "".join(format_exception(raised.value))
        assert "InvalidTargetDecoder" in message
        assert "event_id=7" in message
        assert "test-revision" in message
        reporter.failed.assert_called_once_with("run", raised.value)
        reporter.close.assert_called_once_with()
        assert worker.signal_processor._signal_receipt.to_dict() == receipt
        assert worker.signal_processor.completed_trials == 0
        assert all(item.args[0].type is not EventType.TrialCompleted
                   for item in worker.event_bus.put_event.call_args_list)

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
            baseline_duration=0.3,
            filters=[],
            generation=None,
            processing_revision=None, acquisition_revision=None, engine_revision=None,
            settings_snapshot=worker.settings_snapshot, runtime_counters=None, record_execution=None,
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


import asyncio
import queue
import time
from unittest.mock import AsyncMock
from tests.processing_helpers import make_processor


class TestDataManagerTimeout:
    def test_returns_item_immediately_when_present(self):
        dm = DataManager(buffer=queue.Queue())
        dm.put_data("chunk")
        assert dm.get_data(timeout=0.5) == "chunk"

    def test_returns_none_after_timeout_when_empty(self):
        dm = DataManager(buffer=queue.Queue())
        t0 = time.perf_counter()
        assert dm.get_data(timeout=0.05) is None
        assert time.perf_counter() - t0 >= 0.04


class TestNextChunkEventDriven:
    def test_chunk_delivered_promptly(self):
        """next_chunk returns immediately when data arrives without polling delay."""
        dm = DataManager(buffer=queue.Queue())
        sp = make_processor(detector=MagicMock(), decoder=MagicMock(),
                             command_queue=MagicMock(), data_manager=dm,
                             event_bus=MagicMock())

        async def scenario():
            async def feeder():
                await asyncio.sleep(0.02)
                dm.put_data("the-chunk")
            asyncio.get_running_loop().create_task(feeder())
            t0 = time.perf_counter()
            chunk = await sp.next_chunk(timeout=1.0)
            return chunk, time.perf_counter() - t0

        chunk, elapsed = asyncio.run(scenario())
        assert chunk == "the-chunk"
        assert elapsed < 0.2

    def test_returns_none_when_no_data_within_window(self):
        dm = DataManager(buffer=queue.Queue())
        sp = make_processor(detector=MagicMock(), decoder=MagicMock(),
                             command_queue=MagicMock(), data_manager=dm,
                             event_bus=MagicMock())
        assert asyncio.run(sp.next_chunk(timeout=0.05)) is None


def test_stop_command_does_not_depend_on_queue_empty_probe():
    command_queue = queue.Queue()
    command_queue.put_nowait((SignalProcessorCommand.should_stop, None))
    command_queue.empty = MagicMock(return_value=True)
    processor = make_processor(
        detector=MagicMock(), decoder=MagicMock(), command_queue=command_queue,
        data_manager=MagicMock(), event_bus=MagicMock(),
    )

    async def stop_after_data_read():
        processor._should_stop = True

    processor._current_state.run = AsyncMock(side_effect=stop_after_data_read)

    asyncio.run(processor.serve())

    processor._current_state.run.assert_not_awaited()
    command_queue.empty.assert_not_called()
