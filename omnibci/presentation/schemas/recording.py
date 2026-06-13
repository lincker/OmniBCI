"""
Recording API schemas — the "record a run" orchestration (session + stream).
"""

from typing import Optional

from pydantic import BaseModel, Field


class StartRecordingRequest(BaseModel):
    paradigm: Optional[str] = Field(
        None, description="Paradigm name for the session; defaults to config value"
    )


class RecordingResponse(BaseModel):
    session_id: str = Field(..., description="The session created/ended for this recording")
