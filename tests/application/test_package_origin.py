"""Installation origin and accepted repository evidence are host-owned records."""
import base64
import hashlib
import json
from dataclasses import asdict
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from omnibci.application.plugin_management.market import PluginMarketError
from omnibci.application.plugin_management.packages import PluginPackageError
from omnibci.presentation.errors import register_exception_handlers
from omnibci.presentation.routers.plugin_packages import router
from tests.application.test_market_installation_boundary import artifacts, services
from tests.infrastructure.test_market_repository import _NOW, _URL, _DOWNLOAD, _signed
from tests.infrastructure.test_package_store import package_archive


def receipt_path(store):
    return store.packages_dir / 'acme.device/1.0.0/.omnibci-install.json'


def test_local_install_records_origin_without_claiming_repository_verification(artifacts):
    store, archive, _, _ = artifacts
    _, _, installation = services(artifacts)
    installed = installation.install(archive).package
    assert installed.origin == 'local'
    assert installed.verified_repository is None
    receipt = json.loads(receipt_path(store).read_text())
    assert receipt['origin'] == 'local' and receipt['market_verification'] is None
    before = receipt_path(store).read_bytes()
    assert installation.install(archive).already_installed
    assert receipt_path(store).read_bytes() == before


def test_market_install_retains_exact_accepted_proof_but_exposes_only_summary(artifacts):
    store, _, key, _ = artifacts
    market, repository, _ = services(artifacts)
    prepared = repository.prepare_install('acme.device', '1.0.0')
    installed = market.install('acme.device', '1.0.0').package
    receipt = json.loads(receipt_path(store).read_text())
    assert receipt['origin'] == installed.origin == 'market'
    evidence = receipt['market_verification']
    assert evidence['repository_id'] == installed.verified_repository == 'example.plugins'
    assert evidence['key_id'] == 'test-key'
    assert evidence['key_sha256'] == hashlib.sha256(key.public_key().public_bytes_raw()).hexdigest()
    assert evidence['index_sha256'] == hashlib.sha256(prepared.proof).hexdigest()
    assert base64.b64decode(evidence['proof']) == prepared.proof
    assert evidence['accepted_at'] == _NOW.isoformat()
    public = asdict(installed)
    assert not {'proof', 'key_sha256', 'market_verification', 'location', 'download_url'} & public.keys()


@pytest.mark.parametrize('first', ['local', 'market'])
def test_same_bytes_across_sources_preserve_origin_enablement_and_evidence(artifacts, first):
    store, archive, _, _ = artifacts
    market, _, installation = services(artifacts)
    if first == 'local':
        installation.install(archive)
    else:
        market.install('acme.device', '1.0.0')
    store.set_enabled('acme.device', '1.0.0', True)
    driver = receipt_path(store).parent / 'driver.py'
    modified = driver.stat().st_mtime_ns
    result = market.install('acme.device', '1.0.0')
    assert result.already_installed and result.package.enabled
    assert result.package.origin == first and result.package.verified_repository == 'example.plugins'
    before = receipt_path(store).read_bytes()
    assert installation.install(archive).already_installed
    assert market.install('acme.device', '1.0.0').already_installed
    assert receipt_path(store).read_bytes() == before
    assert driver.stat().st_mtime_ns == modified


def test_same_proof_retry_does_not_rewrite_acceptance_time(artifacts):
    store, _, _, _ = artifacts
    market, repository, _ = services(artifacts)
    market.install('acme.device', '1.0.0')
    original = receipt_path(store).read_bytes()
    repository.clock = lambda: _NOW + timedelta(minutes=1)
    assert market.install('acme.device', '1.0.0').already_installed
    assert receipt_path(store).read_bytes() == original
    assert json.loads(original)['market_verification']['accepted_at'] == _NOW.isoformat()


def test_explicit_new_verification_updates_evidence_without_changing_origin(artifacts):
    store, archive, key, index = artifacts
    market, repository, installation = services(artifacts)
    installation.install(archive)
    market.install('acme.device', '1.0.0')
    before = json.loads(receipt_path(store).read_text())
    index['issued_at'] = (_NOW + timedelta(seconds=1)).strftime('%Y-%m-%dT%H:%M:%SZ')
    proof = _signed(index, key)
    repository._opener.routes[_URL] = proof
    repository.clock = lambda: _NOW + timedelta(seconds=2)
    result = market.install('acme.device', '1.0.0')
    after = json.loads(receipt_path(store).read_text())
    assert result.already_installed and result.package.origin == 'local'
    assert after['files'] == before['files'] and after['sha256'] == before['sha256']
    assert after['market_verification']['index_sha256'] == hashlib.sha256(proof).hexdigest()
    assert after['market_verification']['accepted_at'] == repository.clock().isoformat()


@pytest.mark.parametrize('existing', [False, True])
def test_proof_expiring_during_archive_validation_cannot_be_published(artifacts, monkeypatch, existing):
    store, archive, _, _ = artifacts
    market, repository, installation = services(artifacts)
    if existing:
        installation.install(archive)
    before = receipt_path(store).read_bytes() if existing else None
    original = store.installer._extract
    def delayed(*args, **kwargs):
        files = original(*args, **kwargs)
        repository.clock = lambda: _NOW + timedelta(days=7)
        return files
    monkeypatch.setattr(store.installer, '_extract', delayed)
    with pytest.raises(PluginMarketError, match='expired'):
        market.install('acme.device', '1.0.0')
    assert (receipt_path(store).read_bytes() if existing else None) == before
    if not existing:
        assert store.list_packages() == []


