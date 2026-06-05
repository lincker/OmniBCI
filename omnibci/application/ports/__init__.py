"""
Application-layer driven ports.

These abstractions let bounded-context application services collaborate with
the running engine without depending on its concrete implementation
(NeuroEngine / its EventBus).  Concrete adapters live in the same layer
(NeuroEngineService) or in infrastructure.
"""
from omnibci.application.ports.engine_port import EnginePort

__all__ = ["EnginePort"]
