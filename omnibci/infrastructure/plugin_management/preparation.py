"""Private, JSON-safe artifact handoff between local preparation and a host."""
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
import platform
import sys

from omnibci.application.configuration.models import ProfileBinding
from omnibci.application.configuration.definitions import MODULES
from omnibci.application.plugin_management.execution import PluginExecutionReceipt, fingerprint
from omnibci.application.plugin_management.identity import is_valid_sha256
from omnibci.application.plugin_management.packages import PluginPackageError
from omnibci.application.plugin_management.schema import validate_parameters
from omnibci.infrastructure.plugin_management.inventory import (
    artifact_inventory_with_stats, content_digest, verify_inventory_unchanged,
)
from omnibci.infrastructure.plugin_management.registry import InstalledPackageRegistry
from omnibci.infrastructure.plugin_management.loader import PluginLoadError
from omnibci.infrastructure.plugin_management.manifest import PluginResolutionError, parse_manifest, read_manifest
from omnibci.infrastructure.plugin_management.installer import PackageInstallError, PluginPackageInstaller
from omnibci.infrastructure.plugin_management.paths import managed_package_path
from omnibci.infrastructure.plugin_management.compatibility import (
    check_dependencies, installed_host_libraries, installed_host_version,
)
from omnibci.infrastructure.plugin_management.catalog import vendor_plugins_dir


_FIELDS = frozenset({
    'location', 'plugin_dir', 'content_sha256', 'environment_sha256', 'node_id', 'target',
    'origin', 'verified_repository', 'entrypoint', 'binding_sha256',
})
_FAILURES = (ValueError, TypeError, OSError, PluginResolutionError, PluginPackageError, PackageInstallError)


def configured_bindings(snapshot: dict, *, slot: str | None = None, modules=None) -> tuple[ProfileBinding, ...]:
    """Require explicit selections while leaving unrelated dormant inputs alone."""
    if 'PLUGINS.bindings' not in snapshot:
        raise PluginLoadError('Runtime configuration requires pinned PLUGINS.bindings')
    raw = snapshot['PLUGINS.bindings']
    if not isinstance(raw, list):
        raise PluginLoadError('PLUGINS.bindings must be a list')
    if any(not isinstance(value, dict) or not isinstance(value.get('slot'), str) for value in raw):
        raise PluginLoadError('Each plugin binding must name a slot')
    if len({value['slot'] for value in raw}) != len(raw):
        raise PluginLoadError('Duplicate plugin slot')
    return tuple(ProfileBinding.from_dict(value) for value in raw
                 if (slot is None or value['slot'] == slot)
                 and (modules is None or value['slot'].split('.', 1)[0] in modules))


def _environment() -> dict:
    return {
        'python': sys.version, 'implementation': sys.implementation.name,
        'abi': sys.implementation.cache_tag, 'platform': sys.platform,
        'target': f'{sys.platform}-{platform.machine().lower()}',
        'host_version': installed_host_version(), 'libraries': installed_host_libraries(),
    }


def _artifact_state(location, binding, plugin_dir, environment):
    if plugin_dir is None:
        if not location.is_relative_to(vendor_plugins_dir().resolve()):
            raise ValueError('Built-in origin requires host-distributed plugin content')
        origin, repository = 'builtin', None
        expected = None
    else:
        selection = binding.selection
        if location != managed_package_path(plugin_dir / 'packages', selection.package_id, selection.version):
            raise ValueError('Execution requires a managed package or isolated development snapshot')
        if (selection.package_id, selection.version) not in InstalledPackageRegistry(plugin_dir).read():
            raise ValueError('Selected package is not enabled on this node')
        installer = PluginPackageInstaller(plugin_dir / 'packages', host_version=environment['host_version'],
                                           host_dependencies=environment['libraries'])
        receipt = installer.read_receipt(location, selection.package_id, selection.version)
        expected = receipt['files']
        origin = receipt['origin']
        proof = receipt['market_verification']
        repository = proof['repository_id'] if proof is not None else None
    files, stats = artifact_inventory_with_stats(location)
    if expected is not None and files != expected:
        raise ValueError('Installed artifact inventory or content changed')
    return files, stats, content_digest(files), origin, repository


