"""
Unit tests for Command value objects.

Commands are frozen dataclasses — tests verify immutability, default values,
and that distinct command types are not interchangeable.
"""
import pytest
from omnibci.application.commands.acquisition_commands import (
    ConnectDataProviderCommand,
    DisconnectDataProviderCommand,
    StartStreamCommand,
    StopStreamCommand,
)
from omnibci.application.commands.engine_commands import (
    StartEngineCommand,
    StopEngineCommand,
)
from omnibci.application.commands.stimulation_commands import (
    StartStimulationCommand,
    StopStimulationCommand,
    StartStimTrialCommand,
)


class TestAcquisitionCommands:
    def test_connect_is_frozen(self):
        cmd = ConnectDataProviderCommand()
        with pytest.raises((AttributeError, TypeError)):
            cmd.foo = "bar"  # type: ignore[attr-defined]

    def test_commands_are_value_equal(self):
        assert ConnectDataProviderCommand() == ConnectDataProviderCommand()
        assert DisconnectDataProviderCommand() == DisconnectDataProviderCommand()
        assert StartStreamCommand() == StartStreamCommand()
        assert StopStreamCommand() == StopStreamCommand()

    def test_distinct_command_types_not_equal(self):
        assert ConnectDataProviderCommand() != DisconnectDataProviderCommand()
        assert StartStreamCommand() != StopStreamCommand()


class TestEngineCommands:
    def test_all_instantiate(self):
        for cls in (
            StartEngineCommand,
            StopEngineCommand,
        ):
            assert cls() is not None

    def test_frozen(self):
        cmd = StartEngineCommand()
        with pytest.raises((AttributeError, TypeError)):
            cmd.x = 1  # type: ignore[attr-defined]


class TestStimulationCommands:
    def test_start_default_paradigm_is_none(self):
        cmd = StartStimulationCommand()
        assert cmd.paradigm is None

    def test_start_with_paradigm(self):
        cmd = StartStimulationCommand(paradigm="classic40SSVEP")
        assert cmd.paradigm == "classic40SSVEP"

    def test_trial_default_target_is_none(self):
        assert StartStimTrialCommand().target_index is None

    def test_trial_with_target(self):
        cmd = StartStimTrialCommand(target_index=3)
        assert cmd.target_index == 3

    def test_stop_instantiates(self):
        assert StopStimulationCommand() is not None
