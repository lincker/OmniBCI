"""HTTP values for offline package installation and installed inventory."""
from pydantic import BaseModel, Field

from omnibci.presentation.schemas.plugin_config import PluginKind, PluginSelectionResponse


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
    package_id: str
    version: str
    label: str
    description: str | None
    sha256: str | None
    contributions: list[PackageContributionResponse]
    enabled: bool
    available: bool
    error: str | None
    references: list[PackageReferenceResponse] = Field(default_factory=list)


class PackageInstallationResponse(BaseModel):
    package: PackageVersionResponse
    already_installed: bool
