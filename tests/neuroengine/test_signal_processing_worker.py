"""Tests for SignalProcessingWorker plugin loading and processor wiring."""
from __future__ import annotations

from unittest.mock import MagicMock, call

import numpy as np
import pytest

import omnibci
import omnibci.conf as conf_module
import omnibci.infrastructure.neuroengine.processing.worker as worker_module
import omnibci.infrastructure.plugins.registry as registry_module
from omnibci.domain.ports.algorithm import Decoder, EpochDetector, Filter
from omnibci.infrastructure.neuroengine.processing.worker import (
    SignalProcessingWorker,
)
from omnibci.infrastructure.plugins.loader import PluginLoader
from omnibci.infrastructure.plugins.registry import ObjectFactory


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


def _settings(monkeypatch, *, return_value=None, side_effect=None):
    settings = MagicMock()
    settings.get.return_value = return_value
    settings.get.side_effect = side_effect
    monkeypatch.setattr(conf_module, "settings", settings)
    return settings


class _FirstFilter(Filter):
    def apply(self, data, ctx):
        return np.asarray(data)


class _SecondFilter(Filter):
    def apply(self, data, ctx):
        return np.asarray(data)


class TestFilterLoading:
    def test_settings_failure_falls_back_to_no_filters(self, monkeypatch):
        _settings(monkeypatch, side_effect=RuntimeError("settings unavailable"))

        assert _worker()._load_filters() == []

    @pytest.mark.parametrize("configured", [None, []])
    def test_missing_or_empty_configuration_returns_no_filters(
        self,
        monkeypatch,
        configured,
    ):
        _settings(monkeypatch, return_value=configured)

        assert _worker()._load_filters() == []

    def test_loads_known_filters_in_order_and_skips_unknown(
        self,
        monkeypatch,
        caplog,
    ):
        _settings(monkeypatch, return_value=["SECOND", "MISSING", "FIRST"])
        monkeypatch.setattr(
            registry_module,
            "registry_for",
            MagicMock(
                return_value={
                    "FIRST": _FirstFilter,
                    "SECOND": _SecondFilter,
                }
            ),
        )

        filters = _worker()._load_filters()

        assert [type(filter_) for filter_ in filters] == [
            _SecondFilter,
            _FirstFilter,
        ]
        assert "Unknown filter 'MISSING'" in caplog.text


class TestAlgorithmLoading:
    def test_loads_plugins_and_resolves_configured_algorithm(self, monkeypatch):
        settings = _settings(monkeypatch)
        settings.get.side_effect = {
            "ALGORITHM.decoder": "decoder-type",
            "ALGORITHM.epoch_detector": "detector-type",
        }.__getitem__
        monkeypatch.setattr(omnibci, "__path__", ["/package/omnibci"])

        load_from_dir = MagicMock()
        expected_decoder = MagicMock()
        expected_detector = MagicMock()
        create = MagicMock(side_effect=[expected_decoder, expected_detector])
        monkeypatch.setattr(PluginLoader, "load_from_dir", load_from_dir)
        monkeypatch.setattr(ObjectFactory, "create", create)

        worker = _worker()
        filters = [MagicMock(spec=Filter)]
        worker._load_filters = MagicMock(return_value=filters)

        decoder, detector, loaded_filters = worker._load_algorithm()

        load_from_dir.assert_called_once_with(
            "/package/omnibci/infrastructure/plugins/algorithms",
            base_package="omnibci.infrastructure.plugins.algorithms",
        )
        assert create.call_args_list == [
            call(Decoder, "decoder-type"),
            call(EpochDetector, "detector-type"),
        ]
        assert decoder is expected_decoder
        assert detector is expected_detector
        assert loaded_filters is filters


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
