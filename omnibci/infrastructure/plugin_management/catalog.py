"""Plugin discovery, manifest catalog and deterministic runtime resolution.

This is the single read-only entry point for plugin metadata.  It resolves
trusted user/builtin roots, parses manifest.toml without importing plugin
Python code, applies precedence and identity rules, and exposes exact package
contributions for configuration and runtime resolution.

Executable loading remains isolated in :mod:`omnibci.infrastructure.plugin_management.loader`.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Literal, Optional

import omnibci
from omnibci.infrastructure.plugin_management.registry import enabled_package_directories
from omnibci.infrastructure.plugin_management.compatibility import installed_host_version

from omnibci.application.plugin_management.identity import PLUGIN_KINDS, PluginSelection
from omnibci.infrastructure.plugin_management.descriptors import (
    PluginDescriptor,
)

from omnibci.infrastructure.plugin_management.manifest import (
    MANIFEST_NAME, PluginManifestError, PluginResolutionError,
    parse_manifest, read_manifest,
)

log = logging.getLogger(__name__)

_APP_NAME = "OmniBCI"
_APP_AUTHOR = "OmniBCI"  # Windows roaming path segment


@dataclass(frozen=True, slots=True)
class PluginRoot:
    """A trusted search root and its catalog precedence.

    Source is assigned by application configuration, never by plugin
    manifests.  Lower precedence values control deterministic scan ordering;
    duplicate exact package versions are rejected rather than overridden.
    """

    path: Path
    source: Literal["user", "builtin"]
    precedence: int


def vendor_plugins_dir() -> Path:
    """Absolute path to the vendor (builtin) plugins directory."""
    pkg_root = Path(next(iter(omnibci.__path__)))
    return pkg_root / "infrastructure" / "plugins"


def user_plugins_dir(*, host_settings: dict | None = None) -> Optional[Path]:
    """Resolve the user plugins directory; return None if undeterminable.

    Resolution order
    ----------------
    1. ``OMNIBCI_PLUGIN_DIR`` environment variable (useful for CI / dev).
    2. ``PLUGINS.user_dir`` from explicitly supplied server startup settings.
    3. The platform-specific default from *platformdirs*:
         macOS   ~/Library/Application Support/OmniBCI/plugins
         Linux   ~/.local/share/OmniBCI/plugins          (XDG_DATA_HOME honoured)
         Windows %APPDATA%\\OmniBCI\\OmniBCI\\plugins

    The directory is **not created** here — callers that need to write should
    mkdir themselves.
    """
    # 1. Env override
    env = os.environ.get("OMNIBCI_PLUGIN_DIR")
    if env:
        return Path(env)

    # The composition root publishes this directory to child processes via env.
    raw = (host_settings or {}).get("PLUGINS.user_dir")
    if raw:
        return Path(raw)

    # 3. platformdirs (optional dependency — graceful degradation)
    try:
        from platformdirs import user_data_dir  # noqa: PLC0415
        return Path(user_data_dir(_APP_NAME, _APP_AUTHOR)) / "plugins"
    except ImportError:
        log.warning(
            "platformdirs not installed; user plugin directory unavailable. "
            "Install it with: pip install platformdirs"
        )
        return None


RootProvider = Callable[[], list[PluginRoot]]


class PluginNotFoundError(PluginResolutionError):
    """No plugin matches the configured alias or exact selection."""


class AmbiguousPluginError(PluginResolutionError):
    """More than one plugin has the same key at the winning precedence."""


def _parse_package(
    root: PluginRoot, package_dir: Path, host_version: str | None,
) -> tuple[PluginDescriptor, ...]:
    resolved_root = root.path.resolve()
    if not package_dir.resolve().is_relative_to(resolved_root):
        raise PluginManifestError(
            f"{package_dir}: plugin directory resolves outside search root {resolved_root}"
        )
    document = read_manifest(package_dir)
    return parse_manifest(document, package_dir, source=root.source,
                          precedence=root.precedence, host_version=host_version)


def _iter_plugin_dirs(directory: Path) -> Iterable[Path]:
    """Discover plugin packages without descending into package contents."""
    manifest = directory / MANIFEST_NAME
    if manifest.is_symlink() or manifest.exists():
        yield directory
        return
    try:
        children = sorted(directory.iterdir(), key=lambda child: child.name)
    except OSError as exc:
        log.warning("Cannot scan plugin directory %s: %s", directory, exc)
        return

    for child in children:
        if child.name.startswith("__"):
            continue
        if child.is_symlink():
            log.warning("Skipping symlink in plugin search tree: %s", child)
            continue
        if not child.is_dir():
            continue

        manifest_path = child / MANIFEST_NAME
        if manifest_path.is_symlink() or manifest_path.exists():
            yield child
            continue

        yield from _iter_plugin_dirs(child)


_BUILTIN_SUBPATHS: tuple[str, ...] = (
    "paradigms",
    "algorithms/decoders",
    "acquisition",
    "effectors",
    "algorithms/filters",
    "algorithms/epoch",
)


def _builtin_roots() -> list[PluginRoot]:
    base = vendor_plugins_dir()
    roots: list[PluginRoot] = []
    for subpath in _BUILTIN_SUBPATHS:
        vp = base / subpath
        if vp.is_dir():
            roots.append(PluginRoot(vp.resolve(), "builtin", 100))
    return roots


def _default_roots(plugin_dir: Path | None = None) -> list[PluginRoot]:
    """Return user enabled packages (read once) plus builtin roots."""
    roots: list[PluginRoot] = []
    user = plugin_dir if plugin_dir is not None else user_plugins_dir()
    if user is not None:
        roots.extend(PluginRoot(path, "user", 0) for path in enabled_package_directories(user))
    roots.extend(_builtin_roots())
    return roots


def _package_stat_key(package_dir: Path) -> tuple[tuple[str, int, int, int, int], ...] | None:
    """Stat manifest.toml and schema JSON files without following symlinks."""
    entries: list[tuple[str, int, int, int, int]] = []
    try:
        for child in sorted(package_dir.iterdir(), key=lambda item: item.name):
            if child.name == MANIFEST_NAME or child.suffix == ".json":
                st = child.lstat()
                entries.append((child.name, st.st_mode, st.st_ino, st.st_size, st.st_mtime_ns))
    except OSError:
        return None
    return tuple(entries)


class PluginCatalog:
    """Discover plugins and resolve exact package contributions.

    Discovery reads manifests only.  Python code is imported later by
    :class:`PluginLoader`, and only for the descriptor returned by ``resolve_selection``.
    """

    def __init__(
        self,
        root_providers: dict[str, RootProvider] | None = None,
        *, host_version: str | None = None, plugin_dir: Path | None = None,
    ) -> None:
        if root_providers is not None and plugin_dir is not None:
            raise ValueError("Choose explicit root providers or a plugin directory, not both")
        self.host_version = host_version if host_version is not None else installed_host_version()
        self._plugin_dir = Path(plugin_dir).resolve() if plugin_dir is not None else None
        self._root_providers = root_providers
        self._package_cache: dict[
            tuple[Path, str, int, str | None],
            tuple[tuple[tuple[str, int, int, int, int], ...], tuple[PluginDescriptor, ...]],
        ] = {}

    def _collect_roots(self) -> list[PluginRoot]:
        if self._root_providers is None:
            return _default_roots(self._plugin_dir)
        roots: list[PluginRoot] = []
        for provider in self._root_providers.values():
            roots.extend(provider())
        return roots

    def _scan_packages(self) -> tuple[tuple[PluginDescriptor, ...], tuple[PluginManifestError, ...]]:
        descriptors: list[PluginDescriptor] = []
        failures: list[PluginManifestError] = []

        # A package can contribute to any category. Scan all configured roots,
        # deduplicating physical packages even when roots overlap. Retain the
        # trusted builtin classification when a user root covers the same path.
        packages: dict[Path, PluginRoot] = {}
        for candidate in self._collect_roots():
            root = PluginRoot(candidate.path.resolve(), candidate.source, candidate.precedence)
            for package_dir in _iter_plugin_dirs(root.path):
                location = package_dir.resolve()
                existing = packages.get(location)
                if existing is None:
                    packages[location] = root
                    continue
                previous = existing
                if (root.source == "builtin" and previous.source != "builtin") or (
                    root.source == previous.source and root.precedence < previous.precedence
                ):
                    packages[location] = root

        active_keys: set[tuple[Path, str, int, str | None]] = set()
        for package_dir, root in sorted(
            packages.items(), key=lambda item: (item[1].precedence, str(item[0])),
        ):
            cache_key = (package_dir, root.source, root.precedence, self.host_version)
            active_keys.add(cache_key)
            stat_key = _package_stat_key(package_dir)
            cached = self._package_cache.get(cache_key)
            if stat_key is not None and cached is not None and cached[0] == stat_key:
                descriptors.extend(cached[1])
                continue
            try:
                contributions = _parse_package(root, package_dir, self.host_version)
                if stat_key is not None:
                    self._package_cache[cache_key] = (stat_key, contributions)
                descriptors.extend(contributions)
            except PluginManifestError as exc:
                self._package_cache.pop(cache_key, None)
                log.error("Skipping invalid plugin: %s", exc)
                failures.append(exc)
            except Exception as exc:  # defensive boundary for user data
                self._package_cache.pop(cache_key, None)
                error = PluginManifestError(
                    f"{package_dir / MANIFEST_NAME}: unexpected manifest validation error: {exc}"
                )
                log.exception("Skipping invalid plugin: %s", error)
                failures.append(error)

        for stale in set(self._package_cache) - active_keys:
            self._package_cache.pop(stale, None)

        return tuple(descriptors), tuple(failures)

    def _scan(
        self,
        category: str,
        *,
        all_categories: bool = False,
    ) -> tuple[tuple[PluginDescriptor, ...], tuple[PluginManifestError, ...]]:
        if category not in PLUGIN_KINDS:
            raise ValueError(
                f"Unknown plugin category: {category!r}. "
                f"Valid: {sorted(PLUGIN_KINDS)}"
            )
        descriptors, failures = self._scan_packages()
        if all_categories:
            return descriptors, failures
        return tuple(item for item in descriptors if item.category == category), failures

    def scan_all(self) -> tuple[PluginDescriptor, ...]:
        """Return valid contributions across all categories in one scan."""
        descriptors, _ = self._scan_packages()
        return descriptors

    def scan(self, category: str) -> tuple[PluginDescriptor, ...]:
        """Return valid contributions in deterministic package/manifest order."""
        descriptors, _ = self._scan(category)
        return descriptors

    @staticmethod
    def _resolve_selection(
        category: str,
        selection: PluginSelection,
        descriptors: tuple[PluginDescriptor, ...],
        failures: tuple[PluginManifestError, ...],
    ) -> PluginDescriptor:
        for failure in failures:
            if failure.package_id == selection.package_id and (
                failure.version is None or failure.version == selection.version
            ):
                raise PluginManifestError(
                    f"Invalid selected package {selection.package_id!r} "
                    f"version {selection.version!r}; refusing fallback: {failure}"
                ) from failure

        package = [
            item for item in descriptors
            if item.identifier == selection.package_id and item.version == selection.version
        ]
        locations = {item.location for item in package}
        if len(locations) > 1:
            raise AmbiguousPluginError(
                f"Ambiguous package {selection.package_id!r} version {selection.version!r}: "
                + ", ".join(str(path) for path in sorted(locations))
            )
        matches = [
            item for item in package
            if item.category == category and item.contribution_id == selection.contribution_id
        ]
        if not matches:
            raise PluginNotFoundError(f"Unknown {category} selection {selection!r}")
        return matches[0]

    def resolve_selection(self, category: str, selection: PluginSelection) -> PluginDescriptor:
        """Resolve one exact package, contribution and version without aliases.

        Distinct installations of the same package version are ambiguous even
        across root priorities. Installation/activation policy must resolve
        that conflict explicitly; no root priority may override the requested identity.
        """
        descriptors, failures = self._scan(category, all_categories=True)
        return self._resolve_selection(category, selection, descriptors, failures)

    def list_contributions(self, category: str) -> tuple[PluginDescriptor, ...]:
        """List exact, executable selections without collapsing registry aliases."""
        descriptors, failures = self._scan(category, all_categories=True)
        selections = dict.fromkeys(
            item.selection for item in descriptors if item.category == category
        )
        resolved: list[PluginDescriptor] = []
        for selection in selections:
            try:
                resolved.append(self._resolve_selection(category, selection, descriptors, failures))
            except PluginResolutionError as exc:
                log.error("Omitting unavailable contribution: %s", exc)
        return tuple(resolved)


def adapt_descriptor(descriptor: PluginDescriptor, platform_name: str):
    """Adapt a read-only plugin descriptor to application configuration metadata."""
    from copy import deepcopy
    from omnibci.application.plugin_management.catalog import PluginConfiguration

    return PluginConfiguration(
        selection=descriptor.selection,
        kind=descriptor.category,
        domain=descriptor.package.domain,
        label=descriptor.display_name,
        description=descriptor.description,
        schema=deepcopy(descriptor.configuration_schema),
        configuration_version=descriptor.configuration_version,
        available=descriptor.supports_platform(platform_name),
        controls=deepcopy(dict(descriptor.controls)),
        channel_requirements=descriptor.channel_requirements,
    )


class CatalogConfigurationAdapter:
    """Adapt read-only package descriptors to application configuration metadata."""

    def __init__(self, catalog: PluginCatalog | None = None, *, platform_name: str | None = None):
        import sys

        self.catalog = catalog or PluginCatalog()
        self.platform_name = platform_name if platform_name is not None else sys.platform

    def list(self, kind: str):
        return [adapt_descriptor(descriptor, self.platform_name) for descriptor in self.catalog.list_contributions(kind)]

    def get(self, kind: str, selection: PluginSelection):
        from omnibci.application.plugin_management.schema import PluginSchemaError

        try:
            return adapt_descriptor(self.catalog.resolve_selection(kind, selection), self.platform_name)
        except PluginResolutionError as exc:
            raise PluginSchemaError(str(exc)) from exc
