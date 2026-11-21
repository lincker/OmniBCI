"""
Defaults consumed by stimulation commands.
"""
from omnibci.application.commands.stimulation_commands import (
    StartStimTrialCommand,
)


class TestStimulationCommands:
    def test_trial_defaults(self):
        cmd = StartStimTrialCommand(trial_params=[{}])
        assert cmd.n_trials == 1
        assert cmd.trial_params == [{}]
