from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field, computed_field


class StartStimulationRequest(BaseModel):
    paradigm: Optional[str] = None


class StartTrialRequest(BaseModel):
    """Trigger one or more stimulation trials.

    n_trials     — how many trials to run (paradigm-agnostic base parameter).
    trial_params — per-trial target/event ids; len must equal n_trials when
                   provided.  None → each trial uses the paradigm default.
    """
    n_trials: int = 1
    trial_params: Optional[List[int]] = None


class ParadigmInfo(BaseModel):
    """Metadata for a discovered stimulation paradigm.

    Fields
    ------
    id           Machine identifier; unique per plugin (= manifest package.id).
    plugin_type  Registry key used to actually start the paradigm.
    display_name Human-readable label shown in the UI.
    description  Optional long description.
    version      Semver string from the manifest, if present.
    source       "builtin" (ships with app) or "user" (downloaded / self-written).

    Deprecated
    ----------
    name         Alias for ``display_name``; kept for one release cycle.
    """

    id: str
    plugin_type: str
    display_name: str
    description: Optional[str] = None
    version: Optional[str] = None
    source: str = "user"
    # Read-only plugin parameters from manifest [params]; shown in the Web UI.
    params: Dict[str, Any] = Field(default_factory=dict)

    @computed_field
    @property
    def name(self) -> str:
        """Deprecated alias for display_name."""
        return self.display_name


class ParadigmsResponse(BaseModel):
    paradigms: List[ParadigmInfo]


class StimulationStatusResponse(BaseModel):
    """Live status of the Stimulation subsystem.

    active          — the application service has issued a StartStimulation command.
    process_alive   — the Stimulation subprocess is currently running (auto_spawn only).
    engine_connected — the Stimulation process has an active IPC connection to the engine.
    """
    active: bool
    process_alive: bool
    engine_connected: bool
    # Paradigm the running subprocess was spawned with (auto_spawn mode); None otherwise.
    paradigm: Optional[str] = None
