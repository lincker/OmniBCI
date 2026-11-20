"""Preparation pins local bytes and environment without importing plugin code."""
from dataclasses import replace
import json
import shutil

import pytest

from omnibci.application.configuration.models import ProfileBinding
from omnibci.application.plugin_packages import PluginPackageError
from omnibci.infrastructure.plugins.loader import PluginLoadError
from omnibci.infrastructure.plugins.package_installer import PackageInstallError
from omnibci.infrastructure.plugins.package_lifecycle_store import ManagedPluginPackageStore
from tests.infrastructure.test_package_store import package_archive
from tests.plugin_configuration_helpers import builtin_selection


@pytest.fixture
def installed(tmp_path):
    store = ManagedPluginPackageStore(tmp_path / 'plugins')
    archive = package_archive(tmp_path)
    package = store.install_archive(archive).package
    store.set_enabled(package.package_id, package.version, True)
    binding = ProfileBinding('actuation.effector', 'effector', None,
                             package.contributions[0].selection, {'gain': 1.0})
    return store, binding, archive


def prepare(store, binding):
    from omnibci.infrastructure.plugins.preparation import PluginArtifactPreparer
    return PluginArtifactPreparer(store).prepare(binding)


def read(binding, record):
    from omnibci.infrastructure.plugins.preparation import read_prepared_plugin
    return read_prepared_plugin(binding, record)


def test_managed_preparation_and_json_handoff_are_static_and_offline(installed, monkeypatch):
    from omnibci.infrastructure.plugins.plugin_catalog import PluginCatalog
    store, binding, _ = installed
    record = prepare(store, binding)
    monkeypatch.setattr(PluginCatalog, 'resolve_selection', lambda *a, **k: pytest.fail('No second catalog search'))
    descriptor = read(binding, json.loads(json.dumps(record)))
    assert descriptor.selection == binding.selection
    assert descriptor.location == store.packages_dir / 'acme.device/1.0.0'
    assert record['origin'] == 'local' and record['verified_repository'] is None
    assert len(record['content_sha256']) == len(record['environment_sha256']) == 64
    assert record['entrypoint'] == 'driver:Driver'
    assert not list(descriptor.location.rglob('*.pyc'))


def test_identical_content_has_same_identity_in_different_stores(installed, tmp_path):
    store, binding, archive = installed
    other = ManagedPluginPackageStore(tmp_path / 'other')
    other.install_archive(archive)
    other.set_enabled(binding.selection.package_id, binding.selection.version, True)
    first, second = prepare(store, binding), prepare(other, binding)
    assert first['location'] != second['location']
    assert first['content_sha256'] == second['content_sha256']
    assert first['environment_sha256'] == second['environment_sha256']


def test_builtin_preparation_uses_distribution_content_without_upload_receipt(tmp_path):
    store = ManagedPluginPackageStore(tmp_path / 'plugins')
    binding = ProfileBinding('actuation.effector', 'effector', None,
                             builtin_selection('effector', 'logger'), {})
    record = prepare(store, binding)
    assert record['origin'] == 'builtin' and record['plugin_dir'] is None
    assert read(binding, record).source == 'builtin'
    assert not store.plugin_dir.exists()


@pytest.mark.parametrize('change', ['code', 'resource', 'schema', 'manifest', 'missing', 'extra'])
def test_changed_artifact_is_rejected_before_import(installed, change):
    store, binding, _ = installed
    record = prepare(store, binding)
    root = store.packages_dir / 'acme.device/1.0.0'
    if change == 'missing':
        (root / 'driver.py').unlink()
    else:
        path = {'code': 'driver.py', 'resource': 'model.bin', 'schema': 'configuration.schema.json',
                'manifest': 'manifest.toml', 'extra': 'injected.py'}[change]
        (root / path).write_text('changed')
    with pytest.raises(PluginLoadError, match='content|inventory'):
        read(binding, record)


