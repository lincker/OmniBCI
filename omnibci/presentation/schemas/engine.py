from pydantic import BaseModel


class EngineStatusResponse(BaseModel):
    running: bool
    acquisition_alive: bool
    processor_alive: bool
    stimulation_connected: bool
