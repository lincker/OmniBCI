"""
Unit tests for NeuroEngineBuilder and NeuroEngineConfig.

The builder assembles real component types (checked via isinstance) but
no processes are started — we only verify the wiring is correct.
"""
from __future__ import annotations

import multiprocessing as mp
import asyncio
from unittest.mock import patch, MagicMock

import pytest

from omnibci.application.configuration.models import ProfileBinding
from omnibci.application.plugin_management.identity import PluginSelection
from tests.execution_helpers import synthetic_execution_snapshot

from omnibci.infrastructure.neuroengine.builder import NeuroEngineBuilder, NeuroEngineConfig
from omnibci.infrastructure.neuroengine.engine import NeuroEngine
from omnibci.infrastructure.ipc.event_bus import EventBus
from omnibci.infrastructure.ipc.data_manager import DataManager
from omnibci.infrastructure.acquisition.worker import AcquisitionWorker
from omnibci.infrastructure.signal_processing.worker import SignalProcessingWorker
from omnibci.infrastructure.neuroengine.gateway import PeerGateway


def _source_snapshot():
    return synthetic_execution_snapshot({
        "CONFIG.module_revisions": {"acquisition": "a" * 64, "processing": "b" * 64},
        "CONFIG.engine_revision": "c" * 64, "CONFIG.generation": "run-1",
        "PLUGINS.bindings": [ProfileBinding("acquisition.source", "acquisition", None,
            PluginSelection("test.source", "default", "1.0.0"), {}).to_dict()],
    })


# Patch heavy imports so the builder test doesn't need config files / plugins
@pytest.fixture()
def patched_builder():
    """
    Return a builder whose sub-process constructors are patched so no
    real child processes or sockets are created during the test.
    """
    with (
        patch(
            "omnibci.infrastructure.neuroengine.builder.AcquisitionWorker",
            return_value=MagicMock(spec=AcquisitionWorker),
        ),
        patch(
            "omnibci.infrastructure.neuroengine.builder.SignalProcessingWorker",
            return_value=MagicMock(spec=SignalProcessingWorker),
        ),
        patch(
            "omnibci.infrastructure.neuroengine.builder.PeerGateway",
            return_value=MagicMock(spec=PeerGateway),
        ),
    ):
        yield NeuroEngineBuilder(settings_snapshot={})


class TestNeuroEngineConfig:
    def test_default_queue_size(self):
        assert NeuroEngineConfig().queue_size == 100

    def test_custom_queue_size(self):
        assert NeuroEngineConfig(queue_size=50).queue_size == 50


