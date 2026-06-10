"""
gateway — neuroengine's boundary to external peers (stimulation, effector, …).

PeerGateway owns one PeerChannel per peer. Each channel routes EventBus events
to/from its peer according to a RouteSpec, replacing the old single-peer,
broadcast-everything ServerMessageManagement.
"""
from omnibci.infrastructure.neuroengine.gateway.routing import RouteSpec, PeerConfig
from omnibci.infrastructure.neuroengine.gateway.peer_channel import PeerChannel
from omnibci.infrastructure.neuroengine.gateway.peer_gateway import PeerGateway

__all__ = ["RouteSpec", "PeerConfig", "PeerChannel", "PeerGateway"]
