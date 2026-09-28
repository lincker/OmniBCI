"""Exact versions become discoverable only through explicit atomic enablement."""
import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from omnibci.application.plugin_packages import PluginPackageConflictError, PluginPackageError, PluginPackageStorageError
from omnibci.domain.ports.effector import Effector
from omnibci.infrastructure.plugins.loader import PluginLoader
from omnibci.infrastructure.plugins.package_lifecycle_store import ManagedPluginPackageStore
from omnibci.infrastructure.plugins.plugin_catalog import AmbiguousPluginError, PluginCatalog, PluginNotFoundError
from tests.infrastructure.test_package_store import package_archive


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("OMNIBCI_PLUGIN_DIR", str(tmp_path / "plugins"))
    return ManagedPluginPackageStore()


def test_enable_disable_persists_without_import_or_alias_fallback(store, tmp_path):
    package = store.install_archive(package_archive(tmp_path)).package
    selection = package.contributions[0].selection
    with pytest.raises(PluginNotFoundError):
        PluginCatalog().resolve_selection("effector", selection)
    assert store.set_enabled(package.package_id, package.version, True).enabled
    assert ManagedPluginPackageStore().get_package(package.package_id, package.version).enabled
    assert PluginCatalog().resolve_selection("effector", selection).location == store.packages_dir / "acme.device/1.0.0"
    assert not list(store.packages_dir.rglob("__pycache__"))
    store.set_enabled(package.package_id, package.version, False)
    with pytest.raises(PluginNotFoundError):
        PluginCatalog().resolve_selection("effector", selection)


def test_multiple_enabled_versions_are_exact_addressable_and_alias_is_ambiguous(store, tmp_path):
    for version in ("1.0.0", "2.0.0"):
        package = store.install_archive(package_archive(tmp_path, version)).package
        store.set_enabled(package.package_id, version, True)
        assert PluginCatalog().resolve_selection("effector", package.contributions[0].selection).version == version
    assert len([item for item in PluginCatalog().list_contributions("effector") if item.identifier == "acme.device"]) == 2
    with pytest.raises(AmbiguousPluginError):
        PluginCatalog().resolve_legacy("effector", "test_output")


def test_enabled_catalog_descriptor_loads_only_the_selected_version(store, tmp_path):
    for version in ("1.0.0", "2.0.0"):
        code = f"""from omnibci.sdk import Effector
class Driver(Effector):
    marker = {version!r}
    def setup(self): pass
    def teardown(self): pass
    def play(self, command): pass
"""
        package = store.install_archive(package_archive(tmp_path, version, code=code)).package
        store.set_enabled(package.package_id, version, True)
        descriptor = PluginCatalog().resolve_selection("effector", package.contributions[0].selection)
        assert PluginLoader.load_entrypoint(descriptor, Effector).marker == version


def test_enabled_version_must_be_disabled_before_removal(store, tmp_path):
    store.install_archive(package_archive(tmp_path))
    store.set_enabled("acme.device", "1.0.0", True)
    with pytest.raises(PluginPackageConflictError, match="Disable"):
        store.remove("acme.device", "1.0.0")
    store.set_enabled("acme.device", "1.0.0", False)
    store.remove("acme.device", "1.0.0")
    assert store.list_packages() == []


def test_package_conflicting_with_builtin_exact_identity_cannot_be_enabled(store, tmp_path):
    store.install_archive(package_archive(tmp_path, "0.1.0", package_id="omnibci.logger"))
    with pytest.raises(PluginPackageConflictError, match="exact package"):
        store.set_enabled("omnibci.logger", "0.1.0", True)
    assert not store.registry.read()


def test_changed_installed_code_cannot_be_enabled(store, tmp_path):
    store.install_archive(package_archive(tmp_path))
    (store.packages_dir / "acme.device/1.0.0/driver.py").write_text("# replaced")
    with pytest.raises(PluginPackageError, match="conflicting content"):
        store.set_enabled("acme.device", "1.0.0", True)
    assert not store.registry.read()


def test_case_insensitive_filesystems_cannot_bypass_enablement_removal_guard(store, tmp_path):
    store.install_archive(package_archive(tmp_path, "1.0.0-RC"))
    store.set_enabled("acme.device", "1.0.0-RC", True)
    with pytest.raises(PluginPackageError, match="exact installed spelling"):
        store.remove("Acme.Device", "1.0.0-RC")
    with pytest.raises(PluginPackageError, match="exact installed spelling"):
        store.remove("acme.device", "1.0.0-rc")
    assert store.get_package("acme.device", "1.0.0-RC").enabled


def test_failed_atomic_state_write_preserves_previous_enablement(store, tmp_path, monkeypatch):
    for version in ("1.0.0", "2.0.0"):
        store.install_archive(package_archive(tmp_path, version))
    store.set_enabled("acme.device", "1.0.0", True)
    previous = store.registry.path.read_bytes()
    def fail(*_):
        raise OSError("disk write failed")
    monkeypatch.setattr("omnibci.infrastructure.plugins.installed_registry.os.replace", fail)
    with pytest.raises(PluginPackageStorageError, match="disk write"):
        store.set_enabled("acme.device", "2.0.0", True)
    assert store.registry.path.read_bytes() == previous
    assert not list(store.registry.state_dir.glob(".enabled-*"))


def test_separate_registry_instances_serialize_updates(store, tmp_path):
    for version in ("1.0.0", "2.0.0"):
        store.install_archive(package_archive(tmp_path, version))
    def enable(version):
        return ManagedPluginPackageStore(store.plugin_dir).set_enabled("acme.device", version, True)
    with ThreadPoolExecutor(max_workers=2) as executor:
        assert all(item.enabled for item in executor.map(enable, ("1.0.0", "2.0.0")))
    assert store.registry.read() == {("acme.device", "1.0.0"), ("acme.device", "2.0.0")}


@pytest.mark.parametrize("document", [
    {}, {"schema_version": True, "enabled": []},
    {"schema_version": 1, "enabled": [{"package_id": "../bad", "version": "1"}]},
    {"schema_version": 1, "enabled": [{"package_id": "acme.device", "version": "1"}] * 2},
    {"schema_version": 1, "enabled": [{"package_id": "acme.device", "version": "1"}]},
])
def test_corrupt_or_missing_enabled_packages_fail_closed(store, document):
    store.registry.state_dir.mkdir(parents=True)
    store.registry.path.write_text(json.dumps(document))
    with pytest.raises(PluginPackageStorageError):
        PluginCatalog().scan("effector")


def test_symlink_state_is_rejected(store, tmp_path):
    target = tmp_path / "elsewhere"
    target.mkdir()
    store.plugin_dir.mkdir()
    store.registry.state_dir.symlink_to(target, target_is_directory=True)
    with pytest.raises(PluginPackageStorageError, match="symlink"):
        store.registry.read()
