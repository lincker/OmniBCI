"""Shared cleanup contract for actuation and stimulation applications."""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from omnibci.infrastructure.actuation.application import Actuation
from omnibci.infrastructure.stimulation.application import Stimulation


_APPLICATION_TYPES = [Actuation, Stimulation]


def _application(application_type):
    application = application_type.__new__(application_type)
    application.peer_channel = MagicMock()
    application.peer_channel.stop = AsyncMock()
    application.event_bus = MagicMock()
    application.event_bus.stop = AsyncMock()
    application._close_event_queue = MagicMock()
    return application


def _set_failure(application, site: str, error: BaseException) -> None:
    if site == "peer_channel":
        application.peer_channel.stop.side_effect = error
    elif site == "event_bus":
        application.event_bus.stop.side_effect = error
    else:
        application._close_event_queue.side_effect = error


@pytest.mark.parametrize(
    "application_type",
    _APPLICATION_TYPES,
    ids=["actuation", "stimulation"],
)
class TestApplicationCleanup:
    @pytest.mark.asyncio
    async def test_success_releases_every_ipc_owner(self, application_type):
        application = _application(application_type)

        await application.stop()

        application.peer_channel.stop.assert_awaited_once_with()
        application.event_bus.stop.assert_awaited_once_with()
        application._close_event_queue.assert_called_once_with()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "failure_site",
        ["peer_channel", "event_bus", "event_queue"],
    )
    async def test_component_failure_is_aggregated_after_remaining_cleanup(
        self,
        application_type,
        failure_site,
    ):
        application = _application(application_type)
        error = OSError(f"{failure_site} failed")
        _set_failure(application, failure_site, error)

        with pytest.raises(ExceptionGroup) as raised:
            await application.stop()

        assert raised.value.exceptions == (error,)
        application.peer_channel.stop.assert_awaited_once_with()
        application.event_bus.stop.assert_awaited_once_with()
        application._close_event_queue.assert_called_once_with()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("cancel_site", ["peer_channel", "event_bus"])
    async def test_cancellation_is_re_raised_after_remaining_cleanup(
        self,
        application_type,
        cancel_site,
    ):
        application = _application(application_type)
        cancellation = asyncio.CancelledError(f"{cancel_site} cancelled")
        _set_failure(application, cancel_site, cancellation)

        with pytest.raises(asyncio.CancelledError) as raised:
            await application.stop()

        assert raised.value is cancellation
        assert raised.value.__cause__ is None
        application.peer_channel.stop.assert_awaited_once_with()
        application.event_bus.stop.assert_awaited_once_with()
        application._close_event_queue.assert_called_once_with()

    @pytest.mark.asyncio
    async def test_first_cancellation_wins_when_both_async_owners_cancel(
        self,
        application_type,
    ):
        application = _application(application_type)
        peer_cancellation = asyncio.CancelledError("peer cancelled")
        bus_cancellation = asyncio.CancelledError("bus cancelled")
        application.peer_channel.stop.side_effect = peer_cancellation
        application.event_bus.stop.side_effect = bus_cancellation

        with pytest.raises(asyncio.CancelledError) as raised:
            await application.stop()

        assert raised.value is peer_cancellation
        application._close_event_queue.assert_called_once_with()

    @pytest.mark.asyncio
    async def test_cancellation_preserves_additional_cleanup_errors_as_cause(
        self,
        application_type,
    ):
        application = _application(application_type)
        cancellation = asyncio.CancelledError("peer cancelled")
        cleanup_error = OSError("event bus failed")
        application.peer_channel.stop.side_effect = cancellation
        application.event_bus.stop.side_effect = cleanup_error

        with pytest.raises(asyncio.CancelledError) as raised:
            await application.stop()

        assert raised.value is cancellation
        assert isinstance(raised.value.__cause__, ExceptionGroup)
        assert raised.value.__cause__.exceptions == (cleanup_error,)
        application._close_event_queue.assert_called_once_with()
