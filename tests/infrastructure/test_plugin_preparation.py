"""Preparation pins local bytes and environment without importing plugin code."""
from dataclasses import replace
import json
import shutil

import pytest

from omnibci.application.configuration.models import ProfileBinding
from omnibci.application.plugin_management.packages import PluginPackageError
from omnibci.infrastructure.plugin_management.loader import PluginLoadError
from omnibci.infrastructure.plugin_management.installer import PackageInstallError
from omnibci.infrastructure.plugin_management.store import ManagedPluginPackageStore
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
    from omnibci.infrastructure.plugin_management.preparation import PluginArtifactPreparer
    return PluginArtifactPreparer(store).prepare(binding)


def read(binding, record):
    from omnibci.infrastructure.plugin_management.preparation import read_prepared_plugin
    return read_prepared_plugin(binding, record)


def test_execution_receipt_exports_only_owned_pins_without_local_paths(installed):
    from omnibci.infrastructure.plugin_management.preparation import execution_receipt
    from omnibci.application.plugin_management.execution import bindings_fingerprint

    store, binding, _ = installed
    record = prepare(store, binding)
    snapshot = {"PLUGINS.bindings": [binding.to_dict()], "PLUGINS.artifacts": {binding.slot: record},
                "CONFIG.generation": "run-1", "CONFIG.module_revisions": {"actuation": "a" * 64},
                "CONFIG.engine_revision": "b" * 64}
    receipt = execution_receipt(snapshot, "actuation", "prepare-1")
    assert receipt.artifacts[binding.slot]["content_sha256"] == record["content_sha256"]
    assert receipt.environment_sha256 == record["environment_sha256"]
    assert receipt.bindings_sha256 == bindings_fingerprint(snapshot["PLUGINS.bindings"], "actuation")
    assert "location" not in json.dumps(receipt.to_dict())
    assert str(store.plugin_dir) not in json.dumps(receipt.to_dict())


def test_managed_preparation_and_json_handoff_are_static_and_offline(installed, monkeypatch):
    from omnibci.infrastructure.plugin_management.catalog import PluginCatalog
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
    from omnibci.infrastructure.plugin_management.installer import PluginPackageInstaller
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
    from omnibci.infrastructure.plugin_management import preparation
    store, binding, _ = installed
    record = prepare(store, binding)
    libraries = preparation.installed_host_libraries()
    monkeypatch.setattr(preparation, 'installed_host_libraries', lambda: {**libraries, 'numpy': '0.0.0'})
    with pytest.raises(PluginLoadError, match='environment changed'):
        read(binding, record)


def test_edit_during_static_declaration_read_does_not_escape_verification(installed, monkeypatch):
    from omnibci.infrastructure.plugin_management import preparation
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
    from omnibci.application.plugin_management.packages import MarketVerification
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


def test_prepare_and_load_bundle_hash_artifact_once_per_stage(tmp_path, monkeypatch):
    from omnibci.domain.ports.effector import Effector
    from omnibci.infrastructure.plugin_management import inventory as inventory_mod
    from omnibci.infrastructure.plugin_management import loader as loader_mod
    from omnibci.infrastructure.plugin_management import preparation
    from omnibci.infrastructure.plugin_management.runtime import load_configured_plugin_bundle

    store = ManagedPluginPackageStore(tmp_path / 'plugins')
    code = """from omnibci.sdk import Effector
class Driver(Effector):
    def __init__(self, *, parameters, resource_root):
        self.parameters = parameters
    def setup(self): pass
    def execute(self, command): pass
    def quiesce(self): pass
    def teardown(self): pass
"""
    package = store.install_archive(package_archive(tmp_path, code=code)).package
    store.set_enabled(package.package_id, package.version, True)
    binding = ProfileBinding('actuation.effector', 'effector', None,
                             package.contributions[0].selection, {'gain': 1.0})
    calls = 0
    original_with_stats = inventory_mod.artifact_inventory_with_stats
    original_inventory = inventory_mod.artifact_inventory

    def count_with_stats(location):
        nonlocal calls
        calls += 1
        return original_with_stats(location)

    def count_inventory(location):
        nonlocal calls
        calls += 1
        return original_inventory(location)

    monkeypatch.setattr(preparation, 'artifact_inventory_with_stats', count_with_stats)
    monkeypatch.setattr(loader_mod, 'artifact_inventory', count_inventory)

    record = prepare(store, binding)
    assert calls == 1

    calls = 0
    snapshot = {
        'PLUGINS.bindings': [binding.to_dict()],
        'PLUGINS.artifacts': {binding.slot: record},
    }
    loaded = load_configured_plugin_bundle(snapshot, binding.slot, 'effector', Effector)
    assert calls == 1
    assert isinstance(loaded.instance, Effector)
    assert 'command' in loaded.payload_schemas


