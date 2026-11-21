from typing import Optional
from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

from omnibci.application.commands.stimulation_commands import StartStimTrialCommand


class StartTrialRequest(BaseModel):
    """Start an explicit batch of per-trial JSON objects."""
    model_config = ConfigDict(extra="forbid")

    n_trials: int = Field(1, strict=True, ge=1, le=10000)
    trial_params: list[dict[str, JsonValue]] = Field(min_length=1, max_length=10000)

    @model_validator(mode="after")
    def validate_batch(self):
        command = StartStimTrialCommand(n_trials=self.n_trials, trial_params=self.trial_params)
        self.trial_params = command.trial_params
        return self


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
