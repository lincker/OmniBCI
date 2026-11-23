"""Market verification feeds the same guarded installation path as local archives."""
from contextlib import contextmanager, nullcontext
from dataclasses import replace
from datetime import UTC, datetime

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from omnibci.application.plugin_management.market import PluginMarketError
from omnibci.application.plugin_management.packages import PluginPackageError
from omnibci.application.plugin_management.package_service import PluginPackageService
from omnibci.application.plugin_management.market_service import PluginMarketService
from omnibci.infrastructure.plugin_management.market import MarketRepositoryAdapter
from omnibci.infrastructure.plugin_management.market import StaticMarketRepository
from omnibci.infrastructure.plugin_management.store import ManagedPluginPackageStore
from tests.infrastructure.test_market_repository import _NOW, _URL, _DOWNLOAD, _Opener, _index, _signed
from tests.infrastructure.test_package_store import package_archive


@pytest.fixture
def artifacts(tmp_path):
    archive = package_archive(tmp_path)
    key = Ed25519PrivateKey.generate()
    index = _index(archive.read_bytes())
    index["packages"][0]["domain"] = "actuation"
    index["packages"][0]["contributions"] = [{"id": "output", "kind": "effector"}]
    store = ManagedPluginPackageStore(tmp_path / "plugins")
    return store, archive, key, index


def services(artifacts, guard=nullcontext):
    store, archive, key, index = artifacts
    repository = StaticMarketRepository(_URL, {"test-key": key.public_key()}, archive.parent / "cache",
                                        expected_repository_id="example.plugins", clock=lambda: _NOW)
    repository._opener = _Opener({_URL: _signed(index, key), _DOWNLOAD: archive.read_bytes()})
    installation = PluginPackageService(store, admission_guard=guard)
    return PluginMarketService(MarketRepositoryAdapter(repository), installation), repository, installation


def test_market_verification_and_local_publication_share_one_installation_owner(artifacts, monkeypatch):
    held = []
    @contextmanager
    def guard():
        assert not held
        held.append(True)
        try:
            yield
        finally:
            held.pop()
    market, repository, installation = services(artifacts, guard)
    store, archive, _, _ = artifacts
    prepare, verify, publish = repository.prepare_install, repository.verify_prepared, store.install_archive
    calls = []
    original_publish = store.installer._publish
    def publishing(*args):
        assert held
        calls.append("publish")
        return original_publish(*args)
    monkeypatch.setattr(store.installer, "_publish", publishing)
    def downloading(*args):
        assert not held
        calls.append("download")
        return prepare(*args)
    def checking(prepared):
        assert held
        calls.append("verify")
        return verify(prepared)
    def installing(*args, **kwargs):
        assert held
        calls.append("install")
        return publish(*args, **kwargs)
    monkeypatch.setattr(repository, "prepare_install", downloading)
    monkeypatch.setattr(repository, "verify_prepared", checking)
    monkeypatch.setattr(store, "install_archive", installing)

    result = market.install("acme.device", "1.0.0")
    assert calls == ["download", "install", "verify", "publish"]
    assert not result.package.enabled and not result.already_installed
    assert installation.install(archive).already_installed
    assert calls == ["download", "install", "verify", "publish", "install", "publish"]
    assert not hasattr(repository, "installer") and not hasattr(repository, "install")
    assert not hasattr(market.repository, "store") and not hasattr(market.repository, "install_prepared")
    assert not list(store.packages_dir.rglob("__pycache__"))


@pytest.mark.parametrize("problem", ["expired", "metadata", "archive"])
def test_delayed_publication_rechecks_proof_identity_and_archive(artifacts, problem, monkeypatch):
    market, repository, installation = services(artifacts)
    store, _, _, _ = artifacts
    original = repository.prepare_install
    def prepare(*args):
        prepared = original(*args)
        if problem == "metadata":
            return replace(prepared, package=replace(prepared.package, version="2.0.0"))
        if problem == "archive":
            prepared.archive.write_bytes(b"replaced after download")
        return prepared
    monkeypatch.setattr(repository, "prepare_install", prepare)
    @contextmanager
    def delayed():
        if problem == "expired":
            repository.clock = lambda: datetime(2026, 9, 25, tzinfo=UTC)
        yield
    installation.admission_guard = delayed
    with pytest.raises(PluginMarketError if problem == "expired" else PluginPackageError):
        market.install("acme.device", "1.0.0")
    assert not store.list_packages()


@pytest.mark.parametrize("first", ["local", "market"])
def test_cross_source_retries_preserve_enablement_and_reject_conflicting_bytes(artifacts, tmp_path, first):
    market, repository, installation = services(artifacts)
    store, archive, key, _ = artifacts
    if first == "local":
        installation.install(archive)
    else:
        market.install("acme.device", "1.0.0")
    store.set_enabled("acme.device", "1.0.0", True)
    retry = (market.install("acme.device", "1.0.0") if first == "local" else installation.install(archive))
    assert retry.already_installed and retry.package.enabled
    original = store.get_package("acme.device", "1.0.0")
    changed = package_archive(tmp_path, code="changed = True\n")
    if first == "local":
        index = _index(changed.read_bytes())
        index["packages"][0]["domain"] = "actuation"
        index["packages"][0]["contributions"] = [{"id": "output", "kind": "effector"}]
        repository._opener.routes = {_URL: _signed(index, key), _DOWNLOAD: changed.read_bytes()}
    with pytest.raises(PluginPackageError, match="different|conflict|immutable"):
        if first == "local":
            market.install("acme.device", "1.0.0")
        else:
            installation.install(changed)
    assert store.get_package("acme.device", "1.0.0") == original
