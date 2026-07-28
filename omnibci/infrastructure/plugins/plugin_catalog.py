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


MANIFEST_NAME = "manifest.toml"
VALID_CATEGORIES = frozenset({"paradigm", "decoder", "acquisition", "effector"})
SUPPORTED_API_VERSIONS = frozenset({"1"})

RootProvider = Callable[[], list[PluginRoot]]

_ROOT_PROVIDERS: dict[str, RootProvider] = {
    "paradigm": paradigm_plugin_roots,
    "decoder": decoder_plugin_roots,
    "acquisition": acquisition_plugin_roots,
    "effector": effector_plugin_roots,
}


class PluginResolutionError(RuntimeError):
    """Base class for actionable plugin discovery/loading failures."""


class PluginManifestError(PluginResolutionError):
    """A manifest cannot produce a valid descriptor."""

    def __init__(self, message: str, *, plugin_type: str | None = None) -> None:
        super().__init__(message)
        self.plugin_type = plugin_type


class PluginNotFoundError(PluginResolutionError):
    """No plugin matches the configured runtime plugin_type."""


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
) -> str:
    value = package.get(field)
    if not isinstance(value, str) or not value.strip():
        raise PluginManifestError(
            f"{manifest_path}: [package].{field} is required",
            plugin_type=plugin_type,
        )
    return value.strip()


def _optional_string(
    package: Mapping,
    field: str,
    manifest_path: Path,
    *,
    plugin_type: str | None = None,
) -> str | None:
    value = package.get(field)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise PluginManifestError(
            f"{manifest_path}: [package].{field} must be a non-empty string",
            plugin_type=plugin_type,
        )
    return value.strip()


def _parse_descriptor(
    category: str,
    root: PluginRoot,
    package_dir: Path,
) -> PluginDescriptor:
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
        category=category,
        name=plugin_id,
        plugin_type=plugin_type,
        publisher=publisher,
        source=root.source,
        location=resolved_location,
        entrypoint=entrypoint,
        api_version=api_version,
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
        version=_optional_string(
            package,
            "version",
            manifest_path,
            plugin_type=plugin_type,
        ),
        params=params,
        controls=controls,
        precedence=root.precedence,
    )


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
    ) -> tuple[tuple[PluginDescriptor, ...], tuple[_ScanFailure, ...]]:
        if category not in VALID_CATEGORIES:
            raise ValueError(
                f"Unknown plugin category: {category!r}. "
                f"Valid: {sorted(VALID_CATEGORIES)}"
            )

        descriptors: list[PluginDescriptor] = []
        failures: list[_ScanFailure] = []

        # Normalize order even for injected/custom providers. If the same
        # physical directory is configured as both user and builtin, retain
        # the trusted builtin classification instead of relabeling it user.
        roots_by_path: dict[Path, PluginRoot] = {}
        for candidate in self._root_providers[category]():
            root_path = candidate.path.resolve()
            normalized = PluginRoot(
                root_path,
                candidate.source,
                candidate.precedence,
            )
            existing = roots_by_path.get(root_path)
            if (
                existing is None
                or (
                    normalized.source == "builtin"
                    and existing.source != "builtin"
                )
                or (
                    normalized.source == existing.source
                    and normalized.precedence < existing.precedence
                )
            ):
                roots_by_path[root_path] = normalized

        roots = sorted(
            roots_by_path.values(),
            key=lambda root: (root.precedence, str(root.path)),
        )
        for root in roots:
            try:
                entries: Iterable[str] = sorted(os.listdir(root.path))
            except OSError as exc:
                log.warning("Cannot scan plugin root %s: %s", root.path, exc)
                continue

            for entry in entries:
                package_dir = root.path / entry
                if (
                    not package_dir.is_dir()
                    or entry.startswith("__")
                    or not (package_dir / MANIFEST_NAME).is_file()
                ):
                    continue
                try:
                    descriptors.append(_parse_descriptor(category, root, package_dir))
                except PluginManifestError as exc:
                    log.error("Skipping invalid plugin: %s", exc)
                    failures.append(_ScanFailure(exc, root.precedence, package_dir))
                except Exception as exc:  # defensive boundary for user data
                    error = PluginManifestError(
                        f"{package_dir / MANIFEST_NAME}: unexpected manifest "
                        f"validation error: {exc}"
                    )
                    log.exception("Skipping invalid plugin: %s", error)
                    failures.append(_ScanFailure(error, root.precedence, package_dir))

        return tuple(descriptors), tuple(failures)

    def scan(self, category: str) -> tuple[PluginDescriptor, ...]:
        """Return every valid manifest in deterministic root/path order."""
        descriptors, _ = self._scan(category)
        return descriptors

    @staticmethod
    def _blocking_failure(
        plugin_type: str,
        failures: tuple[_ScanFailure, ...],
        winning_precedence: int,
    ) -> _ScanFailure | None:
        matches = [
            failure
            for failure in failures
            if failure.error.plugin_type == plugin_type
            and failure.precedence <= winning_precedence
        ]
        return min(matches, key=lambda failure: failure.precedence) if matches else None

    @staticmethod
    def _identity_conflicts(
        descriptors: tuple[PluginDescriptor, ...],
    ) -> dict[tuple[str, str], list[PluginDescriptor]]:
        by_identifier: dict[tuple[str, str], list[PluginDescriptor]] = defaultdict(list)
        for descriptor in descriptors:
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
        """Resolve exactly one runnable descriptor by runtime ``plugin_type``."""
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
                if failure.error.plugin_type == plugin_type
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
