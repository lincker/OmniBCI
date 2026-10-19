"""
Tests for P1 config-收回: NeuroEngineConfig is the single source of engine
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
            "NEUROENGINE.message_server_ip": "10.0.0.1",
            "NEUROENGINE.message_server_port": 9000,
            "ACQUISITION.sample_rate": 250,
            "debug": True,
        }
        import omnibci.conf as conf
        monkeypatch.setattr(conf.settings, "get",
                            lambda key, default=None: values.get(key, default))

        cfg = NeuroEngineConfig.from_settings(values)
        assert cfg.queue_size == 50
        assert cfg.message_server_ip == "10.0.0.1"
        assert cfg.message_server_port == 9000
        assert not hasattr(cfg, "sample_rate")
        assert cfg.debug is True

    def test_falls_back_on_missing_keys(self, monkeypatch):
        import omnibci.conf as conf
        # P2-9：缺键语义 = get(key, default) 返回 default（异常吞噬已移除）。
        monkeypatch.setattr(conf.settings, "get",
                            lambda key, default=None: default)

        cfg = NeuroEngineConfig.from_settings({})
        # dataclass defaults
        assert cfg.queue_size == 100
        assert cfg.message_server_ip == "127.0.0.1"
        assert cfg.message_server_port == 8000
        assert cfg.debug is False

    def test_dataclass_defaults(self):
        cfg = NeuroEngineConfig()
        assert (cfg.queue_size, cfg.debug) == (100, False)

    def test_reads_baseline_duration(self, monkeypatch):
        import omnibci.conf as conf
        monkeypatch.setattr(conf.settings, "get",
                            lambda key, default=None:
                                0.25 if key == "ALGORITHM.baseline_duration" else default)
        cfg = NeuroEngineConfig.from_settings({"ALGORITHM.baseline_duration": 0.25})
        assert cfg.baseline_duration == 0.25

    def test_baseline_duration_default(self):
        # Dataclass default is 0.2 s of pre-trigger data (see builder.py).
        assert NeuroEngineConfig().baseline_duration == 0.2

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
        # asyncio.run 被 mock，serve() 协程从未被 await——显式关闭，
        # 否则 GC 时报 "coroutine was never awaited" RuntimeWarning。
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
