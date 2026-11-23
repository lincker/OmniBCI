"""An explicit installation scope never borrows the default inventory or target."""
import zipfile

import pytest
import tomlkit

from omnibci.infrastructure.plugin_management.catalog import CatalogConfigurationAdapter
from omnibci.infrastructure.plugin_management.registry import InstalledPackageRegistry
from omnibci.infrastructure.plugin_management.installer import PluginPackageInstaller
from omnibci.infrastructure.plugin_management.store import ManagedPluginPackageStore
from omnibci.infrastructure.plugin_management.catalog import PluginCatalog, PluginNotFoundError
from tests.infrastructure.test_package_store import package_archive
from tests.infrastructure.test_plugin_contributions import _write_package


def test_explicit_stores_do_not_conflict_with_default_inventory(tmp_path, monkeypatch):
    default = tmp_path / "default"
    monkeypatch.setenv("OMNIBCI_PLUGIN_DIR", str(default))
    archive = package_archive(tmp_path)
    other = ManagedPluginPackageStore()
    package = other.install_archive(archive).package
    other.set_enabled(package.package_id, package.version, True)
    selected = ManagedPluginPackageStore(tmp_path / "selected")
    selected.install_archive(archive)

    assert selected.set_enabled(package.package_id, package.version, True).enabled
    selection = package.contributions[0].selection
    assert selected.catalog.resolve_selection("effector", selection).location == selected._path(package.package_id, package.version)
    assert other.catalog.resolve_selection("effector", selection).location == other._path(package.package_id, package.version)
    assert other.registry.read() == selected.registry.read() == {("acme.device", "1.0.0")}


def test_corrupt_default_registry_cannot_block_an_explicit_store(tmp_path, monkeypatch):
    default = tmp_path / "default"
    monkeypatch.setenv("OMNIBCI_PLUGIN_DIR", str(default))
    registry = InstalledPackageRegistry(default)
    registry.path.parent.mkdir(parents=True)
    registry.path.write_text("invalid registry")
    selected = ManagedPluginPackageStore(tmp_path / "selected")

    package = selected.install_archive(package_archive(tmp_path)).package
    assert selected.set_enabled(package.package_id, package.version, True).enabled
    assert selected.configuration("effector", package.contributions[0].selection).available
    assert registry.path.read_text() == "invalid registry"


def test_catalog_pins_explicit_directory_while_refreshing_enablement(tmp_path, monkeypatch):
    selected = ManagedPluginPackageStore(tmp_path / "selected")
    catalog = PluginCatalog(plugin_dir=selected.plugin_dir)
    monkeypatch.setenv("OMNIBCI_PLUGIN_DIR", str(tmp_path / "different-default"))
    package = selected.install_archive(package_archive(tmp_path)).package
    selection = package.contributions[0].selection
    assert selected.configuration("effector", selection).schema is not None
    with pytest.raises(PluginNotFoundError):
        catalog.resolve_selection("effector", selection)
    selected.set_enabled(package.package_id, package.version, True)
    assert catalog.resolve_selection("effector", selection).location == selected._path(package.package_id, package.version)
    selected.set_enabled(package.package_id, package.version, False)
    with pytest.raises(PluginNotFoundError):
        catalog.resolve_selection("effector", selection)
    assert selected.configuration("effector", selection).schema is not None


@pytest.mark.parametrize("platform_name", ["win32", "linux"])
def test_inventory_and_configuration_use_the_installation_target(tmp_path, platform_name):
    archive = package_archive(tmp_path)
    with zipfile.ZipFile(archive) as reader:
        contents = {name: reader.read(name) for name in reader.namelist()}
    manifest = tomlkit.loads(contents["manifest.toml"].decode())
    manifest["package"]["platforms"] = [platform_name]
    manifest["package"]["host_version"] = "88.0.0"
    contents["manifest.toml"] = tomlkit.dumps(manifest).encode()
    with zipfile.ZipFile(archive, "w") as writer:
        for name, data in contents.items():
            writer.writestr(name, data)
    directory = tmp_path / "selected"
    installer = PluginPackageInstaller(directory / "packages", platform_name=platform_name, host_version="88.0.0")
    store = ManagedPluginPackageStore(directory, installer=installer)

    package = store.install_archive(archive).package
    assert package.available and package.error is None
    selection = package.contributions[0].selection
    assert store.configuration("effector", selection).available
    store.set_enabled(package.package_id, package.version, True)
    metadata = CatalogConfigurationAdapter(store.catalog, platform_name=platform_name)
    assert metadata.get("effector", selection).available
    assert store.catalog.host_version == installer.host_version
    assert not list(store.packages_dir.rglob("__pycache__"))


@pytest.mark.parametrize("conflict_scope", ["selected", "default"])
def test_other_version_classification_does_not_block_the_selected_store(tmp_path, monkeypatch, conflict_scope):
    monkeypatch.setenv("OMNIBCI_PLUGIN_DIR", str(tmp_path / "default"))
    _write_package(tmp_path / conflict_scope / "acquisition")
    store = ManagedPluginPackageStore(tmp_path / "selected")
    assert store.install_archive(package_archive(tmp_path)).package.available


def test_root_providers_and_plugin_directory_cannot_define_two_scopes(tmp_path):
    with pytest.raises(ValueError, match="scope|root|directory"):
        PluginCatalog({}, plugin_dir=tmp_path)
