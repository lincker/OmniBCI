"""
Stimulation domain commands.
"""
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class StartStimulationCommand:
    """Launch the stimulation paradigm."""
    paradigm: Optional[str] = None  # override config default if provided


@dataclass(frozen=True)
class StopStimulationCommand:
    """Stop the running stimulation."""


@dataclass(frozen=True)
class StartStimTrialCommand:
    """Trigger a single stimulation trial."""
    target_index: Optional[int] = None
