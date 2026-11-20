"""HTTP values for offline package installation and installed inventory."""
from typing import Literal
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
    domain: str | None
    package_id: str
    version: str
    label: str
    description: str | None
    sha256: str | None
    origin: Literal["local", "market"] | None
    verified_repository: str | None = Field(description="Repository accepted at installation; not a current online trust check")
    contributions: list[PackageContributionResponse]
    enabled: bool
    available: bool
    error: str | None
    references: list[PackageReferenceResponse] = Field(default_factory=list)


class PackageInstallationResponse(BaseModel):
    package: PackageVersionResponse
    already_installed: bool