def test_expired_repository_does_not_revoke_an_already_accepted_installation(artifacts, monkeypatch):
    store, _, _, _ = artifacts
    market, repository, _ = services(artifacts)
    market.install('acme.device', '1.0.0')
    repository.clock = lambda: _NOW + timedelta(days=7)
    monkeypatch.setattr(repository, '_fetch_index', lambda: pytest.fail('Runtime contacted the market'))
    reopened = type(store)(store.plugin_dir)
    reopened.verify_integrity('acme.device', '1.0.0')
    package = reopened.set_enabled('acme.device', '1.0.0', True)
    assert package.origin == 'market' and package.verified_repository == 'example.plugins'
    assert package.enabled and package.available


def test_conflicting_market_bytes_cannot_change_local_provenance(artifacts, tmp_path):
    store, archive, key, index = artifacts
    market, repository, installation = services(artifacts)
    installation.install(archive)
    original = receipt_path(store).read_bytes()
    archive = package_archive(tmp_path, code='changed = True\n')
    index['packages'][0]['sha256'] = hashlib.sha256(archive.read_bytes()).hexdigest()
    index['packages'][0]['size'] = archive.stat().st_size
    repository._opener.routes = {_URL: _signed(index, key), _DOWNLOAD: archive.read_bytes()}
    with pytest.raises(PluginPackageError, match='conflict'):
        market.install('acme.device', '1.0.0')
    assert receipt_path(store).read_bytes() == original
    assert store.get_package('acme.device', '1.0.0').origin == 'local'


def test_failed_evidence_replacement_keeps_existing_installation_intact(artifacts, monkeypatch):
    store, archive, _, _ = artifacts
    market, _, installation = services(artifacts)
    installation.install(archive)
    store.set_enabled('acme.device', '1.0.0', True)
    path = receipt_path(store)
    original = path.read_bytes()
    import omnibci.infrastructure.plugin_management.installer as installer_module
    replace = installer_module.os.replace
    def failing(source, destination):
        if Path(destination) == path:
            raise OSError('receipt publication failed')
        return replace(source, destination)
    monkeypatch.setattr(installer_module.os, 'replace', failing)
    with pytest.raises(PluginPackageError, match='receipt publication failed'):
        market.install('acme.device', '1.0.0')
    assert path.read_bytes() == original
    assert store.get_package('acme.device', '1.0.0').enabled
    store.verify_integrity('acme.device', '1.0.0')
    assert not list(store.packages_dir.rglob('__receipt-*'))


@pytest.mark.parametrize('change', [
    {'origin': 'builtin'}, {'origin': 'market'}, {'origin': True},
    {'market_verification': {'repository_id': 'forged'}},
])
def test_invalid_origin_or_evidence_is_not_available(artifacts, change):
    store, archive, _, _ = artifacts
    _, _, installation = services(artifacts)
    installation.install(archive)
    path = receipt_path(store)
    document = json.loads(path.read_text())
    document.update(change)
    path.write_text(json.dumps(document))
    result = store.get_package('acme.device', '1.0.0')
    assert not result.available and result.error
    with pytest.raises(PluginPackageError):
        store.set_enabled('acme.device', '1.0.0', True)


def test_http_upload_cannot_assert_market_origin(artifacts):
    _, archive, _, _ = artifacts
    _, _, installation = services(artifacts)
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(router)
    app.state.plugin_package_service = installation
    with TestClient(app) as client:
        response = client.post('/plugin-packages/install?origin=market', content=archive.read_bytes(),
                               headers={'Content-Type': 'application/zip', 'X-Plugin-Origin': 'market'})
    assert response.status_code == 201
    package = response.json()['data']['package']
    assert package['origin'] == 'local' and package['verified_repository'] is None
    assert not {'market_verification', 'proof', 'location', 'download_url'} & package.keys()


def test_excessively_nested_receipt_is_unavailable_instead_of_crashing_inventory(artifacts):
    store, archive, _, _ = artifacts
    _, _, installation = services(artifacts)
    installation.install(archive)
    path = receipt_path(store)
    path.write_text('{"unexpected":' + '[' * 2000 + '0' + ']' * 2000 + '}')
    package = store.get_package('acme.device', '1.0.0')
    assert not package.available and package.error


@pytest.mark.parametrize('field,value', [
    ('proof', 'not base64'), ('proof', 'x' * (2 * 1024 * 1024)),
    ('index_sha256', '0' * 64), ('key_sha256', 'invalid'),
    ('accepted_at', '2026-11-20T12:00:00'), ('repository_id', ''),
], ids=['base64', 'oversize', 'index-digest', 'key-digest', 'timestamp', 'repository'])
def test_corrupt_market_evidence_cannot_enable_an_installation(artifacts, field, value):
    store, _, _, _ = artifacts
    market, _, _ = services(artifacts)
    market.install('acme.device', '1.0.0')
    path = receipt_path(store)
    document = json.loads(path.read_text())
    document['market_verification'][field] = value
    path.write_text(json.dumps(document))
    assert not store.get_package('acme.device', '1.0.0').available
    with pytest.raises(PluginPackageError):
        store.set_enabled('acme.device', '1.0.0', True)
