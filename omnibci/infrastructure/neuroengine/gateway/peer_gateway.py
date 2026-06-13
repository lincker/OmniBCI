"""
PeerGateway — neuroengine's single boundary to all external peers.

Replaces ServerMessageManagement. Instead of one stimulation server that
broadcasts every registered event to every connected client, the gateway owns
one PeerChannel per peer and routes events per-peer via each channel's RouteSpec.

Lifecycle mirrors the old wrapper so NeuroEngine's call sites are unchanged:
  - start()  : sync — schedules every channel's link as a background task.
  - stop()   : async — tears every channel down (and runs failsafes in Phase 4).
"""

import logging
from typing import Dict

from omnibci.infrastructure.ipc.event_bus import EventBus
from omnibci.infrastructure.neuroengine.gateway.peer_channel import PeerChannel
from omnibci.infrastructure.neuroengine.gateway.routing import PeerConfig

logger = logging.getLogger(__name__)


class PeerGateway:
    def __init__(self, event_bus: EventBus) -> None:
        self._bus = event_bus
        self._channels: Dict[str, PeerChannel] = {}

    def add_peer(self, config: PeerConfig) -> None:
        if config.name in self._channels:
            raise ValueError(f"Peer already registered: {config.name}")
        self._channels[config.name] = PeerChannel(config, self._bus)

    def peer(self, name: str) -> PeerChannel:
        return self._channels[name]

    def start(self) -> None:
        for channel in self._channels.values():
            channel.start()

    async def stop(self) -> None:
        for channel in self._channels.values():
            try:
                await channel.stop()
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning("PeerGateway: error stopping channel '%s': %s",
                               channel.name, exc)
