"""Managed inventory remains separate from selectable catalog contributions."""
import json
import zipfile

import pytest
import tomlkit

from omnibci.application.plugin_packages import PluginPackageError, PluginPackageNotFoundError
from omnibci.infrastructure.plugins.package_store import DirectoryPluginPackageStore
from omnibci.infrastructure.plugins.plugin_catalog import PluginCatalog, PluginNotFoundError


def package_archive(tmp_path, version="1.0.0", *, properties=None, schema_version=1,
                    package_id="acme.device", code=None):
    publisher, name = package_id.split(".")
    manifest = {
        "manifest_version": 2,
        "package": {"id": name, "publisher": publisher, "version": version, "api_version": "2"},
        "contributions": [{"id": "output", "kind": "effector", "plugin_type": "test_output",
                           "entrypoint": "driver:Driver", "display_name": "Test output",
                           "configuration": {"schema": "configuration.schema.json", "schema_version": schema_version}}],
    }
    schema = {"type": "object", "additionalProperties": False, "properties": properties if properties is not None else {
        "gain": {"type": "number", "minimum": 0, "default": 1.0},
    }}
    destination = tmp_path / f"{package_id}-{version}.zip"
    with zipfile.ZipFile(destination, "w") as archive:
        archive.writestr("manifest.toml", tomlkit.dumps(manifest))
        archive.writestr("configuration.schema.json", json.dumps(schema))
        archive.writestr("driver.py", code or "raise AssertionError('Catalog must not import plugin code')\n")
    return destination


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("OMNIBCI_PLUGIN_DIR", str(tmp_path / "plugins"))
    return DirectoryPluginPackageStore()


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