class TestNeuroEngineBuilder:
    def test_acquisition_logs_use_a_separate_bounded_owned_channel(self):
        engine = NeuroEngineBuilder(settings_snapshot={}).build()
        try:
            worker_channel = engine._worker_factory("acquisition", _source_snapshot())
            worker = worker_channel.process
            worker_channel.receiver.close()
            worker_channel.reporter.close()
            channel = worker.log_queue
            assert channel is engine.acquisition_logs.queue
            assert channel._maxsize == 256
            assert channel in engine._owned_ipc_queues
            assert all(channel is not other for other in (
                engine.data_manager.buffer, engine.event_bus._event_queue,
                engine.acquisition_command_queue, engine.processor_command_queue,
                worker.preview_queue,
            ))
        finally:
            asyncio.run(engine._shutdown_resources())

    def test_missing_runtime_snapshot_fails_before_constructing_resources(self):
        with patch("omnibci.infrastructure.neuroengine.builder.mp.Queue") as queue:
            with pytest.raises(ValueError, match="pinned settings snapshot"):
                NeuroEngineBuilder()
        queue.assert_not_called()

    def test_build_returns_neuro_engine(self, patched_builder):
        eng = patched_builder.build()
        assert isinstance(eng, NeuroEngine)

    def test_engine_has_event_bus(self, patched_builder):
        eng = patched_builder.build()
        assert isinstance(eng.event_bus, EventBus)

    def test_event_bus_queue_is_set(self, patched_builder):
        eng = patched_builder.build()
        assert eng.event_bus._event_queue is not None
        assert eng.event_bus._event_queue in eng._owned_ipc_queues

    def test_engine_has_data_manager(self, patched_builder):
        eng = patched_builder.build()
        assert isinstance(eng.data_manager, DataManager)
        assert eng.data_manager.buffer in eng._owned_ipc_queues

    def test_engine_defers_acquisition_worker_construction(self, patched_builder):
        eng = patched_builder.build()
        assert eng.acquisition_worker is None

    def test_engine_defers_processing_worker_construction(self, patched_builder):
        eng = patched_builder.build()
        assert eng.signal_processing_worker is None

    def test_engine_should_stop_false(self, patched_builder):
        eng = patched_builder.build()
        assert eng.should_stop is False

    def test_desktop_stop_policy_reaches_the_worker_owner(self, patched_builder):
        engine = NeuroEngineBuilder(settings_snapshot={}, allow_process_termination=False).build()
        assert engine._allow_process_termination is False

    def test_custom_config_propagated(self):
        """Builder with queue_size=5 should create queues of maxsize=5."""
        with (
            patch("omnibci.infrastructure.neuroengine.builder.AcquisitionWorker", return_value=MagicMock()),
            patch("omnibci.infrastructure.neuroengine.builder.SignalProcessingWorker", return_value=MagicMock()),
            patch("omnibci.infrastructure.neuroengine.builder.PeerGateway", return_value=MagicMock()),
        ):
            eng = NeuroEngineBuilder(NeuroEngineConfig(queue_size=5), settings_snapshot={}).build()
        assert eng.acquisition_command_queue._maxsize == 5
        assert eng.processor_command_queue._maxsize == 5

    def test_processes_share_same_data_manager(self, patched_builder):
        """AcquisitionWorker and SignalProcessingWorker must share the same DataManager."""
        import omnibci.infrastructure.neuroengine.builder as b_mod
        captured = {}

        orig_acq = b_mod.AcquisitionWorker
        orig_sp = b_mod.SignalProcessingWorker

        with (
            patch.object(
                b_mod, "AcquisitionWorker",
                side_effect=lambda **kw: (captured.update(acq_dm=kw["data_manager"]), MagicMock())[1],
            ),
            patch.object(
                b_mod, "SignalProcessingWorker",
                side_effect=lambda **kw: (captured.update(sp_dm=kw["data_manager"]), MagicMock())[1],
            ),
            patch("omnibci.infrastructure.neuroengine.builder.PeerGateway", return_value=MagicMock()),
        ):
            engine = NeuroEngineBuilder(settings_snapshot={}).build()
            for module in ("acquisition", "processing"):
                channel = engine._worker_factory(module, _source_snapshot())
                channel.receiver.close()
                channel.reporter.close()

        assert captured["acq_dm"] is captured["sp_dm"]

    def test_processes_share_same_event_bus(self, patched_builder):
        """SignalProcessingWorker must receive the same EventBus as the engine."""
        import omnibci.infrastructure.neuroengine.builder as b_mod

        captured = {}
        with (
            patch("omnibci.infrastructure.neuroengine.builder.AcquisitionWorker", return_value=MagicMock()),
            patch.object(
                b_mod, "SignalProcessingWorker",
                side_effect=lambda **kw: (captured.update(sp_eb=kw["event_bus"]), MagicMock())[1],
            ),
            patch("omnibci.infrastructure.neuroengine.builder.PeerGateway", return_value=MagicMock()),
        ):
            eng = NeuroEngineBuilder(settings_snapshot={}).build()
            channel = eng._worker_factory("processing", {})
            channel.receiver.close()
            channel.reporter.close()

        assert captured["sp_eb"] is eng.event_bus


