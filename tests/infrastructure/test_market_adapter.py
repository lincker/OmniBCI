"""Operator trust configuration and publication through shared runtime admission."""
import base64
import json
import threading
from contextlib import contextmanager, nullcontext
from dataclasses import replace
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from omnibci.application.plugin_market import PluginMarketError
from omnibci.application.plugin_profiles import ProfileRevisionConflict
from omnibci.application.services.plugin_market_service import PluginMarketService
from omnibci.application.services.plugin_profile_service import PluginProfileService
from omnibci.application.services.runtime_configuration_service import RuntimeConfigurationService
from omnibci.infrastructure.plugins.configuration_catalog import CatalogConfigurationAdapter
from omnibci.infrastructure.plugins.market_adapter import MarketRepositoryAdapter, configured_market_service
from omnibci.infrastructure.plugins.market_repository import StaticMarketRepository
from omnibci.infrastructure.plugins.package_lifecycle_store import ManagedPluginPackageStore
from omnibci.infrastructure.repositories.plugin_profile_repository import TomlPluginProfileRepository
from tests.infrastructure.test_market_repository import _NOW, _URL, _DOWNLOAD, _Opener, _index, _signed
from tests.infrastructure.test_package_store import package_archive


@pytest.fixture
def configured(tmp_path, monkeypatch):
    monkeypatch.setenv("OMNIBCI_PLUGIN_DIR", str(tmp_path / "plugins"))
    key = Ed25519PrivateKey.generate()
    store = ManagedPluginPackageStore()
    environment = {
        "OMNIBCI_MARKET_URL": _URL, "OMNIBCI_MARKET_REPOSITORY_ID": "example.plugins",
        "OMNIBCI_MARKET_TRUSTED_KEYS": json.dumps({"test-key": base64.b64encode(key.public_key().public_bytes_raw()).decode()}),
        "OMNIBCI_MARKET_CACHE_DIR": str(tmp_path / "cache"),
    }
    svc = configured_market_service(store, admission_guard=nullcontext, environ=environment)
    repo = svc.repository.repository
    repo.clock = lambda: _NOW
    archive = package_archive(tmp_path).read_bytes()
    index = _index(archive)
    index["packages"][0]["contributions"] = [{"id": "output", "kind": "effector"}]
    repo._opener = _Opener({_URL: _signed(index, key), _DOWNLOAD: archive})
    return svc, store, environment, key, index


def test_no_configuration_disables_market_without_a_default_endpoint(configured):
    _, store, _, _, _ = configured
    assert configured_market_service(store, admission_guard=nullcontext, environ={}) is None


@pytest.mark.parametrize("change", [
    {"OMNIBCI_MARKET_URL": "http://127.0.0.1/index.json"},
    {"OMNIBCI_MARKET_REPOSITORY_ID": ""},
    {"OMNIBCI_MARKET_REPOSITORY_ID": "different/identity"},
    {"OMNIBCI_MARKET_TRUSTED_KEYS": "not json"},
    {"OMNIBCI_MARKET_TRUSTED_KEYS": "[]"},
    {"OMNIBCI_MARKET_TRUSTED_KEYS": "{}"},
    {"OMNIBCI_MARKET_TRUSTED_KEYS": '{"test-key":"invalid"}'},
    {"OMNIBCI_MARKET_TRUSTED_KEYS": '{"same":"a","same":"b"}'},
    {"OMNIBCI_MARKET_TRUSTED_KEYS": '{"secret-looking-value":17}'},
    {"OMNIBCI_MARKET_CACHE_DIR": ""},
])
def test_operator_configuration_fails_closed_without_leaking_values(configured, change):
    _, store, environment, _, _ = configured
    with pytest.raises(PluginMarketError) as error:
        configured_market_service(store, admission_guard=nullcontext, environ={**environment, **change})
    assert "secret-looking-value" not in str(error.value)


def test_partial_operator_configuration_does_not_silently_enable_a_repository(configured):
    _, store, _, _, _ = configured
    with pytest.raises(PluginMarketError, match="together"):
        configured_market_service(store, admission_guard=nullcontext, environ={"OMNIBCI_MARKET_URL": _URL})


def test_download_occurs_outside_exclusive_guard_and_publication_inside(configured, monkeypatch):
    svc, store, _, _, _ = configured
    state = {"held": False}
    @contextmanager
    def guard():
        state["held"] = True
        try:
            yield
        finally:
            state["held"] = False
    original_prepare = svc.repository.prepare_install
    original_install = svc.repository.install_prepared
    def prepare(*args):
        assert not state["held"]
        return original_prepare(*args)
    def install(prepared):
        assert state["held"]
        return original_install(prepared)
    monkeypatch.setattr(svc.repository, "prepare_install", prepare)
    monkeypatch.setattr(svc.repository, "install_prepared", install)
    svc.admission_guard = guard
    result = svc.install("acme.device", "1.0.0")
    assert not result.package.enabled and result.package.available
    assert store.list_packages() == [result.package]


