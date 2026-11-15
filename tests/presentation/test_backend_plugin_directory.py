"""Backend plugin discovery and spawned workers retain the startup directory."""

import multiprocessing as mp
import os

import pytest
import tomlkit
from fastapi.testclient import TestClient

from omnibci.application.plugin_profiles import PluginSelection
from omnibci.conf import OmniBCISettings
from omnibci.infrastructure.plugins.plugin_catalog import PluginCatalog, user_plugins_dir
from omnibci.presentation.main import create_app


SELECTION = PluginSelection("test.directory", "default", "1.0.0")


def _plugin(directory):
    package = directory / "effectors" / "directory"
    package.mkdir(parents=True)
    (package / "manifest.toml").write_text(tomlkit.dumps({
        "manifest_version": 2,
        "package": {"id": "directory", "publisher": "test", "version": SELECTION.version,
                    "domain": "actuation", "api_version": "2"},
        "contributions": [{"id": "default", "kind": "effector", "plugin_type": "directory_probe",
                           "display_name": "Directory probe", "entrypoint": "driver:Probe"}],
    }), encoding="utf-8")
    return package


def _worker_directory(connection):
    from omnibci.conf import settings
    settings.use_snapshot({"CONFIG.system_revision": "accepted-engine"})
    connection.send((str(user_plugins_dir()), str(PluginCatalog().resolve_selection("effector", SELECTION).location)))
    connection.close()


def _spawned_directory():
    context = mp.get_context("spawn")
    receiver, sender = context.Pipe(duplex=False)
    worker = context.Process(target=_worker_directory, args=(sender,))
    try:
        worker.start()
        sender.close()
        assert receiver.poll(15), "Spawned plugin discovery did not finish"
        result = receiver.recv()
        worker.join(15)
        assert worker.exitcode == 0
        return result
    finally:
        if worker.is_alive():
            worker.terminate()
            worker.join(5)
        sender.close()
        receiver.close()
        worker.close()


@pytest.fixture
def plugin_environment(tmp_path, monkeypatch):
    import omnibci.conf as conf
    monkeypatch.delenv("OMNIBCI_PLUGIN_DIR", raising=False)
    for key in ("OMNIBCI_MARKET_URL", "OMNIBCI_MARKET_REPOSITORY_ID", "OMNIBCI_MARKET_TRUSTED_KEYS"):
        monkeypatch.delenv(key, raising=False)
    root = tmp_path / "configuration"
    monkeypatch.setenv("OMNIBCI_CONFIG_DIR", str(root))
    settings = OmniBCISettings(user_file=root / "settings.toml")
    old, new = tmp_path / "old", tmp_path / "new"
    old_package, new_package = _plugin(old), _plugin(new)
    settings.update({"PLUGINS.user_dir": str(old)}, expected_revision=settings.read_saved()[1])
    monkeypatch.setattr(conf, "settings", settings)
    return old, new, old_package, new_package


def test_api_save_defers_plugin_directory_for_catalog_store_and_spawned_workers(plugin_environment):
    old, new, old_package, new_package = plugin_environment
    with TestClient(create_app()) as client:
        container = client.app.state.container
        catalog = container.plugin_profile_service.catalog.catalog
        store = container.plugin_package_service.store
        revision = client.get("/api/v1/config").json()["data"]["revision"]
        response = client.patch("/api/v1/config", json={
            "expected_revision": revision, "updates": {"PLUGINS.user_dir": str(new)},
        })
        assert response.status_code == 200, response.text
        assert response.json()["data"]["backend_restart_required"]
        assert catalog.resolve_selection("effector", SELECTION).location == old_package
        assert store.plugin_dir == old
        assert _spawned_directory() == (str(old), str(old_package))
    assert "OMNIBCI_PLUGIN_DIR" not in os.environ

    with TestClient(create_app()) as client:
        container = client.app.state.container
        assert container.plugin_profile_service.catalog.catalog.resolve_selection("effector", SELECTION).location == new_package
        assert container.plugin_package_service.store.plugin_dir == new
        assert _spawned_directory() == (str(new), str(new_package))
        entries = client.get("/api/v1/config").json()["data"]["entries"]
        assert not any(entry["restart_required"] for entry in entries)
    assert "OMNIBCI_PLUGIN_DIR" not in os.environ


@pytest.mark.parametrize("failure", [False, True])
def test_lifespan_preserves_environment_override_and_restores_it(plugin_environment, monkeypatch, failure):
    from omnibci.presentation import main
    _, new, _, new_package = plugin_environment
    original = str(new / ".." / "new")
    monkeypatch.setenv("OMNIBCI_PLUGIN_DIR", original)
    if failure:
        async def fail_build(**kwargs):
            assert os.environ["OMNIBCI_PLUGIN_DIR"] == str(new)
            raise RuntimeError("Startup failed")
        monkeypatch.setattr(main, "build_container", fail_build)
        with pytest.raises(RuntimeError, match="Startup failed"):
            with TestClient(create_app()):
                pass
    else:
        with TestClient(create_app()) as client:
            catalog = client.app.state.container.plugin_profile_service.catalog.catalog
            assert catalog.resolve_selection("effector", SELECTION).location == new_package
            assert os.environ["OMNIBCI_PLUGIN_DIR"] == str(new)
    assert os.environ["OMNIBCI_PLUGIN_DIR"] == original
