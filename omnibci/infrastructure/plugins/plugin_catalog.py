"""Plugin discovery, manifest catalog and deterministic runtime resolution.

This is the single read-only entry point for plugin metadata.  It resolves
trusted user/builtin roots, parses manifest.toml without importing plugin
Python code, applies precedence and identity rules, and exposes a path-free
application DTO adapter for HTTP/config queries.

Executable loading remains isolated in :mod:`plugins.loader`.
"""

from __future__ import annotations

import logging
import os
import re
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Iterable, Literal, Optional

import omnibci
import tomlkit

from omnibci.infrastructure.plugins.descriptors import (
    PluginDescriptor,
    PluginEntrypoint,
    PluginPackageDescriptor,
    PluginSelection,
)

if TYPE_CHECKING:
    from omnibci.application.queries.plugin_queries import PluginInfo

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


def user_plugins_dir() -> Optional[Path]:
    """Resolve the user plugins directory; return None if undeterminable.

    Resolution order
    ----------------
    1. ``OMNIBCI_PLUGIN_DIR`` environment variable (useful for CI / dev).
    2. ``PLUGINS.user_dir`` setting from settings.toml.
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

    # 2. settings.toml override (lazy import to avoid circular dependency)
    try:
        from omnibci.conf import settings  # noqa: PLC0415
        raw = settings._raw_get(settings._data, "PLUGINS.user_dir")
        if raw:
            return Path(raw)
    except Exception:
        pass

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


def _plugin_roots(subpath: str) -> list[PluginRoot]:
    """Return trusted root descriptors in deterministic priority order."""
    roots: list[PluginRoot] = []
    user = user_plugins_dir()
    if user is not None:
        up = user / subpath
        if up.is_dir():
            roots.append(PluginRoot(up.resolve(), "user", 0))
    vp = vendor_plugins_dir() / subpath
    if vp.is_dir():
        roots.append(PluginRoot(vp.resolve(), "builtin", 100))
    return roots


def paradigm_plugin_roots() -> list[PluginRoot]:
    return _plugin_roots("paradigms")


def decoder_plugin_roots() -> list[PluginRoot]:
    return _plugin_roots("algorithms/decoders")


def acquisition_plugin_roots() -> list[PluginRoot]:
    return _plugin_roots("acquisition")


def effector_plugin_roots() -> list[PluginRoot]:
    return _plugin_roots("effectors")


def trigger_plugin_roots() -> list[PluginRoot]:
    return _plugin_roots("triggers")


def filter_plugin_roots() -> list[PluginRoot]:
    return _plugin_roots("algorithms/filters")


def epoch_detector_plugin_roots() -> list[PluginRoot]:
    return _plugin_roots("algorithms/epoch")


MANIFEST_NAME = "manifest.toml"
VALID_CATEGORIES = frozenset({
    "paradigm", "decoder", "acquisition", "effector", "trigger", "filter", "epoch_detector",
})
SUPPORTED_API_VERSIONS = frozenset({"1", "2"})

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


class PluginResolutionError(RuntimeError):
    """Base class for actionable plugin discovery/loading failures."""


class PluginManifestError(PluginResolutionError):
    """A manifest cannot produce a valid descriptor."""

    def __init__(self, message: str, *, plugin_type: str | None = None) -> None:
        super().__init__(message)
        self.plugin_type = plugin_type
        self.plugin_types: frozenset[str] = frozenset()
        self.package_id: str | None = None
        self.version: str | None = None


class PluginNotFoundError(PluginResolutionError):
    """No plugin matches the configured alias or exact selection."""


class AmbiguousPluginError(PluginResolutionError):
    """More than one plugin has the same key at the winning precedence."""


class PluginIdentityError(PluginResolutionError):
    """One publisher-qualified identity declares incompatible plugin types."""


class MissingEntrypointError(PluginResolutionError):
    """The selected legacy manifest does not declare executable code."""


@dataclass(frozen=True, slots=True)
class _ScanFailure:
    error: PluginManifestError
    precedence: int
    location: Path


def _table(
    document: Mapping,
    field: str,
    manifest_path: Path,
    *,
    required: bool = False,
    plugin_type: str | None = None,
) -> dict:
    value = document.get(field)
    if value is None:
        if required:
            raise PluginManifestError(
                f"{manifest_path}: missing [{field}] table",
                plugin_type=plugin_type,
            )
        return {}
    if not isinstance(value, Mapping):
        raise PluginManifestError(
            f"{manifest_path}: [{field}] must be a TOML table",
            plugin_type=plugin_type,
        )
    return dict(value)


def _required_string(
    package: Mapping,
    field: str,
    manifest_path: Path,
    *,
    plugin_type: str | None = None,
    section: str = "package",
) -> str:
    value = package.get(field)
    if not isinstance(value, str) or not value.strip():
        raise PluginManifestError(
            f"{manifest_path}: [{section}].{field} is required",
            plugin_type=plugin_type,
        )
    return value.strip()


def _optional_string(
    package: Mapping,
    field: str,
    manifest_path: Path,
    *,
    plugin_type: str | None = None,
    section: str = "package",
) -> str | None:
    value = package.get(field)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise PluginManifestError(
            f"{manifest_path}: [{section}].{field} must be a non-empty string",
            plugin_type=plugin_type,
        )
    return value.strip()


def _read_manifest(
    root: PluginRoot,
    package_dir: Path,
) -> Mapping:
    if package_dir.is_symlink():
        raise PluginManifestError(
            f"{package_dir}: plugin package directory must not be a symlink"
        )

    resolved_root = root.path.resolve()
    resolved_location = package_dir.resolve()
    if not resolved_location.is_relative_to(resolved_root):
        raise PluginManifestError(
            f"{package_dir}: plugin directory resolves outside search root "
            f"{resolved_root}"
        )

    manifest_path = resolved_location / MANIFEST_NAME
    if manifest_path.is_symlink():
        raise PluginManifestError(f"{manifest_path}: manifest must not be a symlink")
    try:
        document = tomlkit.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise PluginManifestError(f"{manifest_path}: cannot parse manifest: {exc}") from exc
    return document.unwrap()


def _parse_descriptor(
    category: str,
    root: PluginRoot,
    package_dir: Path,
    document: Mapping,
) -> PluginDescriptor:
    """Normalize the legacy single-entry manifest without changing its rules."""
    resolved_location = package_dir.resolve()
    manifest_path = resolved_location / MANIFEST_NAME

    package = _table(document, "package", manifest_path, required=True)
    plugin_type_hint = _optional_string(package, "plugin_type", manifest_path)
    plugin_id_hint = _optional_string(
        package,
        "id",
        manifest_path,
        plugin_type=plugin_type_hint,
    )
    effective_hint = plugin_type_hint or plugin_id_hint

    plugin_id = _required_string(
        package,
        "id",
        manifest_path,
        plugin_type=effective_hint,
    )
    declared_kind = _required_string(
        package,
        "kind",
        manifest_path,
        plugin_type=effective_hint,
    )
    if declared_kind != category:
        raise PluginManifestError(
            f"{manifest_path}: kind {declared_kind!r} does not match "
            f"search category {category!r}",
            plugin_type=effective_hint,
        )

    api_version = _optional_string(
        package,
        "api_version",
        manifest_path,
        plugin_type=effective_hint,
    ) or "1"
    if api_version not in SUPPORTED_API_VERSIONS:
        raise PluginManifestError(
            f"{manifest_path}: unsupported api_version {api_version!r}; "
            f"supported: {sorted(SUPPORTED_API_VERSIONS)}",
            plugin_type=effective_hint,
        )

    plugin_type = plugin_type_hint
    if plugin_type is None:
        plugin_type = plugin_id
        log.warning(
            "%s: missing [package].plugin_type; using id=%r for v1 compatibility",
            manifest_path,
            plugin_id,
        )

    publisher = (
        _optional_string(
            package,
            "publisher",
            manifest_path,
            plugin_type=plugin_type,
        )
        or _optional_string(
            package,
            "author",
            manifest_path,
            plugin_type=plugin_type,
        )  # v1 compatibility alias
        or ("omnibci" if root.source == "builtin" else "local")
    )
    if "publisher" not in package:
        log.warning(
            "%s: missing [package].publisher; using %r",
            manifest_path,
            publisher,
        )

    raw_entrypoint = package.get("entrypoint")
    if raw_entrypoint is None:
        # Also accept the more structured spelling for third-party packages.
        raw_entrypoint = _table(
            document,
            "runtime",
            manifest_path,
            plugin_type=plugin_type,
        ).get("entrypoint")
    entrypoint: PluginEntrypoint | None = None
    if raw_entrypoint is not None:
        try:
            entrypoint = PluginEntrypoint.parse(raw_entrypoint)
        except ValueError as exc:
            raise PluginManifestError(
                f"{manifest_path}: invalid entrypoint {raw_entrypoint!r}: {exc}",
                plugin_type=plugin_type,
            ) from exc

    params = _table(
        document,
        "params",
        manifest_path,
        plugin_type=plugin_type,
    )
    raw_controls = _table(
        document,
        "controls",
        manifest_path,
        plugin_type=plugin_type,
    )
    controls: dict[str, dict] = {}
    for key, value in raw_controls.items():
        if not isinstance(value, Mapping):
            raise PluginManifestError(
                f"{manifest_path}: [controls.{key}] must be a TOML table",
                plugin_type=plugin_type,
            )
        controls[str(key)] = dict(value)

    return PluginDescriptor(
        package=PluginPackageDescriptor(
            name=plugin_id,
            publisher=publisher,
            source=root.source,
            location=resolved_location,
            api_version=api_version,
            version=_optional_string(
                package, "version", manifest_path, plugin_type=plugin_type,
            ),
            precedence=root.precedence,
        ),
        contribution_id="default",
        category=category,
        plugin_type=plugin_type,
        entrypoint=entrypoint,
        display_name=_optional_string(
            package,
            "display_name",
            manifest_path,
            plugin_type=plugin_type,
        ) or plugin_id,
        description=_optional_string(
            package,
            "description",
            manifest_path,
            plugin_type=plugin_type,
        ),
        params=params,
        controls=controls,
    )


def _v2_identifier(
    table: Mapping, field: str, manifest_path: Path, *, section: str = "package",
) -> str:
    value = _required_string(table, field, manifest_path, section=section)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", value):
        raise PluginManifestError(
            f"{manifest_path}: invalid {field} {value!r}; use letters, digits, '_' or '-'"
        )
    return value


def _parse_contributions(
    root: PluginRoot, package_dir: Path, document: Mapping,
) -> tuple[PluginDescriptor, ...]:
    """Validate a v2 package completely before exposing any contribution."""
    manifest_path = package_dir / MANIFEST_NAME
    package = _table(document, "package", manifest_path, required=True)
    if any(key in package for key in ("kind", "plugin_type", "entrypoint")) or any(
        key in document for key in ("runtime", "params", "controls")
    ):
        raise PluginManifestError(
            f"{manifest_path}: v2 execution fields belong to contributions"
        )
    api_version = _required_string(package, "api_version", manifest_path)
    if api_version not in SUPPORTED_API_VERSIONS:
        raise PluginManifestError(
            f"{manifest_path}: unsupported api_version {api_version!r}"
        )
    metadata = PluginPackageDescriptor(
        name=_v2_identifier(package, "id", manifest_path),
        publisher=_v2_identifier(package, "publisher", manifest_path),
        source=root.source,
        location=package_dir.resolve(),
        api_version=api_version,
        version=_required_string(package, "version", manifest_path),
        manifest_version=2,
        platforms=_platforms(package, manifest_path),
        precedence=root.precedence,
    )
    contributions = document.get("contributions")
    if not isinstance(contributions, list) or not contributions:
        raise PluginManifestError(
            f"{manifest_path}: v2 requires a non-empty [[contributions]] array"
        )
    identifiers: set[str] = set()
    descriptors: list[PluginDescriptor] = []
    for contribution in contributions:
        if not isinstance(contribution, Mapping):
            raise PluginManifestError(f"{manifest_path}: contribution must be a TOML table")
        identifier = _v2_identifier(contribution, "id", manifest_path, section="contributions")
        if identifier in identifiers:
            raise PluginManifestError(
                f"{manifest_path}: duplicate contribution id {identifier!r}"
            )
        identifiers.add(identifier)
        category = _required_string(contribution, "kind", manifest_path, section="contributions")
        if category not in VALID_CATEGORIES:
            raise PluginManifestError(
                f"{manifest_path}: unknown contribution kind {category!r}"
            )
        raw_entrypoint = _required_string(
            contribution, "entrypoint", manifest_path, section="contributions",
        )
        try:
            entrypoint = PluginEntrypoint.parse(raw_entrypoint)
        except ValueError as exc:
            raise PluginManifestError(f"{manifest_path}: invalid entrypoint: {exc}") from exc
        controls = _table(contribution, "controls", manifest_path)
        if any(not isinstance(value, Mapping) for value in controls.values()):
            raise PluginManifestError(f"{manifest_path}: contribution controls must be TOML tables")
        descriptors.append(PluginDescriptor(
            package=metadata,
            contribution_id=identifier,
            category=category,
            plugin_type=_optional_string(
                contribution, "plugin_type", manifest_path, section="contributions",
            ) or identifier,
            entrypoint=entrypoint,
            display_name=_optional_string(
                contribution, "display_name", manifest_path, section="contributions",
            ) or identifier,
            description=_optional_string(
                contribution, "description", manifest_path, section="contributions",
            ),
            params=_table(contribution, "params", manifest_path),
            controls={key: dict(value) for key, value in controls.items()},
            platforms=_platforms(contribution, manifest_path),
        ))
    return tuple(descriptors)


def _platforms(table: Mapping, manifest_path: Path) -> tuple[str, ...]:
    if "platforms" not in table:
        return ()
    value = table["platforms"]
    if not isinstance(value, list) or not value or any(
        not isinstance(item, str) or item not in {"darwin", "linux", "win32"}
        for item in value
    ) or len(value) != len(set(value)):
        raise PluginManifestError(
            f"{manifest_path}: platforms must contain unique darwin, linux or win32 values"
        )
    return tuple(value)


def _parse_package(
    category: str, root: PluginRoot, package_dir: Path, legacy_categories: set[str],
) -> tuple[PluginDescriptor, ...]:
    document = _read_manifest(root, package_dir)
    manifest_path = package_dir / MANIFEST_NAME
    package = _table(document, "package", manifest_path, required=True)
    manifest_version = document.get("manifest_version", 1)
    try:
        if type(manifest_version) is not int or manifest_version not in (1, 2):
            raise PluginManifestError(
                f"{manifest_path}: unsupported manifest_version {manifest_version!r}"
            )
        if manifest_version == 2:
            return _parse_contributions(root, package_dir, document)
        if "contributions" in document:
            raise PluginManifestError(f"{manifest_path}: contributions require manifest_version = 2")
        declared_kind = package.get("kind")
        legacy_category = (
            declared_kind if isinstance(declared_kind, str) and declared_kind in legacy_categories
            else sorted(legacy_categories)[0]
        )
        return (_parse_descriptor(legacy_category, root, package_dir, document),)
    except PluginManifestError as exc:
        # Preserve identity hints even when a sibling contribution is invalid.
        # They prevent a broken package from exposing another copy as a fallback.
        publisher = package.get("publisher")
        if manifest_version == 1:
            publisher = publisher or package.get("author") or (
                "omnibci" if root.source == "builtin" else "local"
            )
        name, version = package.get("id"), package.get("version")
        if isinstance(publisher, str) and publisher.strip() and isinstance(name, str) and name.strip():
            exc.package_id = f"{publisher.strip()}.{name.strip()}"
        if isinstance(version, str) and version.strip():
            exc.version = version.strip()
        aliases: set[str] = set()
        if manifest_version == 1 and category in legacy_categories:
            alias = exc.plugin_type or package.get("plugin_type") or name
            if isinstance(alias, str):
                aliases.add(alias.strip())
        contributions = document.get("contributions", [])
        if isinstance(contributions, list):
            for contribution in contributions:
                if isinstance(contribution, Mapping) and contribution.get("kind") == category:
                    alias = contribution.get("plugin_type") or contribution.get("id")
                    if isinstance(alias, str):
                        aliases.add(alias.strip())
        exc.plugin_types = frozenset(aliases)
        raise


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
    """Discover plugins and resolve a configured ``plugin_type``.

    Discovery reads manifests only.  Python code is imported later by
    :class:`PluginLoader`, and only for the descriptor returned by ``resolve``.
    """

    def __init__(
        self,
        root_providers: dict[str, RootProvider] | None = None,
    ) -> None:
        self._root_providers = (
            root_providers if root_providers is not None else _ROOT_PROVIDERS
        )

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
        packages: dict[Path, tuple[PluginRoot, set[str]]] = {}
        for legacy_category, provider in self._root_providers.items():
            for candidate in provider():
                root = PluginRoot(candidate.path.resolve(), candidate.source, candidate.precedence)
                for package_dir in _iter_plugin_dirs(root.path):
                    location = package_dir.resolve()
                    existing = packages.get(location)
                    if existing is None:
                        packages[location] = (root, {legacy_category})
                        continue
                    previous, categories = existing
                    categories.add(legacy_category)
                    if (root.source == "builtin" and previous.source != "builtin") or (
                        root.source == previous.source and root.precedence < previous.precedence
                    ):
                        packages[location] = (root, categories)

        for package_dir, (root, legacy_categories) in sorted(
            packages.items(), key=lambda item: (item[1][0].precedence, str(item[0])),
        ):
            try:
                contributions = _parse_package(category, root, package_dir, legacy_categories)
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
        that conflict explicitly; directory precedence is only a legacy rule.
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

    @staticmethod
    def _blocking_failure(
        plugin_type: str,
        failures: tuple[_ScanFailure, ...],
        winning_precedence: int,
    ) -> _ScanFailure | None:
        matches = [
            failure
            for failure in failures
            if plugin_type in failure.error.plugin_types
            and failure.precedence <= winning_precedence
        ]
        return min(matches, key=lambda failure: failure.precedence) if matches else None

    @staticmethod
    def _identity_conflicts(
        descriptors: tuple[PluginDescriptor, ...],
    ) -> dict[tuple[str, str], list[PluginDescriptor]]:
        by_identifier: dict[tuple[str, str], list[PluginDescriptor]] = defaultdict(list)
        for descriptor in descriptors:
            if descriptor.package.manifest_version == 1:
                by_identifier[(descriptor.publisher, descriptor.name)].append(descriptor)
        return {
            identity: candidates
            for identity, candidates in by_identifier.items()
            if len({candidate.plugin_type for candidate in candidates}) > 1
        }

    @staticmethod
    def _pick(
        category: str,
        plugin_type: str,
        candidates: list[PluginDescriptor],
    ) -> PluginDescriptor:
        winning_precedence = min(candidate.precedence for candidate in candidates)
        winners = [
            candidate
            for candidate in candidates
            if candidate.precedence == winning_precedence
        ]
        if len(winners) != 1:
            locations = ", ".join(str(candidate.location) for candidate in winners)
            raise AmbiguousPluginError(
                f"Ambiguous {category} plugin_type {plugin_type!r} at precedence "
                f"{winning_precedence}: {locations}"
            )

        winner = winners[0]
        for shadowed in candidates:
            if shadowed is not winner:
                log.info(
                    "Plugin %s (%s) shadows %s",
                    winner.identifier,
                    winner.location,
                    shadowed.location,
                )
        return winner

    def list(self, category: str) -> tuple[PluginDescriptor, ...]:
        """Return runnable, precedence-resolved descriptors for UI/config use."""
        descriptors, failures = self._scan(category)
        conflicts = self._identity_conflicts(descriptors)
        for (publisher, plugin_id), candidates in conflicts.items():
            declarations = ", ".join(
                f"{candidate.plugin_type}@{candidate.location}"
                for candidate in candidates
            )
            log.error(
                "Omitting conflicting plugin identity %s: %s",
                f"{publisher}.{plugin_id}",
                declarations,
            )
        descriptors = tuple(
            descriptor
            for descriptor in descriptors
            if (descriptor.publisher, descriptor.name) not in conflicts
        )
        grouped: dict[str, list[PluginDescriptor]] = defaultdict(list)
        order: list[str] = []
        for descriptor in descriptors:
            if descriptor.plugin_type not in grouped:
                order.append(descriptor.plugin_type)
            grouped[descriptor.plugin_type].append(descriptor)

        resolved: list[PluginDescriptor] = []
        for plugin_type in order:
            winning_precedence = min(
                descriptor.precedence for descriptor in grouped[plugin_type]
            )
            failure = self._blocking_failure(
                plugin_type,
                failures,
                winning_precedence,
            )
            if failure is not None:
                log.error(
                    "Omitting plugin_type %r because a winning-precedence "
                    "candidate is invalid: %s",
                    plugin_type,
                    failure.error,
                )
                continue
            try:
                descriptor = self._pick(category, plugin_type, grouped[plugin_type])
            except AmbiguousPluginError as exc:
                log.error("Omitting ambiguous plugin from catalog: %s", exc)
                continue
            # Actuation is the first subsystem migrated to selected-entrypoint
            # loading. Other categories remain catalog-compatible with v1
            # manifests until their runtimes migrate in later batches.
            if category == "effector" and descriptor.entrypoint is None:
                log.error(
                    "Omitting non-runnable plugin %s: %s has no entrypoint",
                    descriptor.identifier,
                    descriptor.manifest_path,
                )
                continue
            resolved.append(descriptor)
        return tuple(resolved)

    def resolve(self, category: str, plugin_type: str) -> PluginDescriptor:
        """Compatibility entry point for callers using a runtime registry key."""
        return self.resolve_legacy(category, plugin_type)

    def resolve_legacy(self, category: str, plugin_type: str) -> PluginDescriptor:
        """Resolve a registry alias using the existing root precedence rules."""
        if not isinstance(plugin_type, str) or not plugin_type:
            raise PluginNotFoundError(
                f"No {category} plugin_type configured"
            )

        descriptors, failures = self._scan(category)
        conflicts = self._identity_conflicts(descriptors)
        eligible_descriptors = tuple(
            descriptor
            for descriptor in descriptors
            if (descriptor.publisher, descriptor.name) not in conflicts
        )
        matches = [
            descriptor
            for descriptor in eligible_descriptors
            if descriptor.plugin_type == plugin_type
        ]
        if not matches:
            for (publisher, plugin_id), candidates in conflicts.items():
                if any(
                    candidate.plugin_type == plugin_type
                    for candidate in candidates
                ):
                    declarations = ", ".join(
                        f"{candidate.plugin_type}@{candidate.location}"
                        for candidate in candidates
                    )
                    identifier = f"{publisher}.{plugin_id}"
                    raise PluginIdentityError(
                        f"Plugin identity {identifier!r} declares conflicting "
                        f"plugin types: {declarations}"
                    )
            invalid_matches = [
                failure
                for failure in failures
                if plugin_type in failure.error.plugin_types
            ]
            if invalid_matches:
                failure = min(
                    invalid_matches,
                    key=lambda candidate: candidate.precedence,
                )
                raise PluginManifestError(
                    f"Invalid {category} plugin_type {plugin_type!r} at "
                    f"{failure.location}: {failure.error}",
                    plugin_type=plugin_type,
                ) from failure.error
            raise PluginNotFoundError(
                f"Unknown {category} plugin_type {plugin_type!r}"
            )

        winning_precedence = min(descriptor.precedence for descriptor in matches)
        failure = self._blocking_failure(
            plugin_type,
            failures,
            winning_precedence,
        )
        if failure is not None:
            raise PluginManifestError(
                f"Invalid winning-precedence {category} plugin_type "
                f"{plugin_type!r} at {failure.location}; refusing fallback: "
                f"{failure.error}",
                plugin_type=plugin_type,
            ) from failure.error

        descriptor = self._pick(category, plugin_type, matches)
        if descriptor.entrypoint is None:
            raise MissingEntrypointError(
                f"{descriptor.manifest_path}: selected {category} plugin_type "
                f"{plugin_type!r} has no entrypoint"
            )
        return descriptor


def list_plugins(category: str) -> list["PluginInfo"]:
    """Return path-free, precedence-resolved plugins for UI/config queries."""
    # Keep the application DTO out of the runtime catalog's import path.
    from omnibci.application.queries.plugin_queries import PluginInfo

    return [
        PluginInfo(
            id=descriptor.name,
            plugin_type=descriptor.plugin_type,
            display_name=descriptor.display_name,
            description=descriptor.description,
            version=descriptor.version,
            source=descriptor.source,
            params=dict(descriptor.params),
            controls={
                key: dict(value)
                for key, value in descriptor.controls.items()
            },
        )
        for descriptor in PluginCatalog().list(category)
    ]
