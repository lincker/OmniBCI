"""
Concrete peer configurations (the routing table, in code).

Each factory returns a PeerConfig wiring a transport + RouteSpec for one peer.
This is the single place the per-peer routing table lives.
"""

from omnibci.shared.events import Event, EventType
from omnibci.infrastructure.ipc.peer_transport import ListenTransport
from omnibci.infrastructure.neuroengine.gateway.routing import RouteSpec, PeerConfig

# Stimulation peer name — single source is the application-layer port
# vocabulary (application.ports.engine_port.STIMULATION_PEER); re-exported
# here under the historical name for gateway-side consumers.
from omnibci.application.ports.engine_port import STIMULATION_PEER as STIMULATION
# Actuation peer name (used by PeerGateway.peer("actuation")). The actuation
# subsystem loads an Effector plugin to carry out the commands.
ACTUATION = "actuation"

# Synthetic fault the engine raises onto its own bus when the actuation link is
# lost or goes silent. It is NOT sent to the (dead) device — device-side
# dead-man safe-stop is the controller's responsibility; this is the engine's
# internal alarm, consumed by ActuationFeedbackHandler.
ACTUATION_LINK_LOST = {"code": "LINK_LOST", "source": "engine_watchdog"}


def _actuation_failsafe():
    """Events published onto the bus when the actuation link trips the watchdog."""
    return [Event(EventType.ActuationFault, ACTUATION_LINK_LOST)]


def stimulation_peer_config(host: str, port: int) -> PeerConfig:
    """
    neuroengine ↔ stimulation channel.

    Outbound (engine → stimulation): the three events the old
    ServerMessageManagement pushed — result feedback + stim control.
    Inbound (stimulation → engine): StimulationAck (consumed downstream by the
    results broadcaster). MessageFromStimulation is allowed for forward-compat.

    Behaviour is identical to the previous single-server push, now expressed as
    one routed channel.
    """
    return PeerConfig(
        name=STIMULATION,
        transport=ListenTransport(host=host, port=port),
        routes=RouteSpec(
            outbound=frozenset({
                EventType.TrialCompleted,
                EventType.StartStimTrial,
                EventType.StopStimulation,
            }),
            inbound=frozenset({
                EventType.StimulationAck,
                EventType.MessageFromStimulation,
            }),
        ),
    )


def actuation_peer_config(host: str, port: int,
                          watchdog_timeout_s: float = 1.0) -> PeerConfig:
    """
    neuroengine ↔ actuation channel. The engine *listens*; the actuation
    controller dials in (same topology as stimulation).

    Outbound (engine → actuation): ActuationCommand — the device-drive command,
    produced by ActuationCommandTranslator from each decoded trial.
    Inbound (actuation → engine): execution acks, device state/sensor feedback,
    and faults (consumed by ActuationFeedbackHandler).

    Safety (Phase 4): if the link drops or the controller goes silent past
    ``watchdog_timeout_s``, the channel raises an internal ActuationFault
    (LINK_LOST). Pass ``watchdog_timeout_s=None`` to disable the heartbeat check
    (disconnect-edge failsafe still applies via ``failsafe``).
    """
    return PeerConfig(
        name=ACTUATION,
        # One controller owns an actuation command stream.  A second connection
        # must not receive and execute the same physical command.
        transport=ListenTransport(host=host, port=port, max_clients=1),
        routes=RouteSpec(
            outbound=frozenset({
                EventType.ActuationCommand,
            }),
            inbound=frozenset({
                EventType.ActuationAck,
                EventType.ActuationState,
                EventType.ActuationFault,
            }),
        ),
        failsafe=_actuation_failsafe,
        watchdog_timeout_s=watchdog_timeout_s,
    )
