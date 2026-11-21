"""
Stimulation domain commands.
"""
from dataclasses import dataclass
from omnibci.application.plugin_schema import validate_parameters


@dataclass(frozen=True)
class StartStimulationCommand:
    """Launch the stimulation paradigm."""


@dataclass(frozen=True)
class StopStimulationCommand:
    """Stop the running stimulation."""


@dataclass(frozen=True, kw_only=True)
class StartStimTrialCommand:
    """A bounded batch of explicit, independent per-trial JSON objects."""

    trial_params: list[dict[str, object]]
    n_trials: int = 1

    def __post_init__(self) -> None:
        if type(self.n_trials) is not int or not 1 <= self.n_trials <= 10000:
            raise ValueError("n_trials must be an integer in 1..10000")
        if (not isinstance(self.trial_params, list) or len(self.trial_params) != self.n_trials
                or any(not isinstance(value, dict) for value in self.trial_params)):
            raise ValueError("trial_params must contain n_trials JSON objects")
        copied = validate_parameters(None, {"trial_params": self.trial_params})
        object.__setattr__(self, "trial_params", copied["trial_params"])
