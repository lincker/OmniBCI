"""Bind verified repository downloads to the managed inventory and operator config."""
import json
import os
import re
from contextlib import contextmanager
from pathlib import Path

from platformdirs import user_cache_dir

from omnibci.application.plugin_market import PluginMarketError
from omnibci.application.plugin_packages import PackageInstallation, PluginPackageError, PluginPackageStorageError
from omnibci.application.services.plugin_market_service import PluginMarketService
from omnibci.infrastructure.plugins.market_repository import MarketRepositoryError, StaticMarketRepository, _object_without_duplicates
from omnibci.infrastructure.plugins.package_installer import PackageInstallError
from omnibci.infrastructure.plugins.package_store import DirectoryPluginPackageStore


@contextmanager
def _market_errors():
    try:
        yield
    except MarketRepositoryError as exc:
        raise PluginMarketError(str(exc)) from exc
    except PackageInstallError as exc:
        raise PluginPackageError(str(exc)) from exc
    except OSError as exc:
        raise PluginPackageStorageError(f"Cannot access market cache or package storage: {exc}") from exc


class MarketRepositoryAdapter:
    def __init__(self, repository: StaticMarketRepository, store: DirectoryPluginPackageStore):
        if repository.installer is not store.installer:
            raise ValueError("Market and local installs must use the same managed installer")
        self.repository = repository
        self.store = store

    def list_packages(self):
        with _market_errors():
            return self.repository.list_packages()

    def prepare_install(self, package_id, version):
        with _market_errors():
            return self.repository.prepare_install(package_id, version)

    def install_prepared(self, prepared):
        with _market_errors():
            result = self.repository.install_prepared(prepared)
            return PackageInstallation(self.store.get_package(result.package_id, result.version), result.already_installed)


def configured_market_service(store: DirectoryPluginPackageStore, *, admission_guard, environ=None):
    """No configuration means disabled; partial/invalid trust configuration fails closed."""
    environment = os.environ if environ is None else environ
    names = ("OMNIBCI_MARKET_URL", "OMNIBCI_MARKET_REPOSITORY_ID", "OMNIBCI_MARKET_TRUSTED_KEYS")
    values = [environment.get(name) for name in names]
    if all(value is None for value in values):
        return None
    if any(not isinstance(value, str) or not value.strip() for value in values):
        raise PluginMarketError("Market configuration requires URL, repository ID and trusted public keys together")
    url, repository_id, encoded_keys = values
    if len(url) > 2048 or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", repository_id):
        raise PluginMarketError("Configured market URL or repository ID is invalid")
    if len(encoded_keys) > 64 * 1024:
        raise PluginMarketError("Configured market public-key mapping is too large")
    try:
        keys = json.loads(encoded_keys, object_pairs_hook=_object_without_duplicates)
    except (ValueError, MarketRepositoryError) as exc:
        raise PluginMarketError("OMNIBCI_MARKET_TRUSTED_KEYS must be a JSON object of key IDs and Base64 public keys") from exc
    if not isinstance(keys, dict) or not 1 <= len(keys) <= 128 or any(not isinstance(value, str) for value in keys.values()):
        raise PluginMarketError("Configure between 1 and 128 Base64 Ed25519 public keys")
    cache = environment.get("OMNIBCI_MARKET_CACHE_DIR")
    if cache is not None and (not isinstance(cache, str) or not cache.strip() or "\0" in cache):
        raise PluginMarketError("OMNIBCI_MARKET_CACHE_DIR must name a local cache directory")
    cache_dir = Path(cache).expanduser() if cache is not None else Path(user_cache_dir("OmniBCI", "OmniBCI")) / "plugin-market"
    with _market_errors():
        repository = StaticMarketRepository(url, keys, store.installer, cache_dir,
                                            expected_repository_id=repository_id)
    return PluginMarketService(MarketRepositoryAdapter(repository, store), admission_guard=admission_guard)
