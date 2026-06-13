"""
Engine restart regression — stop → start must rebuild the engine graph.

mp.Process objects are single-use and NeuroEngine._stop() closes the command
queues, so reusing the old engine raised "cannot start a process twice".
NeuroEngineService now rebuilds via NeuroEngineBuilder on the first start
after a stop and replays recorded bus subscriptions onto the fresh EventBus.

NeuroEngineBuilder is patched — no real processes are spawned.
"""
from unittest.mock import MagicMock, patch

import pytest

from omnibci.application.commands.engine_commands import (
    StartEngineCommand,
    StopEngineCommand,
)
from omnibci.application.services.engine_service import NeuroEngineService
from omnibci.shared.events import EventType


def _mock_engine():
    engine = MagicMock()
    engine.should_stop = False
    # run() must return promptly so the engine thread joins cleanly.
    engine.run = MagicMock()
    return engine


@pytest.fixture()
def svc():
    service = NeuroEngineService()
    service._engine = _mock_engine()
    service._running = False
    return service


class TestRestartRebuildsEngine:
    @pytest.mark.asyncio
    async def test_start_stop_start_builds_new_engine(self, svc):
        first = svc._engine
        rebuilt = _mock_engine()

        await svc.handle_start_engine(StartEngineCommand())
        await svc.handle_stop_engine(StopEngineCommand())

        with patch(
            "omnibci.application.services.engine_service.NeuroEngineBuilder"
        ) as builder_cls:
            builder_cls.return_value.build.return_value = rebuilt
            await svc.handle_start_engine(StartEngineCommand())

        assert svc._engine is rebuilt
        assert svc._engine is not first
        builder_cls.return_value.build.assert_called_once()

    @pytest.mark.asyncio
    async def test_first_start_does_not_rebuild(self, svc):
        first = svc._engine
        with patch(
            "omnibci.application.services.engine_service.NeuroEngineBuilder"
        ) as builder_cls:
            await svc.handle_start_engine(StartEngineCommand())
        builder_cls.return_value.build.assert_not_called()
        assert svc._engine is first

    @pytest.mark.asyncio
    async def test_subscriptions_replayed_onto_new_bus(self, svc):
        handler = MagicMock()
        svc.subscribe(EventType.TrialCompleted, handler)

        await svc.handle_start_engine(StartEngineCommand())
        await svc.handle_stop_engine(StopEngineCommand())

        rebuilt = _mock_engine()
        with patch(
            "omnibci.application.services.engine_service.NeuroEngineBuilder"
        ) as builder_cls:
            builder_cls.return_value.build.return_value = rebuilt
            await svc.handle_start_engine(StartEngineCommand())

        rebuilt.event_bus.register_event_listener.assert_called_once_with(
            EventType.TrialCompleted, handler
        )

    @pytest.mark.asyncio
    async def test_stop_is_idempotent(self, svc):
        await svc.handle_start_engine(StartEngineCommand())
        await svc.handle_stop_engine(StopEngineCommand())
        await svc.handle_stop_engine(StopEngineCommand())  # no error
        assert svc._running is False

    @pytest.mark.asyncio
    async def test_restart_cycle_twice(self, svc):
        """Two full stop/start cycles — each start after stop rebuilds."""
        await svc.handle_start_engine(StartEngineCommand())
        await svc.handle_stop_engine(StopEngineCommand())

        with patch(
            "omnibci.application.services.engine_service.NeuroEngineBuilder"
        ) as builder_cls:
            builder_cls.return_value.build.side_effect = [_mock_engine(), _mock_engine()]
            await svc.handle_start_engine(StartEngineCommand())
            await svc.handle_stop_engine(StopEngineCommand())
            await svc.handle_start_engine(StartEngineCommand())
            assert builder_cls.return_value.build.call_count == 2
