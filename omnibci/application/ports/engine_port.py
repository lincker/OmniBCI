"""
EnginePort — Driven Port (application layer).

Why this exists
---------------
Before P0, AcquisitionAppService and StimulationAppService reached into
``engine_service._engine.event_bus`` — a private attribute of another service
wrapping a concrete NeuroEngine.  That leaked the engine's internals across
bounded contexts and hard-bound the acquisition/stimulation contexts to one
specific engine implementation.

EnginePort replaces that穿透 with an explicit contract: "dispatch an event into
the running engine".  Bounded-context services depend on this interface only;
NeuroEngineService is the adapter that implements it.

Layering note
-------------
The port lives in the application layer (not domain/) on purpose: it abstracts
over the ``Event`` envelope (omnibci.shared.events), a shared-kernel contract.
Keeping the port here keeps the dependency direction clean — domain/ imports
only the shared kernel, never application/ or infrastructure/.
"""

from abc import ABC, abstractmethod
from typing import Any, Callable, Protocol

from omnibci.shared.events import Event

#: 刺激 peer 通道名 —— application 层与 is_peer_connected() 交互使用的词汇；
#: infrastructure 的 peer 路由表（gateway/peers.py）从这里取名，保证单一来源。
STIMULATION_PEER = "stimulation"


class EngineHandle(Protocol):
    """构建完成、可运行的引擎对象的最小结构契约（P0-1）。

    NeuroEngineService 通过它管理引擎生命周期，而不知道引擎的具体类型。
    属性刻意保持宽松（Any）：status 查询目前仍会触达 worker 存活位与
    peer gateway —— 这是当前真实的耦合面，先如实声明，收紧留给后续。
    """
    event_bus: Any
    acquisition_worker: Any
    signal_processing_worker: Any
    peer_gateway: Any
    should_stop: bool
    shutdown_complete: bool

    def run(self) -> None: ...
    def wait_until_started(self, timeout: float | None = None) -> None: ...


#: 引擎工厂：每次调用构建一张全新的引擎图（mp.Process 单次使用，
#: stop 后必须整图重建）。由组合根（bootstrap）注入具体实现。
EngineFactory = Callable[[], EngineHandle]


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
