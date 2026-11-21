"""Output nodes stop and refetch configuration after their own link fails."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from omnibci.infrastructure.actuation.application import Actuation
from omnibci.infrastructure.stimulation.application import Stimulation
from omnibci.shared.events import Event, EventType


@pytest.mark.parametrize("node_class", [Stimulation, Actuation])
@pytest.mark.parametrize("peer,channel_id,stopped", [
    ("engine", "current", True),
    ("engine", "replaced", False),
    ("another", "current", False),
])
def test_only_current_engine_channel_fault_stops_node(node_class, peer, channel_id, stopped):
    app = node_class.__new__(node_class)
    app.event_bus = MagicMock(start=AsyncMock())
    app.peer_channel = MagicMock(channel_id="current", start=AsyncMock())
    app.actuator = MagicMock()
    app._external_node = True
    app._reconnect_requested = False
    app._paused = False
    app.should_stop = False
    if node_class is Actuation:
        app._device_executor = None
    try:
        asyncio.run(app.start())
    finally:
        if node_class is Actuation and app._device_executor is not None:
            app._device_executor.shutdown(wait=True)
    subscriptions = {
        call.kwargs["event_type"]: call.kwargs["handler"]
        for call in app.event_bus.register_event_listener.call_args_list
    }

    subscriptions[EventType.PeerDeliveryFault](Event(EventType.PeerDeliveryFault, {
        "peer": peer, "channel_id": channel_id,
        "reason": "Sender queue is full", "unsent_command_ids": [],
    }))

    assert app.should_stop is stopped
    assert app._paused is stopped
    assert app._reconnect_requested is stopped