@pytest.mark.parametrize('stage', ['prepare', 'consume'])
def test_disabled_package_cannot_be_prepared_or_consumed(installed, stage):
    store, binding, _ = installed
    record = prepare(store, binding) if stage == 'consume' else None
    store.set_enabled(binding.selection.package_id, binding.selection.version, False)
    with pytest.raises(PluginLoadError, match='enabled|Unknown'):
        read(binding, record) if stage == 'consume' else prepare(store, binding)


def test_prepared_binding_cannot_be_reused_after_parameter_or_identity_edit(installed):
    store, binding, _ = installed
    record = prepare(store, binding)
    with pytest.raises(PluginLoadError, match='binding'):
        read(replace(binding, parameters={'gain': 2.0}), record)
    with pytest.raises(PluginLoadError, match='binding'):
        read(replace(binding, selection=replace(binding.selection, version='2.0.0')), record)


@pytest.mark.parametrize('field', ['environment_sha256', 'node_id', 'target'])
def test_prepared_record_rejects_another_execution_environment(installed, field):
    store, binding, _ = installed
    record = prepare(store, binding)
    record[field] = '0' * 64 if field.endswith('sha256') else 'different'
    with pytest.raises(PluginLoadError, match='environment|node|target'):
        read(binding, record)


@pytest.mark.parametrize('edit', ['extra', 'missing', 'relative', 'bad_hash', 'source_claim'])
def test_prepared_record_has_a_closed_private_codec(installed, edit):
    store, binding, _ = installed
    record = prepare(store, binding)
    if edit == 'extra':
        record['context'] = {}
    elif edit == 'missing':
        record.pop('content_sha256')
    elif edit == 'relative':
        record['location'] = '../plugin'
    elif edit == 'bad_hash':
        record['content_sha256'] = True
    else:
        record['origin'] = 'builtin'
    with pytest.raises(PluginLoadError):
        read(binding, record)


@pytest.mark.parametrize('path', ['helper.py', 'resources/model.bin', '__pycache__/link.pyc'])
def test_symlinks_are_rejected_even_in_ignored_cache_paths(installed, tmp_path, path):
    store, binding, _ = installed
    record = prepare(store, binding)
    target = tmp_path / 'outside'
    target.write_text('outside')
    link = store.packages_dir / 'acme.device/1.0.0' / path
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(target)
    with pytest.raises(PluginLoadError, match='symlink'):
        read(binding, record)


def test_receipt_metadata_and_inert_bytecode_do_not_change_content_identity(installed):
    store, binding, _ = installed
    first = prepare(store, binding)
    root = store.packages_dir / 'acme.device/1.0.0'
    receipt = root / '.omnibci-install.json'
    receipt.write_text(json.dumps(json.loads(receipt.read_text()), indent=4))
    (root / '__pycache__').mkdir()
    (root / '__pycache__/driver.pyc').write_bytes(b'inert')
    assert prepare(store, binding)['content_sha256'] == first['content_sha256']
    assert read(binding, first).selection == binding.selection


def test_preparation_rechecks_declared_dependencies_in_the_actual_environment(tmp_path):
    from omnibci.infrastructure.plugins.package_installer import PluginPackageInstaller
    from tests.infrastructure.test_package_installer import _archive, _manifest
    directory = tmp_path / 'plugins'
    installer = PluginPackageInstaller(directory / 'packages', host_dependencies={'numpy': '0.0.0'})
    store = ManagedPluginPackageStore(directory, installer=installer)
    package = store.install_archive(_archive(tmp_path, manifest=_manifest(requires={'numpy': '0.0.0'}))).package
    store.set_enabled(package.package_id, package.version, True)
    binding = ProfileBinding('acquisition.source', 'acquisition', None,
                             package.contributions[0].selection, {})
    with pytest.raises(PluginLoadError, match='dependency'):
        prepare(store, binding)


