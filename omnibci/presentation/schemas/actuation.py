from typing import Optional
from pydantic import BaseModel, computed_field


class ActuationStatusResponse(BaseModel):
    """Live status of the Actuation subsystem."""
    active: bool
    process_alive: bool
    connected: bool
    engine_linked: bool
    effector: Optional[str] = None

    @computed_field(json_schema_extra={"deprecated": True})
    @property
    def engine_connected(self) -> bool:
        """Legacy name for the controller's IPC connection."""
        return self.connected
