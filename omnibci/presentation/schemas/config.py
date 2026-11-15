"""Backend startup settings and the values captured by the running backend."""

from typing import Any, Dict, List

from pydantic import BaseModel, ConfigDict, Field


class ConfigEntry(BaseModel):
    key: str = Field(..., description="Dotted host settings key")
    value: Any = Field(..., description="Saved value for the next backend startup")
    type: str = Field(..., description="string | float")
    requires: str = Field(..., description="backend-restart")
    description: str
    active_value: Any = Field(..., description="Value captured when this backend started")
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
    saved: List[str]
    revision: str = Field(..., description="Revision after saving the host settings")
    backend_restart_required: bool = Field(
        ..., description="Whether any saved backend settings differ from the startup snapshot")
