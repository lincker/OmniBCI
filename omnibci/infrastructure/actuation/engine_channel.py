"""Controller connection to the server-owned Actuation listener."""
from omnibci.infrastructure.ipc.peer_configs import ENGINE, actuation_client_config as engine_peer_config

__all__ = ["ENGINE", "engine_peer_config"]
