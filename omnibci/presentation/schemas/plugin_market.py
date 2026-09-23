"""Data-only market catalog and exact installation requests."""
from pydantic import BaseModel, ConfigDict, Field

from omnibci.presentation.schemas.plugin_config import PluginKind, PluginSelectionResponse


class MarketContributionResponse(BaseModel):
    kind: PluginKind
    selection: PluginSelectionResponse


class MarketPackageResponse(BaseModel):
    repository_id: str
    package_id: str
    version: str
    display_name: str
    description: str
    download_url: str
    sha256: str
    size: int
    contributions: list[MarketContributionResponse]
    api_version: str | None
    platforms: list[str]
    host_version: str | None
    requires: list[tuple[str, str]]


class InstallMarketPackageRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    package_id: str = Field(min_length=3, max_length=255)
    version: str = Field(min_length=1, max_length=128)
