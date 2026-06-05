"""
Acquisition domain commands.
Each command is an immutable value object representing a user's intent.
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class ConnectDataProviderCommand:
    """Connect to the configured data source."""


@dataclass(frozen=True)
class DisconnectDataProviderCommand:
    """Disconnect from the current data source."""


@dataclass(frozen=True)
class StartStreamCommand:
    """Begin streaming EEG data from the connected source."""


@dataclass(frozen=True)
class StopStreamCommand:
    """Stop the active data stream."""
