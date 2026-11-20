"""Plugin discovery, manifest catalog and deterministic runtime resolution.

This is the single read-only entry point for plugin metadata.  It resolves
trusted user/builtin roots, parses manifest.toml without importing plugin
Python code, applies precedence and identity rules, and exposes exact package
contributions for configuration and runtime resolution.

Executable loading remains isolated in :mod:`plugins.loader`.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Callable, Iterable, Literal, Optional

import omnibci
from omnibci.infrastructure.plugins.installed_registry import enabled_package_directories
from omnibci.infrastructure.plugins.package_requirements import installed_host_version

from omnibci.application.plugin_identity import PluginSelection
from omnibci.infrastructure.plugins.descriptors import (
    PluginDescriptor,
)

from omnibci.infrastructure.plugins.manifest import (
    MANIFEST_NAME, VALID_CATEGORIES, PluginManifestError, PluginResolutionError,
    parse_manifest, read_manifest,
)

log = logging.getLogger(__name__)

_APP_NAME = "OmniBCI"
_APP_AUTHOR = "OmniBCI"  # Windows roaming path segment


@dataclass(frozen=True, slots=True)
class PluginRoot:
    """A trusted search root and its catalog precedence.

    Source is assigned by application configuration, never by plugin
    manifests.  Lower precedence values win when a plugin_type appears in
    multiple roots.
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


def _plugin_roots(subpath: str, plugin_dir: Path | None = None) -> list[PluginRoot]:
    """Return trusted root descriptors in deterministic priority order."""
    roots: list[PluginRoot] = []
    user = plugin_dir if plugin_dir is not None else user_plugins_dir()
    if user is not None:
        up = user / subpath
        if up.is_dir():
            roots.append(PluginRoot(up.resolve(), "user", 0))
        roots.extend(PluginRoot(path, "user", 0) for path in enabled_package_directories(user))
    vp = vendor_plugins_dir() / subpath
    if vp.is_dir():
        roots.append(PluginRoot(vp.resolve(), "builtin", 100))
    return roots


def paradigm_plugin_roots(plugin_dir: Path | None = None) -> list[PluginRoot]:
    return _plugin_roots("paradigms", plugin_dir)


def decoder_plugin_roots(plugin_dir: Path | None = None) -> list[PluginRoot]:
    return _plugin_roots("algorithms/decoders", plugin_dir)


def acquisition_plugin_roots(plugin_dir: Path | None = None) -> list[PluginRoot]:
    return _plugin_roots("acquisition", plugin_dir)


def effector_plugin_roots(plugin_dir: Path | None = None) -> list[PluginRoot]:
    return _plugin_roots("effectors", plugin_dir)


def trigger_plugin_roots(plugin_dir: Path | None = None) -> list[PluginRoot]:
    return _plugin_roots("triggers", plugin_dir)


def filter_plugin_roots(plugin_dir: Path | None = None) -> list[PluginRoot]:
    return _plugin_roots("algorithms/filters", plugin_dir)


def epoch_detector_plugin_roots(plugin_dir: Path | None = None) -> list[PluginRoot]:
    return _plugin_roots("algorithms/epoch", plugin_dir)


RootProvider = Callable[[], list[PluginRoot]]

_ROOT_PROVIDERS: dict[str, RootProvider] = {
    "paradigm": paradigm_plugin_roots,
    "decoder": decoder_plugin_roots,
    "acquisition": acquisition_plugin_roots,
    "effector": effector_plugin_roots,
    "trigger": trigger_plugin_roots,
    "filter": filter_plugin_roots,
    "epoch_detector": epoch_detector_plugin_roots,
}


class PluginNotFoundError(PluginResolutionError):
    """No plugin matches the configured alias or exact selection."""


class AmbiguousPluginError(PluginResolutionError):
    """More than one plugin has the same key at the winning precedence."""


class MissingEntrypointError(PluginResolutionError):
    """The selected contribution does not declare executable code."""


@dataclass(frozen=True, slots=True)
class _ScanFailure:
    error: PluginManifestError
    precedence: int
    location: Path


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
        self._root_providers = (
            root_providers if root_providers is not None else _ROOT_PROVIDERS
        )
        if plugin_dir is not None:
            directory = Path(plugin_dir).resolve()
            self._root_providers = {kind: partial(provider, plugin_dir=directory)
                                    for kind, provider in _ROOT_PROVIDERS.items()}

    def _scan(
        self,
        category: str,
        *,
        all_categories: bool = False,
    ) -> tuple[tuple[PluginDescriptor, ...], tuple[_ScanFailure, ...]]:
        if category not in VALID_CATEGORIES:
            raise ValueError(
                f"Unknown plugin category: {category!r}. "
                f"Valid: {sorted(VALID_CATEGORIES)}"
            )

        descriptors: list[PluginDescriptor] = []
        failures: list[_ScanFailure] = []

        # A package can contribute to any category. Scan all configured roots,
        # deduplicating physical packages even when roots overlap. Retain the
        # trusted builtin classification when a user root covers the same path.
        packages: dict[Path, PluginRoot] = {}
        for provider in self._root_providers.values():
            for candidate in provider():
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

        for package_dir, root in sorted(
            packages.items(), key=lambda item: (item[1].precedence, str(item[0])),
        ):
            try:
                contributions = _parse_package(root, package_dir, self.host_version)
                descriptors.extend(
                    item for item in contributions if all_categories or item.category == category
                )
            except PluginManifestError as exc:
                log.error("Skipping invalid plugin: %s", exc)
                failures.append(_ScanFailure(exc, root.precedence, package_dir))
            except Exception as exc:  # defensive boundary for user data
                error = PluginManifestError(
                    f"{package_dir / MANIFEST_NAME}: unexpected manifest validation error: {exc}"
                )
                log.exception("Skipping invalid plugin: %s", error)
                failures.append(_ScanFailure(error, root.precedence, package_dir))

        return tuple(descriptors), tuple(failures)

    def scan(self, category: str) -> tuple[PluginDescriptor, ...]:
        """Return valid contributions in deterministic package/manifest order."""
        descriptors, _ = self._scan(category)
        return descriptors

    @staticmethod
    def _resolve_selection(
        category: str,
        selection: PluginSelection,
        descriptors: tuple[PluginDescriptor, ...],
        failures: tuple[_ScanFailure, ...],
    ) -> PluginDescriptor:
        for failure in failures:
            if failure.error.package_id == selection.package_id and (
                failure.error.version is None or failure.error.version == selection.version
            ):
                raise PluginManifestError(
                    f"Invalid selected package {selection.package_id!r} "
                    f"version {selection.version!r}; refusing fallback: {failure.error}"
                ) from failure.error

        domains = {item.package.domain for item in descriptors if item.identifier == selection.package_id}
        if len(domains) > 1:
            raise PluginManifestError(f"Package {selection.package_id!r} changes domain across versions")
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
        descriptor = matches[0]
        if descriptor.entrypoint is None:
            raise MissingEntrypointError(
                f"{descriptor.manifest_path}: selected contribution "
                f"{selection.contribution_id!r} has no entrypoint"
            )
        return descriptor

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
