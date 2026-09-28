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
from omnibci.infrastructure.ipc.peer_identity import PeerIdentity
from omnibci.infrastructure.neuroengine.gateway.routing import PeerConfig, RouteSpec

ENGINE = "engine"


def engine_peer_config(host: str, port: int, *, identity: PeerIdentity | None = None,
                       reconnect: bool = True, on_disconnect=None) -> PeerConfig:
    """PeerConfig for the stimulation process to connect to the neuroengine."""
    return PeerConfig(
        name=ENGINE,
        transport=ConnectTransport(host=host, port=port, identity=identity,
                                   reconnect=reconnect, on_disconnect=on_disconnect),
        routes=RouteSpec(
            outbound=frozenset({
                EventType.CommandResult,
                EventType.StimulationAck,
                EventType.MessageFromStimulation,
            }),
            inbound=frozenset({
                EventType.TrialCompleted,
                EventType.StartStimTrial,
                EventType.StopStimulation,
                EventType.PauseStimulation,
            }),
        ),
    )
