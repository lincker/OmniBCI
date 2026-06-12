"""
Config API schemas — layered configuration (默认 + 个性化 profile).
"""
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class ConfigEntry(BaseModel):
    key: str = Field(..., description="Dotted settings key, e.g. 'ALGORITHM.decoder'")
    default_value: Any = Field(None, description="Value in settings.toml")
    profile_value: Any = Field(None, description="Override from the active profile (null = none)")
    effective_value: Any = Field(None, description="What the system actually uses")
    type: str = Field(..., description="str | int | float | bool | list[str]")
    requires: str = Field(..., description="hot | engine-restart")
    description: str


class ConfigResponse(BaseModel):
    entries: List[ConfigEntry]
    active_profile: Optional[str] = None


class PatchConfigRequest(BaseModel):
    updates: Dict[str, Any] = Field(..., description="Dotted key → new value")
    scope: str = Field(
        "default",
        description="'default' writes settings.toml; any other value names a "
                    "profile (created on first write)",
    )
    description: Optional[str] = Field(None, description="Profile description (profile scope only)")


class PatchConfigResponse(BaseModel):
    applied: List[str]
    scope: str
    engine_restart_required: bool = Field(
        ..., description="True → stop/start the engine for changes to take effect")


class ProfileSummary(BaseModel):
    name: str
    description: Optional[str] = None
    key_count: int
    updated_at: Optional[str] = None


class ProfileListResponse(BaseModel):
    profiles: List[ProfileSummary]
    active_profile: Optional[str] = None


class ProfileDetailResponse(BaseModel):
    name: str
    entries: Dict[str, Any]
    off_whitelist_keys: List[str] = Field(
        default_factory=list,
        description="Keys present in the file but outside the API whitelist "
                    "(hand-edited; read-only here)")


class PutProfileRequest(BaseModel):
    updates: Dict[str, Any]
    description: Optional[str] = None