def test_preparation_checks_platform_before_any_plugin_import(installed, monkeypatch):
    store, binding, _ = installed
    descriptor = store.catalog.resolve_selection(binding.kind, binding.selection)
    monkeypatch.setattr(store.catalog, 'resolve_selection', lambda *a: replace(descriptor, platforms=('unsupported',)))
    with pytest.raises(PluginLoadError, match='platform|supported'):
        prepare(store, binding)


def test_unmanaged_source_is_not_admitted_as_an_installed_artifact(installed, tmp_path, monkeypatch):
    store, binding, _ = installed
    descriptor = store.catalog.resolve_selection(binding.kind, binding.selection)
    root = tmp_path / 'source'
    shutil.copytree(descriptor.location, root)
    descriptor = replace(descriptor, package=replace(descriptor.package, location=root))
    monkeypatch.setattr(store.catalog, 'resolve_selection', lambda *a: descriptor)
    with pytest.raises(PluginLoadError, match='managed|snapshot'):
        prepare(store, binding)


@pytest.mark.parametrize('operation', ['enable', 'verify', 'reinstall'])
def test_extra_unrecorded_payload_cannot_pass_installation_integrity(installed, operation):
    store, binding, archive = installed
    (store.packages_dir / 'acme.device/1.0.0/injected.py').write_text('raise AssertionError')
    with pytest.raises(PluginPackageError, match='inventory|content'):
        if operation == 'enable':
            store.set_enabled(binding.selection.package_id, binding.selection.version, True)
        elif operation == 'verify':
            store.verify_integrity(binding.selection.package_id, binding.selection.version)
        else:
            store.install_archive(archive)


@pytest.mark.parametrize('name', ['driver.pyc', '__pycache__/driver.cpython.pyc'])
def test_installation_rejects_packaged_bytecode(installed, tmp_path, name):
    from tests.infrastructure.test_package_installer import _archive
    store, _, _ = installed
    archive = _archive(tmp_path, entries=[(name, b'bytecode')])
    with pytest.raises(PackageInstallError, match='bytecode|cache'):
        store.installer.install(archive)


def test_environment_drift_is_detected_after_preparation(installed, monkeypatch):
    from omnibci.infrastructure.plugins import preparation
    store, binding, _ = installed
    record = prepare(store, binding)
    libraries = preparation.installed_host_libraries()
    monkeypatch.setattr(preparation, 'installed_host_libraries', lambda: {**libraries, 'numpy': '0.0.0'})
    with pytest.raises(PluginLoadError, match='environment changed'):
        read(binding, record)


def test_edit_during_static_declaration_read_does_not_escape_verification(installed, monkeypatch):
    from omnibci.infrastructure.plugins import preparation
    store, binding, _ = installed
    record = prepare(store, binding)
    parse = preparation.parse_manifest
    def edit(*args, **kwargs):
        descriptors = parse(*args, **kwargs)
        (descriptors[0].location / 'driver.py').write_text('changed during preparation')
        return descriptors
    monkeypatch.setattr(preparation, 'parse_manifest', edit)
    with pytest.raises(PluginLoadError, match='content changed'):
        read(binding, record)


def test_accepted_market_evidence_is_retained_without_refreshing_at_preparation(installed):
    from omnibci.application.plugin_packages import MarketVerification
    store, binding, _ = installed
    path = store.packages_dir / 'acme.device/1.0.0/.omnibci-install.json'
    receipt = json.loads(path.read_text())
    receipt.update(origin='market', market_verification=MarketVerification(
        'example.plugins', 'test-key', 'a' * 64, '2025-01-01T00:00:00+00:00', b'expired accepted index',
    ).to_dict())
    path.write_text(json.dumps(receipt))
    record = prepare(store, binding)
    assert record['origin'] == 'market' and record['verified_repository'] == 'example.plugins'
    assert read(binding, record).selection == binding.selection
    assert path.read_text() == json.dumps(receipt)
