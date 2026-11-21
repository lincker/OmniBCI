"""Static package validation is reusable without catalog discovery or code loading."""
import importlib
import json
from pathlib import Path

import pytest
import tomlkit

from omnibci.infrastructure.plugins.package_installer import PluginPackageInstaller
from omnibci.infrastructure.plugins.plugin_catalog import PluginCatalog, PluginRoot, vendor_plugins_dir


def package(root):
    (root / 'manifest.toml').write_text(tomlkit.dumps({
        'manifest_version': 2,
        'package': {'id': 'device', 'publisher': 'test', 'version': '1.0.0',
                    'api_version': '1', 'domain': 'acquisition'},
        'contributions': [
            {'id': 'source', 'kind': 'acquisition', 'entrypoint': 'driver:Source'},
            {'id': 'output', 'kind': 'trigger', 'entrypoint': 'driver:Output',
             'configuration': {'schema': 'output.json', 'schema_version': 1}},
        ],
    }))
    (root / 'output.json').write_text(json.dumps({
        'type': 'object', 'properties': {}, 'additionalProperties': False,
    }))
    (root / 'driver.py').write_text("raise AssertionError('Static validation imported code')\n")


def test_static_reader_returns_the_same_builtin_descriptors_as_catalog():
    manifest = importlib.import_module('omnibci.infrastructure.plugins.manifest')
    root = vendor_plugins_dir()
    catalog = PluginCatalog({'acquisition': lambda: [PluginRoot(root, 'builtin', 100)]})
    descriptors, errors = catalog._scan('acquisition', all_categories=True)
    assert not errors
    for location in {item.location for item in descriptors}:
        expected = tuple(item for item in descriptors if item.location == location)
        assert manifest.parse_manifest(manifest.read_manifest(location), location, source='builtin', precedence=100,
                                     host_version=catalog.host_version) == expected


def test_installation_reads_a_multi_contribution_manifest_once(tmp_path, monkeypatch):
    package(tmp_path)
    original = Path.read_text
    reads = []
    def read(path, *args, **kwargs):
        if path.name == 'manifest.toml':
            reads.append(path)
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'read_text', read)
    installer = PluginPackageInstaller(tmp_path / 'installed', host_version='1.0.0')
    descriptors = installer._validate_package(tmp_path)
    assert [item.contribution_id for item in descriptors] == ['source', 'output']
    assert reads == [tmp_path / 'manifest.toml']


def test_invalid_sibling_keeps_identity_for_no_fallback_resolution(tmp_path):
    manifest = importlib.import_module('omnibci.infrastructure.plugins.manifest')
    package(tmp_path)
    (tmp_path / 'output.json').write_text('{"type":"object"}')
    with pytest.raises(manifest.PluginManifestError) as caught:
        manifest.parse_manifest(manifest.read_manifest(tmp_path), tmp_path, source='user', precedence=0, host_version='1.0.0')
    assert caught.value.package_id == 'test.device'
    assert caught.value.version == '1.0.0'