def _rt_snapshot(binding):
    return {"PLUGINS.bindings": [binding.to_dict()], "CONFIG.generation": "run",
            "CONFIG.module_revisions": {"actuation": "revision"}}


def _pinned(store, binding):
    from omnibci.infrastructure.plugin_management.preparation import PluginArtifactPreparer

    result = _rt_snapshot(binding)
    result["PLUGINS.artifacts"] = {binding.slot: PluginArtifactPreparer(store).prepare(binding)}
    return result


def test_host_consumes_prepared_json_without_global_catalog_search(installed, monkeypatch):
    from unittest.mock import Mock
    from omnibci.domain.ports.effector import Effector
    from omnibci.infrastructure.plugin_management.catalog import PluginCatalog
    from omnibci.infrastructure.plugin_management.runtime import load_configured_plugin

    store, binding, _ = installed
    prepared = _pinned(store, binding)
    monkeypatch.setattr(PluginCatalog, "resolve_selection", lambda *a: pytest.fail("Global rediscovery"))
    loader = Mock()
    load_configured_plugin(json.loads(json.dumps(prepared)), binding.slot, binding.kind, Effector, loader=loader)
    descriptor = loader.instantiate.call_args.args[0]
    assert descriptor.location == store.packages_dir / "acme.device/1.0.0"
    assert loader.instantiate.call_args.kwargs == {"parameters": {"gain": 1.0}}


def test_runtime_rejects_unprepared_binding_even_when_the_builtin_is_installed():
    from unittest.mock import Mock
    from omnibci.domain.ports.effector import Effector
    from omnibci.infrastructure.plugin_management.runtime import load_configured_plugin

    binding = ProfileBinding("actuation.effector", "effector", None, builtin_selection("effector", "logger"), {})
    loader = Mock()
    with pytest.raises(PluginLoadError, match="prepared artifact"):
        load_configured_plugin(_rt_snapshot(binding), binding.slot, binding.kind, Effector, loader=loader)
    loader.instantiate.assert_not_called()


@pytest.mark.parametrize("change", ["code", "parameters", "removed_pin"])
def test_stale_preparation_never_reaches_constructor(installed, monkeypatch, change):
    from unittest.mock import Mock
    from omnibci.domain.ports.effector import Effector
    from omnibci.infrastructure.plugin_management.runtime import load_configured_plugin

    store, binding, _ = installed
    prepared = _pinned(store, binding)
    monkeypatch.setenv("OMNIBCI_PLUGIN_DIR", str(store.plugin_dir))
    if change == "code":
        (store.packages_dir / "acme.device/1.0.0/driver.py").write_text("changed")
    elif change == "parameters":
        prepared["PLUGINS.bindings"][0]["parameters"]["gain"] = 2.0
    else:
        prepared["PLUGINS.artifacts"].clear()
    loader = Mock()
    with pytest.raises(PluginLoadError):
        load_configured_plugin(prepared, binding.slot, binding.kind, Effector, loader=loader)
    loader.instantiate.assert_not_called()


def test_snapshot_preparation_is_scoped_copied_and_reuses_pins_without_rediscovery(installed, monkeypatch):
    from copy import deepcopy
    from omnibci.infrastructure.plugin_management.catalog import PluginCatalog
    from omnibci.infrastructure.plugin_management.preparation import PluginArtifactPreparer

    store, binding, _ = installed
    candidate = _rt_snapshot(binding)
    candidate["PLUGINS.bindings"].append({"slot": "processing.decoder", "invalid": "dormant"})
    original = deepcopy(candidate)
    preparer = PluginArtifactPreparer(store)
    prepared = preparer.prepare_snapshot(candidate, ("actuation",))
    assert candidate == original and "PLUGINS.artifacts" not in candidate
    assert set(prepared["PLUGINS.artifacts"]) == {binding.slot}
    monkeypatch.setattr(PluginCatalog, "resolve_selection", lambda *a: pytest.fail("Repeated resolution"))
    again = preparer.prepare_snapshot(prepared, ("actuation",))
    assert again == prepared and again is not prepared
    again["PLUGINS.bindings"][0]["parameters"]["gain"] = 9
    assert prepared["PLUGINS.bindings"][0]["parameters"]["gain"] == 1


