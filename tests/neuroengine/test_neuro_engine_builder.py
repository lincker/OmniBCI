"""
Unit tests for NeuroEngineBuilder and NeuroEngineConfig.

The builder assembles real component types (checked via isinstance) but
no processes are started — we only verify the wiring is correct.
"""
from __future__ import annotations

import multiprocessing as mp
from unittest.mock import patch, MagicMock

import pytest

from omnibci.infrastructure.neuroengine.builder import NeuroEngineBuilder, NeuroEngineConfig
from omnibci.infrastructure.neuroengine.engine import NeuroEngine
from omnibci.infrastructure.ipc.event_bus import EventBus
from omnibci.infrastructure.ipc.data_manager import DataManager
from omnibci.infrastructure.acquisition.worker import AcquisitionWorker
from omnibci.infrastructure.neuroengine.processing.worker import SignalProcessingWorker
from omnibci.infrastructure.neuroengine.gateway import PeerGateway


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
        yield NeuroEngineBuilder()


class TestNeuroEngineConfig:
    def test_default_queue_size(self):
        assert NeuroEngineConfig().queue_size == 100

    def test_custom_queue_size(self):
        assert NeuroEngineConfig(queue_size=50).queue_size == 50


class TestNeuroEngineBuilder:
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

    def test_engine_has_acquisition_worker(self, patched_builder):
        eng = patched_builder.build()
        assert eng.acquisition_worker is not None

    def test_engine_has_signal_processing_worker(self, patched_builder):
        eng = patched_builder.build()
        assert eng.signal_processing_worker is not None

    def test_engine_should_stop_false(self, patched_builder):
        eng = patched_builder.build()
        assert eng.should_stop is False

    def test_custom_config_propagated(self):
        """Builder with queue_size=5 should create queues of maxsize=5."""
        with (
            patch("omnibci.infrastructure.neuroengine.builder.AcquisitionWorker", return_value=MagicMock()),
            patch("omnibci.infrastructure.neuroengine.builder.SignalProcessingWorker", return_value=MagicMock()),
            patch("omnibci.infrastructure.neuroengine.builder.PeerGateway", return_value=MagicMock()),
        ):
            eng = NeuroEngineBuilder(NeuroEngineConfig(queue_size=5)).build()
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
            NeuroEngineBuilder().build()

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
            eng = NeuroEngineBuilder().build()

        assert captured["sp_eb"] is eng.event_bus
