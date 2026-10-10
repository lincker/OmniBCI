"""Explicit package enablement and version-profile review contracts."""
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from omnibci.presentation.schemas.plugin_config import PluginKind, PluginSelectionResponse
from omnibci.presentation.schemas.signal_setup import ProcessingInputModel, SourceMetadataModel


class SetPackageEnabledRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    enabled: bool


class ProfileVersionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    source_profile_id: str
    target_version: str = Field(min_length=1, max_length=128)
    name: str | None = Field(default=None, min_length=1, max_length=64)
    parameters: dict[str, Any] | None = None


class CreateVersionProfileRequest(ProfileVersionRequest):
    expected_source_revision: int = Field(ge=1)


class ProfileVersionProposalResponse(BaseModel):
    source_profile_id: str
    source_revision: int
    name: str
    kind: PluginKind
    selection: PluginSelectionResponse
    parameters: dict[str, Any]
    configuration_version: int
    target_enabled: bool
    manual_montage: SourceMetadataModel | None = None
    processing_input: ProcessingInputModel | None = None
