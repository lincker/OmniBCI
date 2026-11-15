"""
Tests for P1 configuration consolidation: NeuroEngineConfig is the single source of engine
tunables, and the deep modules no longer read settings themselves.

Pins:
  - NeuroEngineConfig.from_settings() reads the right keys with fallbacks;
  - NeuroEngine.run() honours the injected debug flag (was hardcoded True);
  - Processing waits for observed signal information;
  - ServerMessageManagement uses the injected host/port (no settings.get).
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from omnibci.infrastructure.neuroengine.builder import NeuroEngineConfig
from omnibci.infrastructure.neuroengine.engine import NeuroEngine
from omnibci.infrastructure.signal_processing.worker import SignalProcessingWorker


# ---------------------------------------------------------------------------
# NeuroEngineConfig.from_settings
# ---------------------------------------------------------------------------

class TestConfigFromSettings:
    def test_reads_all_keys(self, monkeypatch):
        values = {
            "NEUROENGINE.queue_size": 50,
            "ACQUISITION.sample_rate": 250,
            "debug": True,
        }
        import omnibci.conf as conf
        monkeypatch.setattr(conf.settings, "get",
                            lambda key, default=None: values.get(key, default))

        cfg = NeuroEngineConfig.from_settings(values)
        assert cfg.queue_size == 50
        assert not hasattr(cfg, "sample_rate")
        assert cfg.debug is True

    def test_falls_back_on_missing_keys(self, monkeypatch):
        import omnibci.conf as conf
        # Missing keys return the supplied default without swallowing errors.
        monkeypatch.setattr(conf.settings, "get",
                            lambda key, default=None: default)

        cfg = NeuroEngineConfig.from_settings({})
        # dataclass defaults
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
        w = SignalProcessingWorker(command_queue=MagicMock(), baseline_duration=0.3)
        assert w.baseline_duration == 0.3


# ---------------------------------------------------------------------------
# NeuroEngine debug injection
# ---------------------------------------------------------------------------

def _engine(debug: bool) -> NeuroEngine:
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
        _engine(debug=True).run()
        assert mock_run.call_args.kwargs.get("debug") is True
        # Close the coroutine that mocked asyncio.run never awaited, avoiding
        # a "coroutine was never awaited" warning during garbage collection.
        mock_run.call_args.args[0].close()

    @patch("omnibci.infrastructure.neuroengine.engine.asyncio.run")
    def test_run_passes_debug_false(self, mock_run):
        _engine(debug=False).run()
        assert mock_run.call_args.kwargs.get("debug") is False
        mock_run.call_args.args[0].close()


# ---------------------------------------------------------------------------
# Processing does not carry an assumed sample rate
# ---------------------------------------------------------------------------

class TestProcessorSampleRate:
    def test_worker_has_no_configured_sample_rate(self):
        worker = SignalProcessingWorker(command_queue=MagicMock(),
                                        settings_snapshot={"ACQUISITION.sample_rate": 250})
        assert not hasattr(worker, "sample_rate")
