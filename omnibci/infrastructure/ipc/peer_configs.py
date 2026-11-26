"""Server and client routes for independent output modules."""
from omnibci.application.plugin_management.execution import PluginExecutionReceipt
from omnibci.application.ports.ipc_port import ACTUATION_PEER as ACTUATION, STIMULATION_PEER as STIMULATION
from omnibci.infrastructure.ipc.peer_identity import PeerIdentity
from omnibci.infrastructure.ipc.peer_transport import ConnectTransport, ListenTransport
from omnibci.infrastructure.ipc.routing import PeerConfig, RouteSpec
from omnibci.shared.events import Event, EventType

ENGINE = "engine"
STIMULATION_ROUTES = RouteSpec(
    outbound=frozenset({EventType.TrialCompleted, EventType.StartStimTrial,
                        EventType.StopStimulation, EventType.PauseStimulation}),
    inbound=frozenset({EventType.CommandResult, EventType.StimulationAck}),
)
ACTUATION_ROUTES = RouteSpec(
    outbound=frozenset({EventType.ActuationCommand, EventType.SettleActuation, EventType.ResumeActuation}),
    inbound=frozenset({EventType.ActuationAck, EventType.ActuationState, EventType.ActuationFault,
                       EventType.CommandResult}),
)
ACTUATION_LINK_LOST = {"code": "LINK_LOST", "source": "engine_watchdog"}


def _actuation_failsafe():
    return [Event(EventType.ActuationFault, ACTUATION_LINK_LOST)]


def actuation_peer_config(host: str, port: int, watchdog_timeout_s: float = 1.0, *,
                          identity: PeerIdentity | None = None, record_execution=None) -> PeerConfig:
    idle_timeout = watchdog_timeout_s * 2 if watchdog_timeout_s and watchdog_timeout_s > 0 else None
    transport = ListenTransport(host, port, max_clients=1, idle_timeout=idle_timeout,
                                identity=identity, record_execution=record_execution)
    return PeerConfig(ACTUATION, transport, ACTUATION_ROUTES, failsafe=_actuation_failsafe,
                      watchdog_timeout_s=watchdog_timeout_s)


def actuation_client_config(host: str, port: int, *, receipt: PluginExecutionReceipt | None = None,
                            reconnect: bool = True, on_disconnect=None) -> PeerConfig:
    transport = ConnectTransport(host, port, receipt=receipt, reconnect=reconnect, on_disconnect=on_disconnect)
    return PeerConfig(ENGINE, transport, RouteSpec(ACTUATION_ROUTES.inbound, ACTUATION_ROUTES.outbound))


def stimulation_peer_config(host: str, port: int, *, identity: PeerIdentity | None = None,
                            record_execution=None) -> PeerConfig:
    transport = ListenTransport(host, port, identity=identity, record_execution=record_execution,
                                max_clients=1 if identity else None)
    return PeerConfig(STIMULATION, transport, STIMULATION_ROUTES)


def stimulation_client_config(host: str, port: int, *, receipt: PluginExecutionReceipt | None = None,
                             reconnect: bool = True, on_disconnect=None) -> PeerConfig:
    transport = ConnectTransport(host, port, receipt=receipt, reconnect=reconnect, on_disconnect=on_disconnect)
    return PeerConfig(ENGINE, transport, RouteSpec(STIMULATION_ROUTES.inbound, STIMULATION_ROUTES.outbound))
