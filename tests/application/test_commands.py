"""
Defaults consumed by stimulation commands.
"""
from omnibci.application.commands.stimulation_commands import (
    StartStimulationCommand,
    StartStimTrialCommand,
)


class TestStimulationCommands:
    def test_start_default_paradigm_is_none(self):
        cmd = StartStimulationCommand()
        assert cmd.paradigm is None

    def test_trial_defaults(self):
        cmd = StartStimTrialCommand()
        assert cmd.n_trials == 1
        assert cmd.trial_params is None
