"""Application-layer port for controlling a running engine."""

from abc import ABC, abstractmethod
from typing import Callable, Protocol

from omnibci.shared.events import Event

# Shared name used by application admission checks and peer routing.
from omnibci.application.ports.ipc_port import STIMULATION_PEER as STIMULATION_PEER


class EngineHandle(Protocol):
    """Runtime operations managed by NeuroEngineService, without its graph."""
    @property
    def shutdown_complete(self) -> bool: ...

    def subscribe(self, event_type, handler: Callable[[Event], None]) -> None: ...
    def dispatch(self, event: Event) -> None: ...
    def request_stop(self) -> None: ...

    def read_acquisition_preview(self, capture_id: str | None, sequence_id: int | None = None) -> dict: ...

    def run(self) -> None: ...
    def wait_until_started(self, timeout: float | None = None) -> None: ...
    async def start_module(self, module: str, snapshot: dict) -> None: ...
    async def stop_module(self, module: str) -> None: ...
    def is_module_running(self, module: str) -> bool: ...


# Each call returns a fresh graph because multiprocessing workers are single-use.
EngineFactory = Callable[[dict], EngineHandle]


class EnginePort(ABC):
    """
    Contract for dispatching events into the running engine.

    ``is_initialized`` means that the service can accept composition-time
    listener registration. ``is_engine_ready`` means the runtime infrastructure
    is available; business modules have independent running states.
    Implementations must raise ``RuntimeError`` outside that running-ready
    state so callers fail loudly instead of writing into an idle or closed bus.
    """

    @property
    @abstractmethod
    def is_initialized(self) -> bool:
        """Whether the service is ready for application wiring."""

    @property
    @abstractmethod
    def is_engine_ready(self) -> bool:
        """Whether the engine has crossed its startup readiness barrier."""

    @property
    def is_ready(self) -> bool:
        return self.is_engine_ready

    @abstractmethod
    def dispatch(self, event: Event) -> None:
        """Put a single infrastructure Event onto the engine's event bus."""

    def is_peer_connected(self, peer_name: str) -> bool:
        """Whether an optional IPC peer is currently connected.

        Ports without peer topology return False.  The concrete engine service
        overrides this for stimulation/actuation status and admission checks.
        """
        return False

    def is_command_ready(self, event_type) -> bool:
        """Whether a command's owning module can still answer it."""
        return self.is_engine_ready

    @property
    def applied_snapshot(self) -> dict | None:
        """A copy of the acknowledged foundation and module configuration."""
        return None
