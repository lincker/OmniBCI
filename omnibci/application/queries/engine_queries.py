"""
NeuroEngine read-side queries (CQRS query side).
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class GetEngineStatusQuery:
    """Return current NeuroEngine run state."""


@dataclass
class EngineStatusResult:
    running: bool
    acquisition_alive: bool
    processor_alive: bool
    stimulation_connected: bool = False
    actuation_connected: bool = False
