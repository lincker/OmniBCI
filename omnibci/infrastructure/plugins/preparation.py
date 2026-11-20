"""Private, JSON-safe artifact handoff between local preparation and a host."""
import hashlib
import json
from pathlib import Path
import platform
import re
import sys

from omnibci.application.configuration.models import ProfileBinding
from omnibci.application.plugin_packages import PluginPackageError
from omnibci.application.plugin_schema import validate_parameters
from omnibci.infrastructure.plugins.artifact_inventory import artifact_inventory, content_digest
from omnibci.infrastructure.plugins.installed_registry import InstalledPackageRegistry
from omnibci.infrastructure.plugins.loader import PluginLoadError
from omnibci.infrastructure.plugins.manifest import PluginResolutionError, parse_manifest, read_manifest
from omnibci.infrastructure.plugins.package_installer import PackageInstallError, PluginPackageInstaller
from omnibci.infrastructure.plugins.package_paths import managed_package_path
from omnibci.infrastructure.plugins.package_requirements import (
    check_dependencies, installed_host_libraries, installed_host_version,
)
from omnibci.infrastructure.plugins.plugin_catalog import vendor_plugins_dir


_FIELDS = frozenset({
    'location', 'plugin_dir', 'content_sha256', 'environment_sha256', 'node_id', 'target',
    'origin', 'verified_repository', 'entrypoint', 'binding_sha256',
})
_FAILURES = (ValueError, TypeError, OSError, PluginResolutionError, PluginPackageError, PackageInstallError)


def _digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                     allow_nan=False).encode()).hexdigest()


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
    files = artifact_inventory(location)
    if expected is not None and files != expected:
        raise ValueError('Installed artifact inventory or content changed')
    return content_digest(files), origin, repository


def _check_record(record):
    if not isinstance(record, dict) or record.keys() != _FIELDS:
        raise ValueError('Prepared artifact has missing or unknown fields')
    for name in _FIELDS - {'plugin_dir', 'verified_repository'}:
        if not isinstance(record[name], str) or not record[name]:
            raise ValueError(f'Invalid prepared artifact {name}')
    for name in ('content_sha256', 'environment_sha256', 'binding_sha256'):
        if not re.fullmatch('[0-9a-f]{64}', record[name]):
            raise ValueError(f'Invalid prepared artifact {name}')
    for name in ('location', 'plugin_dir'):
        value = record[name]
        if value is None and name == 'plugin_dir':
            continue
        if not isinstance(value, str) or not Path(value).is_absolute() or str(Path(value).resolve()) != value:
            raise ValueError(f'Prepared artifact {name} must be a real absolute path')
    if (record['origin'] not in {'builtin', 'local', 'market'}
            or (record['origin'] == 'builtin') != (record['plugin_dir'] is None)):
        raise ValueError('Invalid prepared artifact origin')
    if record['verified_repository'] is not None and (
            not isinstance(record['verified_repository'], str) or not record['verified_repository']):
        raise ValueError('Invalid prepared repository identity')


def read_prepared_plugin(binding: ProfileBinding, record: dict):
    """Verify the pinned local path; never scan a catalog or consult a repository."""
    try:
        _check_record(record)
        if record['binding_sha256'] != _digest(binding.to_dict()):
            raise ValueError('Prepared artifact does not match this binding')
        environment = _environment()
        if record['node_id'] != platform.node():
            raise ValueError('Prepared artifact belongs to another node')
        if record['target'] != environment['target']:
            raise ValueError('Prepared artifact target changed')
        if record['environment_sha256'] != _digest(environment):
            raise ValueError('Prepared artifact environment changed')
        location = Path(record['location'])
        directory = Path(record['plugin_dir']) if record['plugin_dir'] is not None else None
        digest, origin, _ = _artifact_state(location, binding, directory, environment)
        if digest != record['content_sha256'] or origin != record['origin']:
            raise ValueError('Prepared artifact content or origin changed')
        descriptors = parse_manifest(read_manifest(location), location,
                                     source='builtin' if origin == 'builtin' else 'user',
                                     precedence=100 if origin == 'builtin' else 0,
                                     host_version=environment['host_version'])
        descriptor = next((item for item in descriptors if item.selection == binding.selection
                           and item.category == binding.kind), None)
        if descriptor is None or str(descriptor.entrypoint) != record['entrypoint']:
            raise ValueError('Prepared artifact declaration changed')
        if not descriptor.supports_platform(sys.platform):
            raise ValueError('Selected contribution is not supported on this platform')
        if descriptor.configuration_version != binding.configuration_version:
            raise ValueError('Configuration schema migration required')
        check_dependencies(dict(descriptor.package.requires), environment['libraries'])
        validate_parameters(descriptor.configuration_schema, binding.parameters)
        # Detect edits while reading schemas/declarations, before any code import.
        if content_digest(artifact_inventory(location)) != digest:
            raise ValueError('Prepared artifact content changed while reading declarations')
        return descriptor
    except _FAILURES as exc:
        raise PluginLoadError(f'{binding.slot}: {exc}') from exc


class PluginArtifactPreparer:
    def __init__(self, store):
        self.store = store

    def prepare(self, binding: ProfileBinding) -> dict:
        """Resolve once at admission and bind static declarations to checked bytes."""
        try:
            descriptor = self.store.catalog.resolve_selection(binding.kind, binding.selection)
            if not descriptor.supports_platform(sys.platform):
                raise ValueError('Selected contribution is not supported on this platform')
            environment = _environment()
            check_dependencies(dict(descriptor.package.requires), environment['libraries'])
            directory = None if descriptor.source == 'builtin' else self.store.plugin_dir
            digest, origin, repository = _artifact_state(descriptor.location, binding, directory, environment)
            record = {
                'location': str(descriptor.location), 'plugin_dir': str(directory) if directory is not None else None,
                'content_sha256': digest, 'environment_sha256': _digest(environment),
                'node_id': platform.node(), 'target': environment['target'], 'origin': origin,
                'verified_repository': repository, 'entrypoint': str(descriptor.entrypoint),
                'binding_sha256': _digest(binding.to_dict()),
            }
            if read_prepared_plugin(binding, record) != descriptor:
                raise ValueError('Plugin declaration changed during preparation')
            return record
        except _FAILURES as exc:
            raise PluginLoadError(f'{binding.slot}: {exc}') from exc
