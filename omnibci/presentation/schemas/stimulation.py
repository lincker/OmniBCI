from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field, computed_field

from omnibci.application.queries.plugin_queries import PluginInfo


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
    # Editable controls from manifest [controls.<name>]; the Web UI renders an
    # input per control and sends the values when triggering a trial. Each value
    # is a descriptor dict: {label, type, default, min?, max?, field}.
    controls: Dict[str, Dict[str, Any]] = Field(default_factory=dict)

    @computed_field
    @property
    def name(self) -> str:
        """Deprecated alias for display_name."""
        return self.display_name

    @classmethod
    def from_plugin_info(cls, p: PluginInfo) -> "ParadigmInfo":
        """Map the application-layer discovery DTO to this wire schema.

        唯一允许的方向：presentation 依赖 application。字段一一对应；
        两侧独立演化时此处会以显式报错（而非静默漂移）暴露差异。
        """
        return cls(
            id=p.id,
            plugin_type=p.plugin_type,
            display_name=p.display_name,
            description=p.description,
            version=p.version,
            source=p.source,
            params=p.params,
            controls=p.controls,
        )


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
