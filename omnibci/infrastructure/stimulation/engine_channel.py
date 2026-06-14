"""
engine_channel — stimulation-side peer config for the engine connection.

The engine uses a ListenTransport (it binds a server port and waits).
The stimulation process uses ConnectTransport (it dials out to the engine).
Routes are the mirror image of the engine's stimulation_peer_config in peers.py:
  outbound (stim → engine): StimulationAck, MessageFromStimulation
  inbound  (engine → stim): TrialCompleted, StartStimTrial, StopStimulation
"""

from omnibci.shared.events import EventType
from omnibci.infrastructure.ipc.peer_transport import ConnectTransport
from omnibci.infrastructure.neuroengine.gateway.routing import PeerConfig, RouteSpec

ENGINE = "engine"


def engine_peer_config(host: str, port: int) -> PeerConfig:
    """PeerConfig for the stimulation process to connect to the neuroengine."""
    return PeerConfig(
        name=ENGINE,
        transport=ConnectTransport(host=host, port=port),
        routes=RouteSpec(
            outbound=frozenset({
                EventType.StimulationAck,
                EventType.MessageFromStimulation,
            }),
            inbound=frozenset({
                EventType.TrialCompleted,
                EventType.StartStimTrial,
                EventType.StopStimulation,
            }),
        ),
    )
