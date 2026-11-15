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

    def read_acquisition_preview(self, capture_id: str | None, sequence_id: int | None = None) -> dict: ...

    def run(self) -> None: ...
    def wait_until_started(self, timeout: float | None = None) -> None: ...
    async def reconfigure_output_modules(self, snapshot: dict, modules: tuple[str, ...]) -> None: ...
    async def ensure_output_ready(self, module: str, snapshot: dict) -> None: ...
    def ensure_output_ready_sync(self, module: str, snapshot: dict) -> None: ...
    async def start_module(self, module: str, snapshot: dict) -> None: ...
    async def stop_module(self, module: str) -> None: ...
    def is_module_running(self, module: str) -> bool: ...
    async def set_actuation_active(self, active: bool) -> None: ...


# Each call returns a fresh graph because multiprocessing workers are single-use.
EngineFactory = Callable[[dict], EngineHandle] | Callable[[], EngineHandle]


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

    def is_module_running(self, module: str) -> bool:
        """Whether the requested business module is running on the engine."""
        return False

    async def start_module(self, module: str) -> None:
        """Start one module without restarting the engine or other modules."""
        raise NotImplementedError

    async def stop_module(self, module: str) -> None:
        """Stop one module while keeping the engine available."""
        raise NotImplementedError

    def prepare_output_sync(self, module: str) -> dict:
        """Prepare the selected output configuration before a synchronous start."""
        raise NotImplementedError

    def ensure_output_ready_sync(self, module: str, snapshot: dict) -> None:
        """Ensure the output transport before its local process is spawned."""
        raise NotImplementedError

    @property
    def applied_snapshot(self) -> dict | None:
        """A copy of the acknowledged foundation and module configuration."""
        return None
