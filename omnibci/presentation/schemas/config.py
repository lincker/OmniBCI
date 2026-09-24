"""Saved host settings API schemas."""

from typing import Any, Dict, List

from pydantic import BaseModel, ConfigDict, Field


class ConfigEntry(BaseModel):
    key: str = Field(..., description="Dotted host settings key")
    value: Any = Field(..., description="Saved value; runtime application is separate")
    type: str = Field(..., description="int | float")
    requires: str = Field(..., description="engine-restart")
    description: str


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
    engine_restart_required: bool = Field(
        ..., description="Whether applying these saved changes requires an engine restart")
