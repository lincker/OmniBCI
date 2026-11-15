"""Exact plugin identity shared by profile and signal-preview requests."""
from pydantic import BaseModel, ConfigDict, Field


class PluginSelectionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    package_id: str
    contribution_id: str
    version: str = Field(min_length=1, pattern=r"^[0-9][A-Za-z0-9._+\-]*$")
