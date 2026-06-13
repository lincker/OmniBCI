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

from omnibci.shared.events import Event


class EnginePort(ABC):
    """
    Contract for dispatching events into the running engine.

    Implementations must raise RuntimeError if the engine is not yet
    initialised, so callers fail loudly instead of silently dropping events.
    """

    @abstractmethod
    def dispatch(self, event: Event) -> None:
        """Put a single infrastructure Event onto the engine's event bus."""
