"""
NeuroEngine domain commands.
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class StartEngineCommand:
    """Start the NeuroEngine processing pipeline."""


@dataclass(frozen=True)
class StopEngineCommand:
    """Gracefully shut down the NeuroEngine."""