def _check_record(record):
    if not isinstance(record, dict) or record.keys() != _FIELDS:
        raise ValueError('Prepared artifact has missing or unknown fields')
    for name in _FIELDS - {'plugin_dir', 'verified_repository'}:
        if not isinstance(record[name], str) or not record[name]:
            raise ValueError(f'Invalid prepared artifact {name}')
    for name in ('content_sha256', 'environment_sha256', 'binding_sha256'):
        if not is_valid_sha256(record[name]):
            raise ValueError(f'Invalid prepared artifact {name}')
    for name in ('location', 'plugin_dir'):
        value = record[name]
        if value is None and name == 'plugin_dir':
            continue
        if not isinstance(value, str) or not Path(value).is_absolute() or str(Path(value).resolve()) != value:
            raise ValueError(f'Prepared artifact {name} must be a real absolute path')
    if (record['origin'] not in {'builtin', 'local', 'market', 'development'}
            or (record['origin'] == 'builtin') != (record['plugin_dir'] is None)):
        raise ValueError('Invalid prepared artifact origin')
    if record['verified_repository'] is not None and (
            not isinstance(record['verified_repository'], str) or not record['verified_repository']):
        raise ValueError('Invalid prepared repository identity')


def _verify_prepared_declarations(binding: ProfileBinding, record: dict, *,
                                  location: Path, environment: dict,
                                  files: dict[str, str], stats: dict[str, tuple[int, int, int]],
                                  digest: str, origin: str):
    descriptors = parse_manifest(
        read_manifest(location), location,
        source='builtin' if origin == 'builtin' else 'user',
        precedence=100 if origin == 'builtin' else 0,
        host_version=environment['host_version'],
    )
    descriptor = next((item for item in descriptors if item.selection == binding.selection
                       and item.category == binding.kind), None)
    if descriptor is None or str(descriptor.entrypoint) != record['entrypoint']:
        raise ValueError('Prepared artifact declaration changed')
    if not descriptor.supports_platform(sys.platform):
        raise ValueError(f'Selected contribution is not supported on {sys.platform}')
    if descriptor.configuration_version != binding.configuration_version:
        raise ValueError('Configuration schema migration required')
    check_dependencies(dict(descriptor.package.requires), environment['libraries'])
    validate_parameters(descriptor.configuration_schema, binding.parameters)
    # Detect edits while reading schemas/declarations, before any code import.
    if not verify_inventory_unchanged(location, files, stats):
        raise ValueError('Prepared artifact content changed while reading declarations')
    return replace(
        descriptor,
        package=replace(descriptor.package, content_sha256=digest, verified_files=files),
    )


def read_prepared_plugin(binding: ProfileBinding, record: dict):
    """Verify the pinned local path; never scan a catalog or consult a repository."""
    try:
        _check_record(record)
        if record['binding_sha256'] != fingerprint(binding.to_dict()):
            raise ValueError('Prepared artifact does not match this binding')
        environment = _environment()
        if record['node_id'] != platform.node():
            raise ValueError('Prepared artifact belongs to another node')
        if record['target'] != environment['target']:
            raise ValueError('Prepared artifact target changed')
        if record['environment_sha256'] != fingerprint(environment):
            raise ValueError('Prepared artifact environment changed')
        location = Path(record['location'])
        directory = Path(record['plugin_dir']) if record['plugin_dir'] is not None else None
        files, stats, digest, origin, _ = _artifact_state(location, binding, directory, environment)
        if digest != record['content_sha256'] or origin != record['origin']:
            raise ValueError('Prepared artifact content or origin changed')
        return _verify_prepared_declarations(
            binding, record, location=location, environment=environment,
            files=files, stats=stats, digest=digest, origin=origin,
        )
    except _FAILURES as exc:
        raise PluginLoadError(f'{binding.slot}: {binding.selection.package_id}@{binding.selection.version}: {exc}') from exc


