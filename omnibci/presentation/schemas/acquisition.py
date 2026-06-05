from typing import Optional
from pydantic import BaseModel


class AcquisitionStatusResponse(BaseModel):
    connected: bool
    streaming: bool
    source: Optional[str] = None
    sample_rate: Optional[int] = None
    channel_count: Optional[int] = None
