"""Static v1 declarations select role ownership without package-domain dispatch."""
import pytest

from omnibci.infrastructure.plugins.manifest import parse_manifest, PluginManifestError
from omnibci.infrastructure.plugins.package_installer import PackageInstallError
from omnibci.infrastructure.plugins.plugin_catalog import PluginCatalog, PluginRoot
from tests.infrastructure.test_package_installer import _archive, _installer, _manifest
from tests.infrastructure.test_plugin_contributions import _selection


def parse(document, root):
    return parse_manifest(document, root, source="user", precedence=0, host_version="1.0.0")


def test_one_package_can_declare_roles_from_different_hosts_without_import(tmp_path):
    document = _manifest()
    document['contributions'].append({'id': 'filter', 'kind': 'filter', 'entrypoint': 'driver:Filter'})
    (tmp_path / 'driver.py').write_text("raise AssertionError('Static validation imported code')\n")
    descriptors = parse(document, tmp_path)
    assert {item.category for item in descriptors} == {'acquisition', 'filter'}
    assert all(item.package.domain == 'acquisition' for item in descriptors)
    installed = _installer(tmp_path).install(_archive(tmp_path, manifest=document))
    assert {item.category for item in installed.contributions} == {'acquisition', 'filter'}
    assert not list(installed.location.rglob('__pycache__'))


@pytest.mark.parametrize('kind', ['decoder', 'filter', 'epoch_detector'])
def test_processing_has_one_contract_and_decoder_always_declares_requirements(tmp_path, kind):
    document = _manifest(domain='processing')
    document['contributions'][0]['kind'] = kind
    if kind == 'decoder':
        document['contributions'][0]['channel_requirements'] = {'min_count': 1}
    descriptor, = parse(document, tmp_path)
    assert not hasattr(descriptor, 'signal_input_contract')
    assert (descriptor.channel_requirements is not None) == (kind == 'decoder')


def test_decoder_cannot_omit_requirements_using_the_retired_contract(tmp_path):
    document = _manifest(domain='processing')
    document['contributions'][0].update(kind='decoder', signal_input_contract=1)
    with pytest.raises(PluginManifestError):
        parse(document, tmp_path)


@pytest.mark.parametrize('kind', ['acquisition', 'filter', 'epoch_detector'])
def test_non_decoder_rejects_channel_requirements(tmp_path, kind):
    document = _manifest()
    document['contributions'][0].update(kind=kind, channel_requirements={})
    with pytest.raises(PluginManifestError):
        parse(document, tmp_path)


def test_domain_classification_may_change_between_exact_versions(tmp_path):
    installer = _installer(tmp_path)
    first = installer.install(_archive(tmp_path))
    second = installer.install(_archive(tmp_path, manifest=_manifest(version='2.0.0', domain='processing'), name='second.zip'))
    catalog = PluginCatalog({'acquisition': lambda: [PluginRoot(installer.root, 'user', 0)]}, host_version='1.0.0')
    assert catalog.resolve_selection('acquisition', _selection()).location == first.location
    assert catalog.resolve_selection('acquisition', _selection(version='2.0.0')).location == second.location
    assert first.contributions[0].package.domain == 'acquisition'
    assert second.contributions[0].package.domain == 'processing'


@pytest.mark.parametrize('boundary', ['catalog', 'installer'])
@pytest.mark.parametrize('section,field,value', [
    ('root', 'unrecognized', 'value'),
    ('package', 'source', 'builtin'),
    ('package', 'location', '/outside'),
    ('contribution', 'context', {}),
    ('contribution', 'signal_input_contract', 1),
])
def test_discovery_and_installation_reject_undeclared_manifest_fields(tmp_path, section, field, value, boundary):
    document = _manifest()
    target = document if section == 'root' else document['package'] if section == 'package' else document['contributions'][0]
    target[field] = value
    with pytest.raises((PluginManifestError, PackageInstallError), match=field):
        if boundary == 'catalog':
            parse(document, tmp_path)
        else:
            _installer(tmp_path).install(_archive(tmp_path, manifest=document))


def test_market_lists_contribution_kinds_independently_of_package_domain(tmp_path):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from tests.infrastructure.test_market_repository import (
        _index, _package_bytes, _signed, _URL, _NOW, _Opener, StaticMarketRepository,
    )
    key = Ed25519PrivateKey.generate()
    index = _index(_package_bytes())
    index['packages'][0]['contributions'].append({'id': 'filter', 'kind': 'filter'})
    client = StaticMarketRepository(_URL, {'test-key': key.public_key()}, tmp_path / 'cache',
                                    expected_repository_id='example.plugins', clock=lambda: _NOW)
    opener = _Opener({_URL: _signed(index, key)})
    client._opener = opener
    package, = client.list_packages()
    assert package.domain == 'acquisition'
    assert {item.kind for item in package.contributions} == {'acquisition', 'filter'}
    assert len(opener.requests) == 1


def test_entrypoint_does_not_translate_a_source_filename_into_a_module(tmp_path):
    document = _manifest()
    document['contributions'][0]['entrypoint'] = 'driver.py:Driver'
    with pytest.raises(PackageInstallError, match='Entrypoint'):
        _installer(tmp_path).install(_archive(tmp_path, manifest=document))
