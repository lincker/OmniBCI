"""Managed inventory remains separate from selectable catalog contributions."""
import json
import zipfile

import pytest
from tests.plugin_configuration_helpers import output_payload_files
import tomlkit

from omnibci.application.plugin_management.packages import PluginPackageError, PluginPackageNotFoundError
from omnibci.infrastructure.plugin_management.store import ManagedPluginPackageStore
from omnibci.infrastructure.plugin_management.catalog import PluginCatalog, PluginNotFoundError


def package_archive(tmp_path, version="1.0.0", *, properties=None, schema_version=1,
                    package_id="acme.device", code=None):
    publisher, name = package_id.split(".")
    manifest = {
        "manifest_version": 1,
        "package": {"id": name, "publisher": publisher, "version": version, "api_version": "1", "domain": "actuation"},
        "contributions": [{"id": "output", "kind": "effector",
                           "entrypoint": "driver:Driver", "display_name": "Test output",
                           "configuration": {"schema": "configuration.schema.json", "schema_version": schema_version}}],
    }
    schema = {"type": "object", "additionalProperties": False, "properties": properties if properties is not None else {
        "gain": {"type": "number", "minimum": 0, "default": 1.0},
    }}
    payload_files = output_payload_files(manifest)
    destination = tmp_path / f"{package_id}-{version}.zip"
    with zipfile.ZipFile(destination, "w") as archive:
        for name, contents in payload_files.items():
            archive.writestr(name, contents)
        archive.writestr("manifest.toml", tomlkit.dumps(manifest))
        archive.writestr("configuration.schema.json", json.dumps(schema))
        archive.writestr("driver.py", code or "raise AssertionError('Catalog must not import plugin code')\n")
    return destination


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("OMNIBCI_PLUGIN_DIR", str(tmp_path / "plugins"))
    return ManagedPluginPackageStore()


def test_installed_inventory_is_disabled_and_static(store, tmp_path):
    archive = package_archive(tmp_path)
    result = store.install_archive(archive)
    package = result.package
    assert package.package_id == "acme.device" and package.version == "1.0.0"
    assert package.label == "Test output"
    assert not package.enabled and package.available and package.error is None
    assert store.list_packages() == [package]
    assert store.install_archive(archive).already_installed
    selection = package.contributions[0].selection
    assert store.configuration("effector", selection).schema["properties"]["gain"]["default"] == 1.0
    with pytest.raises(PluginNotFoundError):
        PluginCatalog().resolve_selection("effector", selection)
    assert not list(store.packages_dir.rglob("__pycache__"))


@pytest.mark.parametrize("identity,version", [("../bad", "1.0"), ("acme.device", "../1"), ("acme.device", "1/2")])
def test_lookup_rejects_path_traversal(store, identity, version):
    with pytest.raises(PluginPackageError):
        store.get_package(identity, version)


def test_missing_version_and_bad_zip_have_application_errors(store, tmp_path):
    with pytest.raises(PluginPackageNotFoundError):
        store.get_package("acme.device", "1.0")
    invalid = tmp_path / "broken.zip"
    invalid.write_bytes(b"not a zip")
    with pytest.raises(PluginPackageError):
        store.install_archive(invalid)


@pytest.mark.parametrize("problem", ["missing", "invalid", "digest", "path", "manifest_identity"])
def test_damaged_metadata_is_visible_but_unavailable(store, tmp_path, problem):
    store.install_archive(package_archive(tmp_path))
    location = store.packages_dir / "acme.device" / "1.0.0"
    receipt = location / ".omnibci-install.json"
    document = json.loads(receipt.read_text())
    if problem == "missing":
        receipt.unlink()
    elif problem == "invalid":
        receipt.write_text("{}")
    elif problem == "manifest_identity":
        path = location / "manifest.toml"
        path.write_text(path.read_text().replace('publisher = "acme"', 'publisher = "other"'))
    else:
        if problem == "digest":
            document["sha256"] = "not a digest"
        else:
            document["files"]["../outside"] = "0" * 64
        receipt.write_text(json.dumps(document))
    package = store.list_packages()[0]
    assert package.error and not package.available and not package.contributions


def test_hidden_staging_directories_are_not_inventory(store, tmp_path):
    (store.packages_dir / "__install-pending").mkdir(parents=True)
    (store.packages_dir / "__remove-old").mkdir()
    assert store.list_packages() == []


def test_default_store_uses_configured_user_plugin_root(store, tmp_path):
    assert store.plugin_dir == tmp_path / "plugins"
    assert store.installer.root == tmp_path / "plugins" / "packages"


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


def test_multiple_enabled_versions_are_exact_addressable(store, tmp_path):
    for version in ("1.0.0", "2.0.0"):
        package = store.install_archive(package_archive(tmp_path, version)).package
        store.set_enabled(package.package_id, version, True)
        assert PluginCatalog().resolve_selection("effector", package.contributions[0].selection).version == version
    assert len([item for item in PluginCatalog().list_contributions("effector") if item.identifier == "acme.device"]) == 2
    assert len(PluginCatalog().list_contributions("effector")) >= 2


