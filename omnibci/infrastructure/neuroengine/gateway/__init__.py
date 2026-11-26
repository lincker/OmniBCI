"""
Compatibility access to the shared IPC gateway from the Engine package.

PeerGateway owns one PeerChannel per peer. Each channel routes EventBus events
to/from its peer according to a RouteSpec, replacing the old single-peer,
broadcast-everything ServerMessageManagement.
"""
from omnibci.infrastructure.neuroengine.gateway.peer_gateway import PeerGateway

__all__ = ["PeerGateway"]