def test_snapshot_preparation_does_not_silently_repin_changed_bytes(installed):
    from omnibci.infrastructure.plugin_management.preparation import PluginArtifactPreparer

    store, binding, _ = installed
    prepared = _pinned(store, binding)
    (store.packages_dir / "acme.device/1.0.0/driver.py").write_text("changed")
    with pytest.raises(PluginLoadError, match="content"):
        PluginArtifactPreparer(store).prepare_snapshot(prepared, ("actuation",))


@pytest.mark.parametrize("selected_pins", [None, {}, {"processing.decoder": {"pin": "new"}}])
def test_module_merge_replaces_only_its_own_artifact_pins(selected_pins):
    from omnibci.application.configuration.definitions import merge_module_settings

    before = {"PLUGINS.bindings": [], "PLUGINS.artifacts": {
        "acquisition.source": {"pin": "source"}, "processing.decoder": {"pin": "old"},
        "processing.filter.removed": {"pin": "filter"},
    }}
    selected = {"PLUGINS.bindings": []}
    if selected_pins is not None:
        selected["PLUGINS.artifacts"] = selected_pins
    merged = merge_module_settings(before, selected, ("processing",))
    assert merged["PLUGINS.artifacts"] == {"acquisition.source": {"pin": "source"}, **(selected_pins or {})}
    merged["PLUGINS.artifacts"]["acquisition.source"]["pin"] = "edited"
    assert before["PLUGINS.artifacts"]["acquisition.source"]["pin"] == "source"


def test_preparing_empty_foundation_never_resolves_a_contribution(installed, monkeypatch):
    from omnibci.infrastructure.plugin_management.catalog import PluginCatalog
    from omnibci.infrastructure.plugin_management.preparation import PluginArtifactPreparer

    store, _, _ = installed
    monkeypatch.setattr(PluginCatalog, "resolve_selection", lambda *a: pytest.fail("No selected contribution"))
    assert PluginArtifactPreparer(store).prepare_snapshot({"PLUGINS.bindings": []}, ()) == {
        "PLUGINS.bindings": [], "PLUGINS.artifacts": {},
    }


def _prepared_helper_package(tmp_path, *, helper=False):
    import zipfile
    from omnibci.infrastructure.plugin_management import preparation

    source = '''from omnibci.sdk import Effector
class Driver(Effector):
    def __init__(self, *, parameters, resource_root): pass
    def setup(self): pass
    def execute(self, command):
        from .helper import VALUE
        self.value = VALUE
    def quiesce(self): pass
    def teardown(self): pass
'''
    archive = package_archive(tmp_path, code=source)
    if helper:
        with zipfile.ZipFile(archive, "a") as writer:
            writer.writestr("helper.py", "VALUE = 1\n")
    store = ManagedPluginPackageStore(tmp_path / "plugins")
    package = store.install_archive(archive).package
    store.set_enabled(package.package_id, package.version, True)
    binding = ProfileBinding("actuation.effector", "effector", None,
                             package.contributions[0].selection, {"gain": 1.0})
    snapshot = preparation.PluginArtifactPreparer(store).prepare_snapshot(
        {"PLUGINS.bindings": [binding.to_dict()]}, ("actuation",),
    )
    return snapshot, store.packages_dir / "acme.device/1.0.0"


def test_code_change_between_handoff_verification_and_import_is_rejected(tmp_path, monkeypatch):
    from omnibci.domain.ports.effector import Effector
    from omnibci.infrastructure.plugin_management import preparation
    from omnibci.infrastructure.plugin_management.runtime import load_configured_plugin

    snapshot, root = _prepared_helper_package(tmp_path)
    marker = tmp_path / "executed"
    orig_read = preparation.read_prepared_plugin

    def change_after_read(*args):
        descriptor = orig_read(*args)
        (root / "driver.py").write_text(f'from pathlib import Path\nPath({str(marker)!r}).write_text("bad")\n')
        return descriptor

    monkeypatch.setattr(preparation, "read_prepared_plugin", change_after_read)
    with pytest.raises(PluginLoadError, match="content.*preparation"):
        load_configured_plugin(snapshot, "actuation.effector", "effector", Effector)
    assert not marker.exists()


@pytest.mark.parametrize("existed", [True, False])
def test_lazy_relative_import_rejects_changed_or_injected_source(tmp_path, existed):
    from omnibci.domain.ports.effector import Effector
    from omnibci.infrastructure.plugin_management.runtime import load_configured_plugin

    snapshot, root = _prepared_helper_package(tmp_path, helper=existed)
    instance = load_configured_plugin(snapshot, "actuation.effector", "effector", Effector)
    (root / "helper.py").write_text("VALUE = 999\n")
    with pytest.raises(PluginLoadError, match="source.*preparation"):
        instance.execute({})
    assert not hasattr(instance, "value")
