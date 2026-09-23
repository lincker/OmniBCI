"""Application-layer port for controlling a running engine."""

from abc import ABC, abstractmethod
from typing import Any, Callable, Protocol

from omnibci.shared.events import Event

# Shared name used by application admission checks and peer routing.
STIMULATION_PEER = "stimulation"


class EngineHandle(Protocol):
    """Minimal engine graph managed by NeuroEngineService."""
    event_bus: Any
    acquisition_worker: Any
    signal_processing_worker: Any
    peer_gateway: Any
    should_stop: bool
    shutdown_complete: bool

    def run(self) -> None: ...
    def wait_until_started(self, timeout: float | None = None) -> None: ...


# Each call returns a fresh graph because multiprocessing workers are single-use.
EngineFactory = Callable[[dict], EngineHandle] | Callable[[], EngineHandle]


class EnginePort(ABC):
    """
    Contract for dispatching events into the running engine.

    ``is_initialized`` means that an engine graph exists and can accept
    composition-time listener registration.  ``is_engine_ready`` is the
    stronger runtime state: commands may be dispatched only while it is true.
    Implementations must raise ``RuntimeError`` outside that running-ready
    state so callers fail loudly instead of writing into an idle or closed bus.
    """

    @property
    @abstractmethod
    def is_initialized(self) -> bool:
        """Whether the engine graph has been constructed."""

    @property
    @abstractmethod
    def is_engine_ready(self) -> bool:
        """Whether the engine has crossed its startup readiness barrier."""

    @abstractmethod
    def dispatch(self, event: Event) -> None:
        """Put a single infrastructure Event onto the engine's event bus."""

    def is_peer_connected(self, peer_name: str) -> bool:
        """Whether an optional IPC peer is currently connected.

        Ports without peer topology return False.  The concrete engine service
        overrides this for stimulation/actuation status and admission checks.
        """
        return False

    @property
    def applied_snapshot(self) -> dict | None:
        """A copy of the last successfully started graph's pinned configuration."""
        return None
