"""Compatibility names for the renderer connection to its server-owned IPC host."""
from omnibci.infrastructure.ipc.peer_configs import ENGINE, stimulation_client_config as engine_peer_config

__all__ = ["ENGINE", "engine_peer_config"]
