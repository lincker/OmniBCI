"""
Unit tests for NeuroEngineService (application service).

NeuroEngine itself is fully mocked — no subprocesses are spawned.
"""
import pytest
from unittest.mock import MagicMock, patch, AsyncMock

from omnibci.application.commands.engine_commands import (
    StartEngineCommand,
    StopEngineCommand,
)
from omnibci.application.queries.engine_queries import GetEngineStatusQuery
from omnibci.application.services.engine_service import NeuroEngineService


class TestNeuroEngineServiceStatus:
    def test_status_before_init_returns_all_false(self):
        svc = NeuroEngineService(engine_factory=MagicMock())
        result = svc.handle_get_status(GetEngineStatusQuery())
        assert result.running is False
        assert result.acquisition_alive is False
        assert result.processor_alive is False

    def test_status_with_mock_engine(self, engine_service, mock_engine):
        engine_service._running = True
        result = engine_service.handle_get_status(GetEngineStatusQuery())
        assert result.running is True
        assert result.acquisition_alive is True
        assert result.processor_alive is True

    def test_status_when_processes_dead(self, engine_service, mock_engine):
        mock_engine.acquisition_worker.is_alive.return_value = False
        mock_engine.signal_processing_worker.is_alive.return_value = False
        result = engine_service.handle_get_status(GetEngineStatusQuery())
        assert result.acquisition_alive is False
        assert result.processor_alive is False


class TestNeuroEngineServiceStart:
    @pytest.mark.asyncio
    async def test_start_sets_running(self, engine_service):
        with patch("threading.Thread") as mock_thread_cls:
            mock_thread = MagicMock()
            mock_thread_cls.return_value = mock_thread
            await engine_service.handle_start_engine(StartEngineCommand())
        assert engine_service._running is True

    @pytest.mark.asyncio
    async def test_start_twice_is_idempotent(self, engine_service):
        """Second StartEngineCommand is ignored when already running."""
        with patch("threading.Thread") as mock_thread_cls:
            mock_thread_cls.return_value = MagicMock()
            await engine_service.handle_start_engine(StartEngineCommand())
            thread_call_count = mock_thread_cls.call_count
            await engine_service.handle_start_engine(StartEngineCommand())
            assert mock_thread_cls.call_count == thread_call_count  # no new thread

    @pytest.mark.asyncio
    async def test_start_without_init_raises(self):
        svc = NeuroEngineService(engine_factory=MagicMock())
        # _engine is None — must raise
        with pytest.raises(RuntimeError, match="not initialised"):
            await svc.handle_start_engine(StartEngineCommand())


class TestNeuroEngineServiceStop:
    @pytest.mark.asyncio
    async def test_stop_clears_running(self, engine_service):
        engine_service._running = True
        engine_service._engine_thread = MagicMock()
        await engine_service.handle_stop_engine(StopEngineCommand())
        assert engine_service._running is False

    @pytest.mark.asyncio
    async def test_stop_when_not_running_is_safe(self, engine_service):
        engine_service._running = False
        await engine_service.handle_stop_engine(StopEngineCommand())  # no exception


class TestProcessorHandlersRemoved:
    """Processor start/stop handlers were removed — the signal-processing
    worker auto-starts with the engine and stops on engine shutdown."""

    def test_handlers_are_gone(self, engine_service):
        assert not hasattr(engine_service, "handle_start_signal_processor")
        assert not hasattr(engine_service, "handle_stop_signal_processor")
