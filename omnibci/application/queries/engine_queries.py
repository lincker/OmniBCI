"""
NeuroEngine read-side queries (CQRS query side).
"""
from dataclasses import dataclass
from typing import Any, Dict, Optional


@dataclass(frozen=True)
class GetEngineStatusQuery:
    """Return current NeuroEngine run state."""


@dataclass
class EngineStatusResult:
    running: bool
    acquisition_alive: bool
    processor_alive: bool
    stimulation_connected: bool = False
    extra: Optional[Dict[str, Any]] = None
