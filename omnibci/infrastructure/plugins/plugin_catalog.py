"""Plugin discovery, manifest catalog and deterministic runtime resolution.

This is the single read-only entry point for plugin metadata.  It resolves
trusted user/builtin roots, parses manifest.toml without importing plugin
Python code, applies precedence and identity rules, and exposes exact package
contributions for configuration and runtime resolution.

Executable loading remains isolated in :mod:`plugins.loader`.
"""

from __future__ import annotations

import logging
import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Literal, Optional

import omnibci
import tomlkit

from omnibci.application.plugin_schema import validate_schema
from omnibci.domain.decoder_input import DecoderChannelRequirements
from omnibci.infrastructure.plugins.installed_registry import enabled_package_directories
from omnibci.infrastructure.plugins.package_requirements import check_host_compatibility, installed_host_version

from omnibci.infrastructure.plugins.descriptors import (
    PluginDescriptor,
    PluginEntrypoint,
    PluginPackageDescriptor,
    PluginSelection,
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
        raw = (settings.host_get("PLUGINS.user_dir") if host_settings is None
               else host_settings.get("PLUGINS.user_dir"))
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
        roots.extend(PluginRoot(path, "user", 0) for path in enabled_package_directories(user))
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
SUPPORTED_API_VERSIONS = frozenset({"2"})
DOMAIN_KINDS = {
    "acquisition": frozenset({"acquisition", "trigger"}),
    "processing": frozenset({"decoder", "epoch_detector", "filter"}),
    "stimulation": frozenset({"paradigm", "trigger"}),
    "actuation": frozenset({"effector"}),
}

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


class MissingEntrypointError(PluginResolutionError):
    """The selected contribution does not declare executable code."""


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
    root: PluginRoot, package_dir: Path, document: Mapping, host_version: str | None,
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
    domain = _required_string(package, "domain", manifest_path)
    if domain not in DOMAIN_KINDS:
        raise PluginManifestError(f"{manifest_path}: unsupported package domain {domain!r}")
    try:
        check_host_compatibility(package, host_version)
    except ValueError as exc:
        raise PluginManifestError(f"{manifest_path}: {exc}") from exc
    version = _required_string(package, "version", manifest_path)
    if not re.fullmatch(r"[0-9][A-Za-z0-9._+-]{0,127}", version) or version.endswith("."):
        raise PluginManifestError(f"{manifest_path}: package version must be an exact portable version")
    requirements = package.get("requires", {})
    if not isinstance(requirements, dict) or any(not isinstance(name, str) or not name
            or not isinstance(version, str) or not version for name, version in requirements.items()):
        raise PluginManifestError(f"{manifest_path}: invalid host library requirements")
    metadata = PluginPackageDescriptor(
        name=_v2_identifier(package, "id", manifest_path),
        publisher=_v2_identifier(package, "publisher", manifest_path),
        source=root.source,
        location=package_dir.resolve(),
        api_version=api_version,
        version=version,
        domain=domain, engines=dict(package.get("engines", {})),
        host_version=package.get("host_version"), requires=tuple(sorted(requirements.items())),
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
        if contribution.get("kind") not in DOMAIN_KINDS[domain]:
            raise PluginManifestError(f"{manifest_path}: contribution kind {contribution.get('kind')!r} is not allowed in {domain!r} domain")
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
        configuration_schema, configuration_version = _configuration(contribution, package_dir)
        signal_contract = contribution.get("signal_input_contract")
        processing_kind = category in {"decoder", "epoch_detector", "filter"}
        if (
            processing_kind and (type(signal_contract) is not int or signal_contract not in (1, 2))
            or not processing_kind and signal_contract is not None
            or signal_contract == 2 and category != "decoder"
        ):
            raise PluginManifestError(f"{manifest_path}: invalid signal_input_contract")
        requirements = None
        if "channel_requirements" in contribution:
            if category != "decoder" or signal_contract != 2:
                raise PluginManifestError(
                    f"{manifest_path}: channel_requirements requires a decoder with signal_input_contract = 2"
                )
            try:
                requirements = DecoderChannelRequirements.from_dict(contribution["channel_requirements"])
            except ValueError as exc:
                raise PluginManifestError(f"{manifest_path}: invalid channel_requirements: {exc}") from exc
        elif signal_contract == 2:
            raise PluginManifestError(f"{manifest_path}: signal_input_contract = 2 requires channel_requirements")
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
            configuration_schema=configuration_schema,
            configuration_version=configuration_version,
            signal_input_contract=signal_contract,
            channel_requirements=requirements,
        ))
    return tuple(descriptors)


def _configuration(contribution: Mapping, package_dir: Path) -> tuple[dict | None, int | None]:
    declaration = contribution.get("configuration")
    if declaration is None:
        return None, None
    try:
        if not isinstance(declaration, Mapping) or set(declaration) != {"schema", "schema_version"}:
            raise ValueError("configuration requires schema and schema_version")
        version = declaration["schema_version"]
        if type(version) is not int or version < 1:
            raise ValueError("configuration.schema_version must be a positive integer")
        name = declaration["schema"]
        if (not isinstance(name, str) or not name or "\\" in name
                or ":" in name or Path(name).is_absolute() or ".." in Path(name).parts):
            raise ValueError("configuration.schema must be a relative package path")
        path = package_dir / name
        if path.resolve() != path or not path.resolve().is_relative_to(package_dir.resolve()):
            raise ValueError("configuration.schema must not traverse symlinks")
        if path.stat().st_size > 1024 * 1024:
            raise ValueError("configuration schema exceeds 1 MiB")
        schema = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_unique_json_object)
        return validate_schema(schema), version
    except (OSError, ValueError, RecursionError) as exc:
        raise PluginManifestError(f"{package_dir / MANIFEST_NAME}: invalid configuration: {exc}") from exc


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate JSON key: {key}")
        result[key] = value
    return result


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
    category: str, root: PluginRoot, package_dir: Path, host_version: str | None,
) -> tuple[PluginDescriptor, ...]:
    document = _read_manifest(root, package_dir)
    manifest_path = package_dir / MANIFEST_NAME
    package = _table(document, "package", manifest_path, required=True)
    manifest_version = document.get("manifest_version")
    try:
        if type(manifest_version) is not int or manifest_version != 2:
            raise PluginManifestError(f"{manifest_path}: manifest_version must be 2")
        return _parse_contributions(root, package_dir, document, host_version)
    except PluginManifestError as exc:
        # Preserve identity hints even when a sibling contribution is invalid.
        # They prevent a broken package from exposing another copy as a fallback.
        publisher = package.get("publisher")
        name, version = package.get("id"), package.get("version")
        if isinstance(publisher, str) and publisher.strip() and isinstance(name, str) and name.strip():
            exc.package_id = f"{publisher.strip()}.{name.strip()}"
        if isinstance(version, str) and version.strip():
            exc.version = version.strip()
        aliases: set[str] = set()
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
    """Discover plugins and resolve exact package contributions.

    Discovery reads manifests only.  Python code is imported later by
    :class:`PluginLoader`, and only for the descriptor returned by ``resolve_selection``.
    """

    def __init__(
        self,
        root_providers: dict[str, RootProvider] | None = None,
        *, host_version: str | None = None,
    ) -> None:
        self.host_version = host_version if host_version is not None else installed_host_version()
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
                contributions = _parse_package(category, root, package_dir, self.host_version)
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
