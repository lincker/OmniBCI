"""Exact plugin identity shared by profile and signal-preview requests."""
from pydantic import BaseModel, ConfigDict


class PluginSelectionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    package_id: str
    contribution_id: str
    version: str | None = None
