"""Server startup settings and the values captured by the running server."""

from typing import Any, Dict, List, Literal

from pydantic import BaseModel, ConfigDict, Field


class ConfigEntry(BaseModel):
    key: str = Field(..., description="Dotted host settings key")
    value: Any = Field(..., description="Saved value for the next server startup")
    type: str = Field(..., description="string | float")
    requires: Literal["server-restart"]
    description: str
    active_value: Any = Field(..., description="Value captured when this server started")
    restart_required: bool = Field(..., description="Saved value differs from the startup snapshot")


class ConfigResponse(BaseModel):
    entries: List[ConfigEntry]
    revision: str = Field(..., description="Revision of the saved host settings")


class PatchConfigRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    updates: Dict[str, Any] = Field(..., description="Dotted host settings key to saved value")
    expected_revision: str = Field(
        ..., pattern=r"^[0-9a-f]{64}$", description="Saved revision returned by GET /config")


class PatchConfigResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    saved: List[str]
    revision: str = Field(..., description="Revision after saving the host settings")
    server_restart_required: bool = Field(
        ..., description="Whether any saved server settings differ from the startup snapshot")
