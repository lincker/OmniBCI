"""HTTP schemas for plugin package installation, inventory, lifecycle, and version-profile review."""
from typing import Any, Literal
from pydantic import BaseModel, ConfigDict, Field

from omnibci.presentation.schemas.plugin_config import PluginKind, PluginSelectionResponse
from omnibci.presentation.schemas.signal_setup import ProcessingInputModel


class PackageReferenceResponse(BaseModel):
    scope: str
    identifier: str
    selection: PluginSelectionResponse


class PackageContributionResponse(BaseModel):
    selection: PluginSelectionResponse
    kind: PluginKind
    label: str
    available: bool


class PackageVersionResponse(BaseModel):
    domain: str | None
    package_id: str
    version: str
    label: str
    description: str | None
    sha256: str | None
    origin: Literal["local", "market", "development"] | None
    verified_repository: str | None = Field(description="Repository accepted at installation; not a current online trust check")
    contributions: list[PackageContributionResponse]
    enabled: bool
    available: bool
    error: str | None
    references: list[PackageReferenceResponse] = Field(default_factory=list)


class PackageInstallationResponse(BaseModel):
    package: PackageVersionResponse
    already_installed: bool


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
    processing_input: ProcessingInputModel | None = None
