"""Read and validate package declarations without discovery or plugin imports."""
from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

import tomlkit

from omnibci.application.plugin_management.identity import (
    PACKAGE_DOMAINS, PLUGIN_KINDS, SUPPORTED_PLATFORMS, is_valid_identifier, is_valid_version,
)
from omnibci.application.plugin_management.schema import PluginSchemaError, validate_controls, validate_schema
from omnibci.domain.decoder_input import DecoderChannelRequirements
from omnibci.infrastructure.plugin_management.descriptors import (
    PluginDescriptor, PluginEntrypoint, PluginPackageDescriptor,
)
from omnibci.infrastructure.plugin_management.compatibility import check_host_compatibility
from omnibci.infrastructure.storage import strict_json_loads

MANIFEST_NAME = "manifest.toml"
SUPPORTED_API_VERSIONS = frozenset({"1"})
_PACKAGE_FIELDS = frozenset({
    "id", "publisher", "version", "api_version", "platforms", "host_version",
    "requires", "display_name", "description", "license", "homepage", "repository",
    "icon", "readme", "domain", "engines",
})
_CONTRIBUTION_FIELDS = frozenset({
    "id", "kind", "entrypoint", "plugin_type", "display_name", "description",
    "controls", "platforms", "configuration", "channel_requirements", "payload_schemas",
})


class PluginResolutionError(RuntimeError):
    """Base class for actionable plugin discovery/loading failures."""