def test_enabled_catalog_descriptor_loads_only_the_selected_version(store, tmp_path):
    from omnibci.domain.ports.effector import Effector
    from omnibci.infrastructure.plugin_management.loader import PluginLoader

    for version in ("1.0.0", "2.0.0"):
        code = f"""from omnibci.sdk import Effector
class Driver(Effector):
    marker = {version!r}
    def setup(self): pass
    def teardown(self): pass
    def execute(self, command): pass
    def quiesce(self): pass
"""
        package = store.install_archive(package_archive(tmp_path, version, code=code)).package
        store.set_enabled(package.package_id, version, True)
        descriptor = PluginCatalog().resolve_selection("effector", package.contributions[0].selection)
        assert PluginLoader.load_entrypoint(descriptor, Effector).marker == version


def test_enabled_version_must_be_disabled_before_removal(store, tmp_path):
    from omnibci.application.plugin_management.packages import PluginPackageConflictError

    store.install_archive(package_archive(tmp_path))
    store.set_enabled("acme.device", "1.0.0", True)
    with pytest.raises(PluginPackageConflictError, match="Disable"):
        store.remove("acme.device", "1.0.0")
    store.set_enabled("acme.device", "1.0.0", False)
    store.remove("acme.device", "1.0.0")
    assert store.list_packages() == []


def test_package_conflicting_with_builtin_exact_identity_cannot_be_enabled(store, tmp_path):
    from omnibci.application.plugin_management.packages import PluginPackageConflictError

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
    from omnibci.application.plugin_management.packages import PluginPackageStorageError

    for version in ("1.0.0", "2.0.0"):
        store.install_archive(package_archive(tmp_path, version))
    store.set_enabled("acme.device", "1.0.0", True)
    previous = store.registry.path.read_bytes()

    def fail(*_):
        raise OSError("disk write failed")

    monkeypatch.setattr("omnibci.infrastructure.storage.os.replace", fail)
    with pytest.raises(PluginPackageStorageError, match="disk write"):
        store.set_enabled("acme.device", "2.0.0", True)
    assert store.registry.path.read_bytes() == previous
    assert not list(store.registry.state_dir.glob(".enabled-*"))


def test_separate_registry_instances_serialize_updates(store, tmp_path):
    from concurrent.futures import ThreadPoolExecutor

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
    from omnibci.application.plugin_management.packages import PluginPackageStorageError

    store.registry.state_dir.mkdir(parents=True)
    store.registry.path.write_text(json.dumps(document))
    with pytest.raises(PluginPackageStorageError):
        PluginCatalog().scan("effector")


def test_symlink_state_is_rejected(store, tmp_path):
    from omnibci.application.plugin_management.packages import PluginPackageStorageError

    target = tmp_path / "elsewhere"
    target.mkdir()
    store.plugin_dir.mkdir()
    store.registry.state_dir.symlink_to(target, target_is_directory=True)
    with pytest.raises(PluginPackageStorageError, match="symlink"):
        store.registry.read()


def test_enable_and_list_avoid_redundant_registry_reads(store, tmp_path, monkeypatch):
    from omnibci.infrastructure.plugin_management.registry import InstalledPackageRegistry

    for version in ("1.0.0", "2.0.0", "3.0.0"):
        store.install_archive(package_archive(tmp_path, version))

    reads = 0
    original_read = InstalledPackageRegistry.read

    def counting_read(self):
        nonlocal reads
        reads += 1
        return original_read(self)

    monkeypatch.setattr(InstalledPackageRegistry, "read", counting_read)

    store.set_enabled("acme.device", "1.0.0", True)
    assert reads <= 2

    reads = 0
    packages = store.list_packages()
    assert len(packages) == 3
    assert reads == 1


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
    from omnibci.infrastructure.plugin_management.registry import InstalledPackageRegistry

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
    from omnibci.infrastructure.plugin_management.catalog import CatalogConfigurationAdapter
    from omnibci.infrastructure.plugin_management.installer import PluginPackageInstaller

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
    selected_store = ManagedPluginPackageStore(directory, installer=installer)

    package = selected_store.install_archive(archive).package
    assert package.available and package.error is None
    selection = package.contributions[0].selection
    assert selected_store.configuration("effector", selection).available
    selected_store.set_enabled(package.package_id, package.version, True)
    metadata = CatalogConfigurationAdapter(selected_store.catalog, platform_name=platform_name)
    assert metadata.get("effector", selection).available
    assert selected_store.catalog.host_version == installer.host_version
    assert not list(selected_store.packages_dir.rglob("__pycache__"))


@pytest.mark.parametrize("conflict_scope", ["selected", "default"])
def test_other_version_classification_does_not_block_the_selected_store(tmp_path, monkeypatch, conflict_scope):
    from tests.infrastructure.test_plugin_contributions import _write_package

    monkeypatch.setenv("OMNIBCI_PLUGIN_DIR", str(tmp_path / "default"))
    _write_package(tmp_path / conflict_scope / "acquisition")
    selected_store = ManagedPluginPackageStore(tmp_path / "selected")
    assert selected_store.install_archive(package_archive(tmp_path)).package.available


def test_root_providers_and_plugin_directory_cannot_define_two_scopes(tmp_path):
    with pytest.raises(ValueError, match="scope|root|directory"):
        PluginCatalog({}, plugin_dir=tmp_path)