class PluginArtifactPreparer:
    def __init__(self, store):
        self.store = store

    def prepare_snapshot(self, snapshot: dict, modules) -> dict:
        """Pin only this node's requested modules; retained pins are never refreshed."""
        modules = tuple(modules)
        if any(module not in MODULES for module in modules):
            raise PluginLoadError('Unknown plugin preparation module')
        result = deepcopy(snapshot)
        bindings = configured_bindings(result, modules=modules)
        existing = result.get('PLUGINS.artifacts', {})
        if not isinstance(existing, dict) or any(not isinstance(slot, str) for slot in existing):
            raise PluginLoadError('PLUGINS.artifacts must map slots to prepared artifacts')
        artifacts = {slot: record for slot, record in existing.items() if slot.split('.', 1)[0] not in modules}
        for binding in bindings:
            if binding.slot in existing:
                read_prepared_plugin(binding, existing[binding.slot])
                artifacts[binding.slot] = existing[binding.slot]
            else:
                artifacts[binding.slot] = self.prepare(binding)
        result['PLUGINS.artifacts'] = artifacts
        return result

    def prepare(self, binding: ProfileBinding) -> dict:
        """Resolve once at admission and bind static declarations to checked bytes."""
        try:
            descriptor = self.store.catalog.resolve_selection(binding.kind, binding.selection)
            if not descriptor.supports_platform(sys.platform):
                raise ValueError(f'Selected contribution is not supported on {sys.platform}')
            environment = _environment()
            check_dependencies(dict(descriptor.package.requires), environment['libraries'])
            directory = None if descriptor.source == 'builtin' else self.store.plugin_dir
            files, stats, digest, origin, repository = _artifact_state(
                descriptor.location, binding, directory, environment,
            )
            record = {
                'location': str(descriptor.location), 'plugin_dir': str(directory) if directory is not None else None,
                'content_sha256': digest, 'environment_sha256': fingerprint(environment),
                'node_id': platform.node(), 'target': environment['target'], 'origin': origin,
                'verified_repository': repository, 'entrypoint': str(descriptor.entrypoint),
                'binding_sha256': fingerprint(binding.to_dict()),
            }
            _check_record(record)
            verified = _verify_prepared_declarations(
                binding, record, location=descriptor.location, environment=environment,
                files=files, stats=stats, digest=digest, origin=origin,
            )
            if verified != descriptor:
                raise ValueError('Plugin declaration changed during preparation')
            return record
        except _FAILURES as exc:
            raise PluginLoadError(f'{binding.slot}: {exc}') from exc


def execution_receipt(snapshot: dict, module: str, operation_id: str) -> PluginExecutionReceipt:
    """Project the host's loaded pins; advertise only after its readiness barrier."""
    records = {}
    environment = None
    for binding in configured_bindings(snapshot, modules=(module,)):
        record = snapshot['PLUGINS.artifacts'][binding.slot]
        _check_record(record)
        if record['binding_sha256'] != fingerprint(binding.to_dict()):
            raise PluginLoadError('Execution artifact does not match its binding')
        identity = (record['node_id'], record['target'], record['environment_sha256'])
        if environment is not None and identity != environment:
            raise PluginLoadError('Execution artifacts must use one local environment')
        environment = identity
        records[binding.slot] = {key: record[key] for key in (
            'binding_sha256', 'content_sha256', 'origin', 'verified_repository',
        )}
    if environment is None:
        raise PluginLoadError('Execution requires owned plugin artifacts')
    return PluginExecutionReceipt(
        module, operation_id, snapshot['CONFIG.generation'], snapshot['CONFIG.module_revisions'][module],
        snapshot['CONFIG.engine_revision'], *environment, records,
    )