class PluginManifestError(PluginResolutionError):
    """A manifest cannot produce a valid descriptor."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.package_id: str | None = None
        self.version: str | None = None


def read_manifest(package_dir: Path) -> Mapping:
    if package_dir.is_symlink():
        raise PluginManifestError(
            f"{package_dir}: plugin package directory must not be a symlink"
        )

    resolved_location = package_dir.resolve()
    manifest_path = resolved_location / MANIFEST_NAME
    if manifest_path.is_symlink():
        raise PluginManifestError(f"{manifest_path}: manifest must not be a symlink")
    try:
        document = tomlkit.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise PluginManifestError(f"{manifest_path}: cannot parse manifest: {exc}") from exc
    return document.unwrap()


def _table(
    document: Mapping,
    field: str,
    manifest_path: Path,
    *,
    required: bool = False,
) -> dict:
    value = document.get(field)
    if value is None:
        if required:
            raise PluginManifestError(f"{manifest_path}: missing [{field}] table")
        return {}
    if not isinstance(value, Mapping):
        raise PluginManifestError(f"{manifest_path}: [{field}] must be a TOML table")
    return dict(value)


def _required_string(
    package: Mapping,
    field: str,
    manifest_path: Path,
    *,
    section: str = "package",
) -> str:
    value = package.get(field)
    if not isinstance(value, str) or not value.strip():
        raise PluginManifestError(f"{manifest_path}: [{section}].{field} is required")
    return value.strip()


def _optional_string(
    package: Mapping,
    field: str,
    manifest_path: Path,
    *,
    section: str = "package",
) -> str | None:
    value = package.get(field)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise PluginManifestError(f"{manifest_path}: [{section}].{field} must be a non-empty string")
    return value.strip()


def _identifier(
    table: Mapping, field: str, manifest_path: Path, *, section: str = "package",
) -> str:
    value = _required_string(table, field, manifest_path, section=section)
    if not is_valid_identifier(value):
        raise PluginManifestError(
            f"{manifest_path}: invalid {field} {value!r}; use letters, digits, '_' or '-'"
        )
    return value


def _fields(table: Mapping, allowed: set[str] | frozenset[str], section: str, manifest_path: Path) -> None:
    unknown = set(table) - allowed
    if unknown:
        raise PluginManifestError(
            f"{manifest_path}: Unsupported {section} fields: {', '.join(sorted(unknown))}"
        )


def _parse_contributions(
    package_dir: Path, document: Mapping, *, source: str, precedence: int,
    host_version: str | None,
) -> tuple[PluginDescriptor, ...]:
    """Validate a manifest v1 package completely before exposing any contribution."""
    manifest_path = package_dir / MANIFEST_NAME
    package = _table(document, "package", manifest_path, required=True)
    if any(key in package for key in ("kind", "plugin_type", "entrypoint")) or any(
        key in document for key in ("runtime", "params", "controls")
    ):
        raise PluginManifestError(
            f"{manifest_path}: execution fields belong to contributions"
        )
    _fields(document, {"manifest_version", "package", "contributions"}, "manifest", manifest_path)
    _fields(package, _PACKAGE_FIELDS, "package", manifest_path)
    api_version = _required_string(package, "api_version", manifest_path)
    if api_version not in SUPPORTED_API_VERSIONS:
        raise PluginManifestError(
            f"{manifest_path}: unsupported api_version {api_version!r}"
        )
    domain = _required_string(package, "domain", manifest_path)
    if domain not in PACKAGE_DOMAINS:
        raise PluginManifestError(f"{manifest_path}: unsupported package domain {domain!r}")
    try:
        check_host_compatibility(package, host_version)
    except ValueError as exc:
        raise PluginManifestError(f"{manifest_path}: {exc}") from exc
    version = _required_string(package, "version", manifest_path)
    if not is_valid_version(version):
        raise PluginManifestError(f"{manifest_path}: package version must be an exact portable version")
    requirements = package.get("requires", {})
    if not isinstance(requirements, dict) or any(not isinstance(name, str) or not name
            or not isinstance(version, str) or not version for name, version in requirements.items()):
        raise PluginManifestError(f"{manifest_path}: invalid host library requirements")
    metadata = PluginPackageDescriptor(
        name=_identifier(package, "id", manifest_path),
        publisher=_identifier(package, "publisher", manifest_path),
        source=source,
        location=package_dir.resolve(),
        api_version=api_version,
        version=version,
        domain=domain, engines=dict(package.get("engines", {})),
        host_version=package.get("host_version"), requires=tuple(sorted(requirements.items())),
        manifest_version=1,
        platforms=_platforms(package, manifest_path),
        precedence=precedence,
    )
    contributions = document.get("contributions")
    if not isinstance(contributions, list) or not contributions:
        raise PluginManifestError(
            f"{manifest_path}: manifest requires a non-empty [[contributions]] array"
        )
    identifiers: set[str] = set()
    descriptors: list[PluginDescriptor] = []
    for contribution in contributions:
        if not isinstance(contribution, Mapping):
            raise PluginManifestError(f"{manifest_path}: contribution must be a TOML table")
        _fields(contribution, _CONTRIBUTION_FIELDS, "contribution", manifest_path)
        identifier = _identifier(contribution, "id", manifest_path, section="contributions")
        if identifier in identifiers:
            raise PluginManifestError(
                f"{manifest_path}: duplicate contribution id {identifier!r}"
            )
        identifiers.add(identifier)
        category = _required_string(contribution, "kind", manifest_path, section="contributions")
        if category not in PLUGIN_KINDS:
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
        try:
            validated_controls = validate_controls({key: dict(value) for key, value in controls.items()})
        except PluginSchemaError as exc:
            raise PluginManifestError(f"{manifest_path}: invalid controls: {exc}") from exc
        configuration_schema, configuration_version = _configuration(contribution, package_dir)
        requirements = None
        if category == "decoder":
            if "channel_requirements" not in contribution:
                raise PluginManifestError(f"{manifest_path}: decoder requires channel_requirements")
            try:
                requirements = DecoderChannelRequirements.from_dict(contribution["channel_requirements"])
            except ValueError as exc:
                raise PluginManifestError(f"{manifest_path}: invalid channel_requirements: {exc}") from exc
        elif "channel_requirements" in contribution:
            raise PluginManifestError(f"{manifest_path}: channel_requirements is only valid for a decoder")
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
            controls=validated_controls,
            platforms=_platforms(contribution, manifest_path),
            configuration_schema=configuration_schema,
            configuration_version=configuration_version,
            channel_requirements=requirements,
            payload_schemas=_payload_schemas(contribution, category, package_dir),
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
        return _schema(declaration["schema"], package_dir), version
    except (OSError, ValueError, RecursionError) as exc:
        raise PluginManifestError(f"{package_dir / MANIFEST_NAME}: invalid configuration: {exc}") from exc


def _payload_schemas(contribution: Mapping, kind: str, package_dir: Path) -> dict:
    expected = {"trial", "result"} if kind == "paradigm" else {"command"} if kind == "effector" else set()
    declaration = contribution.get("payload_schemas")
    try:
        if not expected:
            if "payload_schemas" in contribution:
                raise ValueError(f"{kind} does not accept payload_schemas")
            return {}
        if not isinstance(declaration, Mapping) or set(declaration) != expected:
            raise ValueError(f"{kind} requires exactly {sorted(expected)}")
        return {name: _schema(declaration[name], package_dir) for name in sorted(expected)}
    except (OSError, ValueError, RecursionError) as exc:
        raise PluginManifestError(f"{package_dir / MANIFEST_NAME}: invalid payload_schemas: {exc}") from exc


def _schema(name: object, package_dir: Path) -> dict:
    if (not isinstance(name, str) or not name or "\\" in name
            or ":" in name or Path(name).is_absolute() or ".." in Path(name).parts):
        raise ValueError("schema must be a relative package path")
    path = package_dir / name
    if path.resolve() != path or not path.resolve().is_relative_to(package_dir.resolve()):
        raise ValueError("schema must not traverse symlinks")
    if path.stat().st_size > 1024 * 1024:
        raise ValueError("schema exceeds 1 MiB")
    schema = strict_json_loads(path.read_text(encoding="utf-8"))
    return validate_schema(schema)


def resolve_entrypoint_path(
    location: Path,
    module_path: str,
    *,
    manifest_path: Path | None = None,
) -> tuple[Path, Path | None]:
    """Validate and resolve a dotted entrypoint relative to *location*."""
    prefix = f"{manifest_path}: " if manifest_path is not None else ""
    parts = module_path.split(".")
    parent_path = location
    for part in parts[:-1]:
        module_file = parent_path / f"{part}.py"
        package_dir = parent_path / part
        if module_file.exists() and package_dir.exists():
            raise ValueError(f"{prefix}ambiguous module prefix {part!r}")
        if module_file.exists() or not package_dir.is_dir():
            raise ValueError(f"{prefix}entrypoint prefix {part!r} is not a package")
        parent_path = package_dir

    leaf = parts[-1]
    module_file = parent_path / f"{leaf}.py"
    package_dir = parent_path / leaf
    package_file = package_dir / "__init__.py"
    if module_file.exists() and package_dir.exists():
        raise ValueError(
            f"{prefix}entrypoint module {module_path!r} is ambiguous "
            "(both module file and package directory exist)"
        )
    if not module_file.is_file() and not package_file.is_file():
        raise ValueError(f"{prefix}entrypoint module {module_path!r} does not exist under {location}")

    target = module_file if module_file.is_file() else package_file
    if not target.resolve().is_relative_to(location.resolve()):
        raise ValueError(f"{prefix}entrypoint resolves outside plugin directory")
    loaded_package_dir = package_dir if target == package_file else None
    return target, loaded_package_dir


def _platforms(table: Mapping, manifest_path: Path) -> tuple[str, ...]:
    if "platforms" not in table:
        return ()
    value = table["platforms"]
    if not isinstance(value, list) or not value or any(
        not isinstance(item, str) or item not in SUPPORTED_PLATFORMS
        for item in value
    ) or len(value) != len(set(value)):
        raise PluginManifestError(
            f"{manifest_path}: platforms must contain unique darwin, linux or win32 values"
        )
    return tuple(value)


def parse_manifest(
    document: Mapping, package_dir: Path, *, source: str, precedence: int,
    host_version: str | None,
) -> tuple[PluginDescriptor, ...]:
    """Validate the whole package before exposing any contribution."""
    manifest_path = package_dir / MANIFEST_NAME
    package = _table(document, "package", manifest_path, required=True)
    manifest_version = document.get("manifest_version")
    try:
        if type(manifest_version) is not int or manifest_version != 1:
            raise PluginManifestError(f"{manifest_path}: manifest_version must be 1")
        return _parse_contributions(package_dir, document, source=source,
                                    precedence=precedence, host_version=host_version)
    except PluginManifestError as exc:
        # Preserve identity hints even when a sibling contribution is invalid.
        # They prevent a broken package from exposing another copy as a fallback.
        publisher = package.get("publisher")
        name, version = package.get("id"), package.get("version")
        if isinstance(publisher, str) and publisher.strip() and isinstance(name, str) and name.strip():
            exc.package_id = f"{publisher.strip()}.{name.strip()}"
        if isinstance(version, str) and version.strip():
            exc.version = version.strip()
        raise
