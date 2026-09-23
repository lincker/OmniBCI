"""Runtime descriptors for discovered OmniBCI plugins.

``PluginInfo`` is the application/query DTO exposed to the UI.  This module
contains the infrastructure-only contract used to resolve and execute a
plugin.  In particular, filesystem locations and Python entrypoints must never
be reconstructed later from a plugin id.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping


@dataclass(frozen=True, slots=True)
class PluginEntrypoint:
    """A Python object addressed relative to a plugin package directory."""

    module: str
    attribute: str

    @classmethod
    def parse(cls, value: str) -> "PluginEntrypoint":
        """Parse ``module:Attribute`` without accepting absolute/path imports."""
        if not isinstance(value, str):
            raise ValueError("entrypoint must be a string in 'module:Attribute' form")

        module, separator, attribute = value.strip().partition(":")
        if not separator or not module or not attribute:
            raise ValueError("entrypoint must be in 'module:Attribute' form")

        # Accept the earlier documented ``driver.py:Class`` spelling while
        # storing the canonical dotted-module form internally.
        if module.endswith(".py"):
            module = module[:-3]

        if any(char in module for char in ("/", "\\")):
            raise ValueError("entrypoint module must be dotted, not a file path")
        if any(not part.isidentifier() for part in module.split(".")):
            raise ValueError("entrypoint module must contain Python identifiers only")
        if not attribute.isidentifier():
            raise ValueError("entrypoint attribute must be a Python identifier")

        return cls(module=module, attribute=attribute)

    def __str__(self) -> str:
        return f"{self.module}:{self.attribute}"


@dataclass(frozen=True, slots=True)
class PluginSelection:
    """An exact capability selection; None means an unversioned legacy package.

    A missing version never means "latest" or permits another version.
    """

    package_id: str
    contribution_id: str
    version: str | None

    def __post_init__(self) -> None:
        for name in ("package_id", "contribution_id", "version"):
            value = getattr(self, name)
            if name == "version" and value is None:
                continue
            if not isinstance(value, str) or not value or value != value.strip():
                raise ValueError(f"{name} must be a non-empty, trimmed string")


@dataclass(frozen=True, slots=True)
class PluginPackageDescriptor:
    """Shared identity and trusted location of one installed package version.

    ``source``, ``location`` and ``precedence`` are derived from configured
    search roots.  A manifest cannot claim to be builtin or redirect loading
    to another directory.
    """

    name: str
    publisher: str
    source: str
    location: Path
    api_version: str
    version: str | None = None
    manifest_version: int = 1
    platforms: tuple[str, ...] = ()
    # Catalog metadata: lower values have higher priority.  It is intentionally
    # excluded from equality/repr because it is not plugin identity.
    precedence: int = field(default=0, repr=False, compare=False)

    @property
    def identifier(self) -> str:
        """Publisher-qualified package identity (not the runtime registry key).

        Publisher is manifest-declared metadata, not a verified signature or
        trust decision. ``source`` remains derived from the configured root.
        """
        return f"{self.publisher}.{self.name}"

    @property
    def manifest_path(self) -> Path:
        return self.location / "manifest.toml"


@dataclass(frozen=True, slots=True)
class PluginDescriptor:
    """One executable contribution, with shared package metadata.

    The legacy package properties remain available to existing loader and DTO
    callers. New selections use ``selection`` instead of the registry alias.
    """

    package: PluginPackageDescriptor
    contribution_id: str
    category: str
    plugin_type: str
    entrypoint: PluginEntrypoint | None
    display_name: str
    description: str | None = None
    params: Mapping[str, Any] = field(default_factory=dict)
    controls: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    platforms: tuple[str, ...] = ()

    configuration_schema: Mapping[str, Any] | None = None
    configuration_version: int | None = None

    def supports_platform(self, platform_name: str) -> bool:
        """Both package and contribution constraints must permit the host."""
        return (not self.package.platforms or platform_name in self.package.platforms) and (
            not self.platforms or platform_name in self.platforms
        )

    @property
    def selection(self) -> PluginSelection:
        return PluginSelection(self.identifier, self.contribution_id, self.version)

    @property
    def identifier(self) -> str:
        return self.package.identifier

    @property
    def name(self) -> str:
        return self.package.name

    @property
    def publisher(self) -> str:
        return self.package.publisher

    @property
    def source(self) -> str:
        return self.package.source

    @property
    def location(self) -> Path:
        return self.package.location

    @property
    def api_version(self) -> str:
        return self.package.api_version

    @property
    def version(self) -> str | None:
        return self.package.version

    @property
    def precedence(self) -> int:
        return self.package.precedence

    @property
    def manifest_path(self) -> Path:
        return self.package.manifest_path
