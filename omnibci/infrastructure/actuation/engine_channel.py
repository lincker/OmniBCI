"""
engine_channel — actuation-side peer config for the engine connection.

The engine uses a ListenTransport (it binds ACTUATION.server_ip:server_port
and waits). The actuation process uses ConnectTransport (it dials out to the
engine). Routes are the mirror image of the engine's actuation_peer_config in
neuroengine/gateway/peers.py:
  outbound (actuation → engine): ActuationAck, ActuationState, ActuationFault
  inbound  (engine → actuation): ActuationCommand
"""

from omnibci.shared.events import EventType
from omnibci.infrastructure.ipc.peer_transport import ConnectTransport
from omnibci.infrastructure.ipc.peer_identity import PeerIdentity
from omnibci.infrastructure.neuroengine.gateway.routing import PeerConfig, RouteSpec

ENGINE = "engine"


def engine_peer_config(host: str, port: int, *, identity: PeerIdentity | None = None,
                       reconnect: bool = True, on_disconnect=None) -> PeerConfig:
    """PeerConfig for the actuation process to connect to the neuroengine."""
    return PeerConfig(
        name=ENGINE,
        transport=ConnectTransport(host=host, port=port, identity=identity,
                                   reconnect=reconnect, on_disconnect=on_disconnect),
        routes=RouteSpec(
            outbound=frozenset({
                EventType.ActuationAck,
                EventType.ActuationState,
                EventType.ActuationFault,
                EventType.CommandResult,
            }),
            inbound=frozenset({
                EventType.ActuationCommand,
                EventType.SettleActuation,
            }),
        ),
    )
