"""
Stimulation domain commands.
"""
from dataclasses import dataclass
from typing import Optional, Sequence, Any


@dataclass(frozen=True)
class StartStimulationCommand:
    """Launch the stimulation paradigm."""
    paradigm: Optional[str] = None  # override config default if provided


@dataclass(frozen=True)
class StopStimulationCommand:
    """Stop the running stimulation."""


@dataclass(frozen=True)
class StartStimTrialCommand:
    """Trigger one or more stimulation trials.

    n_trials     — how many trials to run (paradigm-agnostic base parameter).
    trial_params — per-trial paradigm-specific parameters; len must equal
                   n_trials when provided.  None → each trial uses the
                   paradigm's default (e.g. SSVEP no-cue free run).
    """
    n_trials: int = 1
    trial_params: Optional[Sequence[Any]] = None
