from typing import Optional
from pydantic import BaseModel


class ActuationStatusResponse(BaseModel):
    """Live status of the Actuation subsystem."""
    active: bool
    process_alive: bool
    engine_connected: bool
    effector: Optional[str] = None
