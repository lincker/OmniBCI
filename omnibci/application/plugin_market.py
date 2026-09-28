"""Signed repository query values and the verified-download installation port."""
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from omnibci.application.plugin_packages import PackageInstallation, PluginPackageError
from omnibci.application.plugin_profiles import PluginSelection


class PluginMarketError(PluginPackageError):
    """Repository trust, configuration or transport validation failed."""


@dataclass(frozen=True, slots=True)
class MarketContribution:
    kind: str
    selection: PluginSelection


@dataclass(frozen=True, slots=True)
class MarketPackage:
    repository_id: str
    package_id: str
    version: str
    display_name: str
    description: str
    download_url: str
    sha256: str
    size: int
    contributions: tuple[MarketContribution, ...]
    api_version: str | None = None
    platforms: tuple[str, ...] = ()
    host_version: str | None = None
    requires: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True, slots=True)
class PreparedMarketInstall:
    """Internal verified artifact; never accepted from an HTTP request."""
    package: MarketPackage
    archive: Path
    proof: bytes


class PluginMarketRepository(Protocol):
    def list_packages(self) -> tuple[MarketPackage, ...]: ...
    def prepare_install(self, package_id: str, version: str) -> PreparedMarketInstall: ...
    def install_prepared(self, prepared: PreparedMarketInstall) -> PackageInstallation: ...
