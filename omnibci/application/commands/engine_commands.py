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


@dataclass(frozen=True)
class StartSignalProcessorCommand:
    """Start the signal processor within the engine."""


@dataclass(frozen=True)
class StopSignalProcessorCommand:
    """Stop the signal processor within the engine."""