def test_busy_runtime_does_not_publish_a_downloaded_package(configured):
    svc, store, _, _, _ = configured
    @contextmanager
    def occupied():
        raise ProfileRevisionConflict("Apply in progress")
        yield
    svc.admission_guard = occupied
    with pytest.raises(ProfileRevisionConflict):
        svc.install("acme.device", "1.0.0")
    assert not store.list_packages()
    assert len(list(svc.repository.repository.cache_dir.glob("*.zip"))) == 1


def test_expiry_between_download_and_publication_is_rechecked(configured):
    svc, store, _, _, _ = configured
    @contextmanager
    def delayed():
        svc.repository.repository.clock = lambda: datetime(2026, 9, 25, tzinfo=UTC)
        yield
    svc.admission_guard = delayed
    with pytest.raises(PluginMarketError, match="expired"):
        svc.install("acme.device", "1.0.0")
    assert not store.list_packages()


def test_prepared_artifact_cannot_change_signed_metadata(configured):
    svc, store, _, _, _ = configured
    prepared = svc.repository.prepare_install("acme.device", "1.0.0")
    altered = replace(prepared, package=replace(prepared.package, version="2.0.0"))
    with pytest.raises(PluginMarketError, match="differs from signed"):
        svc.repository.install_prepared(altered)
    assert not store.list_packages()


@pytest.mark.integration
def test_real_signed_http_to_disabled_install_enable_profile_and_runtime_plan(tmp_path, monkeypatch):
    monkeypatch.setenv("OMNIBCI_PLUGIN_DIR", str(tmp_path / "plugins"))
    key = Ed25519PrivateKey.generate()
    archive = package_archive(tmp_path).read_bytes()
    index = _index(archive)
    index["packages"][0]["contributions"] = [{"id": "output", "kind": "effector"}]
    signed = _signed(index, key)
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            body = {"/index.json": signed, "/packages/device-1.0.0.zip": archive}.get(self.path)
            if body is None:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        store = ManagedPluginPackageStore()
        repo = StaticMarketRepository(f"http://127.0.0.1:{server.server_port}/index.json", {"test-key": key.public_key()},
                                      store.installer, tmp_path / "cache", expected_repository_id="example.plugins",
                                      allow_http=True, clock=lambda: _NOW)
        market = PluginMarketService(MarketRepositoryAdapter(repo, store), admission_guard=nullcontext)
        assert len(market.list()) == 1
        installed = market.install("acme.device", "1.0.0").package
        assert not installed.enabled
        profiles = PluginProfileService(TomlPluginProfileRepository(tmp_path / "config"),
                                        CatalogConfigurationAdapter(), settings_source=lambda: {"ACTUATION.enabled": True})
        assert all(item.selection.package_id != "acme.device" for item in profiles.catalog.list("effector"))
        store.set_enabled("acme.device", "1.0.0", True)
        selection = installed.contributions[0].selection
        output = profiles.create("effector", selection, "Downloaded output", {"gain": 2.0})
        bindings = [{"slot": "actuation.effector", "profile_id": output.profile_id}]
        for slot, kind, alias in (
            ("acquisition.source", "acquisition", "file_reader"),
            ("processing.decoder", "decoder", "THUBCI_SSVEP_classic40CCA"),
            ("processing.epoch_detector", "epoch_detector", "DEFAULT_EPOCH_DETECTOR"),
            ("stimulation.paradigm", "paradigm", "STIMULATION_THUBCI_SSVEP_classic40"),
        ):
            definition = profiles.catalog.legacy(kind, alias)
            profile = profiles.create(kind, definition.selection, alias, {})
            bindings.append({"slot": slot, "profile_id": profile.profile_id})
        runtime = RuntimeConfigurationService(profiles, SimpleNamespace(is_engine_ready=False))
        plan = runtime.plan(bindings, expected_revision=None)
        assert not plan.errors and plan.strategy == "select_on_start"
        pinned = runtime.resolve_plan(plan.plan_id).snapshot
        assert pinned.bindings[0].selection == selection
        assert pinned.bindings[0].parameters == {"gain": 2.0}
        assert profiles.repository.get_activation() is None
        assert not list(store.packages_dir.rglob("__pycache__"))
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
