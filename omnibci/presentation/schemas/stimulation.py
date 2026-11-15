from typing import List, Optional
from pydantic import BaseModel


class StartTrialRequest(BaseModel):
    """Trigger one or more stimulation trials.

    n_trials     — how many trials to run (paradigm-agnostic base parameter).
    trial_params — per-trial target/event ids; len must equal n_trials when
                   provided.  None → each trial uses the paradigm default.
    """
    n_trials: int = 1
    trial_params: Optional[List[int]] = None


class StimulationStatusResponse(BaseModel):
    """Live status of the Stimulation subsystem.

    active          — the application service has started stimulation.
    process_alive   — the Stimulation subprocess is currently running (auto_spawn only).
    engine_connected — the Stimulation process has an active IPC connection to the engine.
    """
    active: bool
    process_alive: bool
    engine_connected: bool
    # Paradigm the running subprocess was spawned with (auto_spawn mode); None otherwise.
    paradigm: Optional[str] = None
