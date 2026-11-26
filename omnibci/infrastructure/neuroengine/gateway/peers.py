"""Compatibility imports for the server-owned output peer configurations."""
from omnibci.infrastructure.ipc.peer_configs import (
    ACTUATION, ACTUATION_LINK_LOST, STIMULATION, actuation_peer_config, stimulation_peer_config,
)

__all__ = ["ACTUATION", "ACTUATION_LINK_LOST", "STIMULATION", "actuation_peer_config", "stimulation_peer_config"]
