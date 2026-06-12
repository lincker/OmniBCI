"""
Routing types for PeerGateway.

RouteSpec declares what flows over a peer channel in each direction; PeerConfig
bundles a peer's name, transport, routes and (optionally) safety policy.
"""

from dataclasses import dataclass
from typing import Callable, FrozenSet, Iterable, Optional

from omnibci.shared.events import Event, EventType
from omnibci.infrastructure.ipc.peer_transport import PeerTransport


@dataclass(frozen=True)
class RouteSpec:
    """
    Event types flowing over one peer channel.

    outbound: engine → peer. The channel subscribes these on the EventBus and
              forwards them to the peer.
    inbound:  peer → engine. White-list: an inbound frame whose type is not here
              is dropped (and logged) rather than injected onto the bus.
    """
    outbound: FrozenSet[EventType]
    inbound: FrozenSet[EventType]


@dataclass(frozen=True)
class PeerConfig:
    """Everything PeerGateway needs to stand up one peer channel."""
    name: str
    transport: PeerTransport
    routes: RouteSpec
    # Events to publish onto the bus when the link drops (effector: command a
    # safe state). None = nothing special on disconnect (fine for stimulation).
    failsafe: Optional[Callable[[], Iterable[Event]]] = None
    # Seconds; if set, a missing heartbeat past this triggers failsafe (Phase 4).
    watchdog_timeout_s: Optional[float] = None
