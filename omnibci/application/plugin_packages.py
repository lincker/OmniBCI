"""Package-management values and storage port, independent of installation format."""
import base64
import hashlib
import re
from datetime import datetime
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Protocol

from omnibci.application.plugin_identity import PluginSelection
from omnibci.application.ports.plugin_configuration_catalog import PluginConfiguration


class PluginPackageError(RuntimeError):
    """A package operation could not safely complete."""


class PluginPackageNotFoundError(PluginPackageError):
    pass


class PluginPackageConflictError(PluginPackageError):
    pass


class PluginPackageStorageError(PluginPackageError):
    pass


@dataclass(frozen=True, slots=True)
class MarketVerification:
    """Private evidence accepted by the configured repository verifier."""
    repository_id: str
    key_id: str
    key_sha256: str
    accepted_at: str
    proof: bytes

    def __post_init__(self) -> None:
        if any(not isinstance(value, str) or not value or value != value.strip() or len(value) > 128
               for value in (self.repository_id, self.key_id)):
            raise ValueError("Invalid repository verification identity")
        if not isinstance(self.key_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", self.key_sha256):
            raise ValueError("Invalid repository key fingerprint")
        if not isinstance(self.proof, bytes) or not 0 < len(self.proof) <= 1024 * 1024:
            raise ValueError("Repository proof must be bounded bytes")
        if not isinstance(self.accepted_at, str) or len(self.accepted_at) > 40:
            raise ValueError("Invalid repository acceptance time")
        timestamp = datetime.fromisoformat(self.accepted_at)
        if timestamp.tzinfo is None or timestamp.utcoffset() is None:
            raise ValueError("Repository acceptance time requires a timezone")

    def to_dict(self) -> dict:
        return dict(repository_id=self.repository_id, key_id=self.key_id, key_sha256=self.key_sha256,
                    accepted_at=self.accepted_at, index_sha256=hashlib.sha256(self.proof).hexdigest(),
                    proof=base64.b64encode(self.proof).decode("ascii"))

    @classmethod
    def from_dict(cls, data: dict) -> "MarketVerification":
        if not isinstance(data, dict) or data.keys() != {
            "repository_id", "key_id", "key_sha256", "accepted_at", "index_sha256", "proof",
        }:
            raise ValueError("Invalid repository verification record")
        if not isinstance(data["proof"], str) or len(data["proof"]) > 4 * ((1024 * 1024 + 2) // 3):
            raise ValueError("Repository proof must be bounded Base64")
        proof = base64.b64decode(data["proof"], validate=True)
        if hashlib.sha256(proof).hexdigest() != data["index_sha256"]:
            raise ValueError("Repository proof digest differs from the accepted index")
        return cls(data["repository_id"], data["key_id"], data["key_sha256"], data["accepted_at"], proof)


@dataclass(frozen=True, slots=True)
class PackageReference:
    scope: str
    identifier: str
    selection: PluginSelection


@dataclass(frozen=True, slots=True)
class PackageContribution:
    selection: PluginSelection
    kind: str
    label: str
    available: bool


@dataclass(frozen=True, slots=True)
class PackageVersion:
    package_id: str
    version: str
    label: str
    description: str | None
    sha256: str | None
    contributions: tuple[PackageContribution, ...]
    domain: str | None
    enabled: bool = False
    available: bool = True
    error: str | None = None
    references: tuple[PackageReference, ...] = ()
    origin: str | None = None
    verified_repository: str | None = None


@dataclass(frozen=True, slots=True)
class PackageInstallation:
    package: PackageVersion
    already_installed: bool


class PluginPackageStore(Protocol):
    max_archive_bytes: int

    def install_archive(self, archive: Path, *, revalidate: Callable[[], MarketVerification] | None = None,
                        expected_sha256: str | None = None,
                        expected_package_id: str | None = None, expected_version: str | None = None,
                        expected_domain: str | None = None,
                        expected_contributions: Mapping[str, str] | None = None) -> PackageInstallation: ...
    def list_packages(self) -> list[PackageVersion]: ...
    def get_package(self, package_id: str, version: str) -> PackageVersion: ...
    def configuration(self, kind: str, selection: PluginSelection) -> PluginConfiguration: ...


class ManagedPluginPackageStore(PluginPackageStore, Protocol):
    def set_enabled(self, package_id: str, version: str, enabled: bool) -> PackageVersion: ...
    def remove(self, package_id: str, version: str) -> None: ...


def package_references_from_settings(settings: dict | None, scope: str = "running") -> tuple[PackageReference, ...]:
    """Adapt one pinned worker/apply snapshot without inferring runtime ownership."""
    if settings is None:
        return ()
    from omnibci.application.configuration.models import ProfileBinding
    return tuple(
        PackageReference(scope, binding.slot, binding.selection)
        for item in settings.get("PLUGINS.bindings", [])
        for binding in (ProfileBinding.from_dict(item),)
    )