class TestConfigFromSettings:
    def test_reads_all_keys(self):
        values = {
            "NEUROENGINE.queue_size": 50,
            "ACQUISITION.sample_rate": 250,
            "debug": True,
        }
        cfg = NeuroEngineConfig.from_settings(values)
        assert cfg.queue_size == 50
        assert not hasattr(cfg, "sample_rate")
        assert cfg.debug is True

    def test_falls_back_on_missing_keys(self):
        cfg = NeuroEngineConfig.from_settings({})
        assert cfg.queue_size == 100
        assert cfg.debug is False

    def test_dataclass_defaults(self):
        cfg = NeuroEngineConfig()
        assert (cfg.queue_size, cfg.debug) == (100, False)

    def test_dormant_module_values_are_not_parsed_by_foundation(self):
        cfg = NeuroEngineConfig.from_settings({
            "ALGORITHM.baseline_duration": "invalid",
            "STIMULATION.server_port": "invalid",
            "ACTUATION.server_port": "invalid",
            "ACTUATION.watchdog_timeout_s": "invalid",
        })
        assert cfg.queue_size == 100
        assert not hasattr(cfg, "baseline_duration")

    def test_worker_stores_baseline_duration(self):
        w = SignalProcessingWorker(command_queue=MagicMock(), baseline_duration=0.3, settings_snapshot={})
        assert w.baseline_duration == 0.3


def _debug_engine(debug: bool) -> NeuroEngine:
    return NeuroEngine(
        event_bus=MagicMock(),
        data_manager=MagicMock(),
        acquisition_worker=MagicMock(),
        acquisition_command_queue=MagicMock(),
        signal_processing_worker=MagicMock(),
        processor_command_queue=MagicMock(),
        peer_gateway=MagicMock(),
        debug=debug,
    )


class TestEngineDebugFlag:
    def test_debug_default_is_false(self):
        eng = NeuroEngine(
            event_bus=MagicMock(), data_manager=MagicMock(),
            acquisition_worker=MagicMock(), acquisition_command_queue=MagicMock(),
            signal_processing_worker=MagicMock(), processor_command_queue=MagicMock(),
            peer_gateway=MagicMock(),
        )
        assert eng._debug is False

    @patch("omnibci.infrastructure.neuroengine.engine.asyncio.run")
    def test_run_passes_debug_true(self, mock_run):
        _debug_engine(debug=True).run()
        assert mock_run.call_args.kwargs.get("debug") is True
        mock_run.call_args.args[0].close()

    @patch("omnibci.infrastructure.neuroengine.engine.asyncio.run")
    def test_run_passes_debug_false(self, mock_run):
        _debug_engine(debug=False).run()
        assert mock_run.call_args.kwargs.get("debug") is False
        mock_run.call_args.args[0].close()


class TestProcessorSampleRate:
    def test_worker_has_no_configured_sample_rate(self):
        worker = SignalProcessingWorker(command_queue=MagicMock(),
                                        settings_snapshot={"ACQUISITION.sample_rate": 250})
        assert not hasattr(worker, "sample_rate")


@pytest.mark.parametrize("worker_type", ["acquisition", "processing"])
def test_worker_configuration_is_detached_and_required(worker_type):
    Worker = AcquisitionWorker if worker_type == "acquisition" else SignalProcessingWorker
    snapshot = {"PLUGINS.bindings": [{"parameters": {"sample_rate": 250}}]}
    worker = Worker(command_queue=None, settings_snapshot=snapshot)
    snapshot["PLUGINS.bindings"][0]["parameters"]["sample_rate"] = 500
    assert worker.settings_snapshot["PLUGINS.bindings"][0]["parameters"]["sample_rate"] == 250
    with pytest.raises(TypeError, match="settings_snapshot"):
        Worker(command_queue=None)


def test_builder_passes_shared_source_context_with_owned_module_revisions():
    snapshot = synthetic_execution_snapshot({
        "ACQUISITION.sample_rate": 250, "ACQUISITION.DATA_PROVIDER_PORT": 4455,
        "CONFIG.module_revisions": {"acquisition": "a" * 64, "processing": "b" * 64},
        "CONFIG.engine_revision": "c" * 64, "CONFIG.generation": "run-1",
        "PLUGINS.bindings": [ProfileBinding("acquisition.source", "acquisition", None,
            PluginSelection("test.source", "default", "1.0.0"), {}).to_dict()],
    })
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
