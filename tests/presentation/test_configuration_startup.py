"""Configuration remains repairable through the real application lifespan."""
import logging
from dataclasses import replace
from copy import deepcopy
from threading import Event
from time import monotonic, sleep
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from omnibci import bootstrap
from omnibci.application.configuration.models import ModuleActivation
from omnibci.infrastructure.repositories.configuration_repository import TomlConfigurationRepository
from omnibci.presentation.main import create_app
from tests.plugin_configuration_helpers import accept_profiles


class MemoryEngine:
    """A graph with real readiness and shutdown barriers, without hardware."""

    def __init__(self):
        self.started = Event()
        self.stopped = Event()
        self.shutdown_complete = False
        self.modules = set()
        self.module_snapshots = {}
        self.actuation_active = False

    def subscribe(self, event_type, handler):
        pass

    def is_peer_connected(self, peer_name):
        return False

    def is_current_peer_channel(self, peer_name, channel_id):
        return False

    def is_module_running(self, module):
        return module in self.modules

    async def start_module(self, module, snapshot):
        self.module_snapshots[module] = deepcopy(snapshot)
        self.modules.add(module)

    async def stop_module(self, module):
        self.modules.discard(module)

    async def set_actuation_active(self, active):
        self.actuation_active = active

    def request_stop(self):
        self.stopped.set()

    def run(self):
        self.started.set()
        self.stopped.wait()
        self.shutdown_complete = True

    def wait_until_started(self, timeout):
        if not self.started.wait(timeout):
            raise RuntimeError("Memory engine did not start")


@pytest.fixture
def startup_environment(tmp_path, monkeypatch):
    root = tmp_path / "module-config"
    root.mkdir()
    (root / "settings.toml").write_text('[LOGGING]\nlevel = "INFO"\n', encoding="utf-8")
    settings = TomlConfigurationRepository(root)
    monkeypatch.setenv("OMNIBCI_CONFIG_DIR", str(root))
    monkeypatch.setenv("OMNIBCI_PLUGIN_DIR", str(tmp_path / "plugins"))
    for key in ("OMNIBCI_MARKET_URL", "OMNIBCI_MARKET_REPOSITORY_ID", "OMNIBCI_MARKET_TRUSTED_KEYS"):
        monkeypatch.delenv(key, raising=False)
    builder = Mock(side_effect=lambda **kwargs: SimpleNamespace(build=MemoryEngine))
    monkeypatch.setattr(bootstrap, "NeuroEngineBuilder", builder)
    return SimpleNamespace(settings=settings, root=root, builder=builder)


def _data(client, path):
    response = client.get(f"/api/v1{path}")
    assert response.status_code == 200, response.text
    return response.json()["data"]


def _select_profiles(client):
    return accept_profiles(client.app.state.container.configuration_service, [
        ("acquisition.source", "acquisition", "file_reader", {
            "data_file_path": "recordings/startup.cnt", "sample_rate": 256,
            "channel_count": 5, "file_reader_chunk_size": 17,
        }),
        ("processing.decoder", "decoder", "THUBCI_SSVEP_classic40FBCCA", {
            "window_duration": 4.25, "offset_time": 0.15,
        }),
        ("processing.epoch_detector", "epoch_detector", "DEFAULT_EPOCH_DETECTOR", {}),
        ("stimulation.paradigm", "paradigm", "STIMULATION_THUBCI_SSVEP_classic40", {}),
    ], module_settings={
        "processing": {"ALGORITHM.baseline_duration": 0.4},
        "stimulation": {"STIMULATION.auto_spawn": False},
    })


@pytest.mark.parametrize("desktop", [False, True])
def test_process_stop_policy_is_injected_into_all_desktop_modules(startup_environment, desktop):
    with TestClient(create_app(desktop=desktop)) as client:
        _select_profiles(client)
        container = client.app.state.container
        assert container.stimulation_process_manager._allow_process_termination is (not desktop)
        assert container.actuation_process_manager._allow_process_termination is (not desktop)
        assert client.post("/api/v1/engine/start").status_code == 200
        assert startup_environment.builder.call_args.kwargs["allow_process_termination"] is (not desktop)


def _assert_management_available(client):
    assert client.get("/healthz").json() == {"status": "ok"}
    for path in ("/config", "/config/plugin-profiles", "/config/plugin-catalog/acquisition",
                 "/config/runtime", "/engine/status", "/acquisition/status"):
        _data(client, path)


def test_fresh_lifespan_leaves_module_configuration_unselected(startup_environment):
    environment = startup_environment
    settings_bytes = (environment.root / "settings.toml").read_bytes()

    with TestClient(create_app()) as client:
        _assert_management_available(client)
        assert all(item["revision"] is None for item in _data(client, "/config/modules")["modules"].values())
        assert _data(client, "/config/plugin-profiles") == []
        assert all(value is None for value in _data(client, "/config/runtime")["accepted"].values())
        environment.builder.assert_not_called()

    assert (environment.root / "settings.toml").read_bytes() == settings_bytes


def test_initial_module_configuration_can_be_applied_without_restarting_server(
    startup_environment, monkeypatch,
):
    environment = startup_environment
    with TestClient(create_app()) as client:
        _assert_management_available(client)
        assert all(item["revision"] is None for item in _data(client, "/config/modules")["modules"].values())
        runtime = _data(client, "/config/runtime")
        assert all(value is None for value in runtime["accepted"].values())
        assert runtime["applied"] is None
        started = client.post("/api/v1/engine/start")
        assert started.status_code == 200, started.text
        environment.builder.assert_called_once()
        assert _data(client, "/engine/status")["acquisition_alive"] is False

        bindings = []
        selections = (
            ("acquisition.source", "acquisition", "file_reader", {
                "data_file_path": "recordings/repaired.cnt", "sample_rate": 250,
                "channel_count": 9, "file_reader_chunk_size": 32,
            }),
            ("processing.decoder", "decoder", "THUBCI_SSVEP_classic40FBCCA", {
                "window_duration": 3.0, "offset_time": 0.14,
            }),
            ("processing.epoch_detector", "epoch_detector", "DEFAULT_EPOCH_DETECTOR", {}),
            ("stimulation.paradigm", "paradigm", "STIMULATION_THUBCI_SSVEP_classic40", {}),
        )
        for slot, kind, plugin_type, parameters in selections:
            definition = next(item for item in _data(client, f"/config/plugin-catalog/{kind}")
                              if item["plugin_type"] == plugin_type)
            response = client.post("/api/v1/config/plugin-profiles", json={
                "kind": kind, "selection": definition["selection"],
                "name": f"Repaired {kind}", "parameters": parameters,
            })
            assert response.status_code == 201, response.text
            bindings.append({"slot": slot, "profile_id": response.json()["data"]["profile_id"]})
        response = client.post("/api/v1/config/apply-plan", json={"settings": {},
            "module": "acquisition", "bindings": [bindings[0]], "expected_revision": None,
        })
        assert response.status_code == 200, response.text
        plan = response.json()["data"]
        assert plan["errors"] == []
        assert plan["strategy"] == "select_on_start"
        operation_id = str(uuid4())
        response = client.post("/api/v1/config/apply", json={
            "plan_id": plan["plan_id"], "operation_id": operation_id,
        })
        assert response.status_code == 202, response.text
        deadline = monotonic() + 5
        while True:
            operation = _data(client, f"/config/operations/{operation_id}")
            if operation["status"] not in ("queued", "applying"):
                break
            assert monotonic() < deadline, operation
            sleep(0.01)
        assert operation["status"] == "succeeded", operation
        accepted = _data(client, "/config/modules/acquisition")
        assert accepted["revision"] == plan["target_revisions"]["acquisition"]
        environment.builder.assert_called_once()

        monkeypatch.setattr(client.app.state.container.acquisition_service, "handle_connect", Mock())
        response = _connect_current(client)
        assert response.status_code == 200, response.text
        assert response.json()["data"]["status"] == "succeeded", response.text
        environment.builder.assert_called_once()
        snapshot = client.app.state.engine_service._engine.module_snapshots["acquisition"]
        assert snapshot["CONFIG.module_revisions"]["acquisition"] == accepted["revision"]
        assert snapshot["ACQUISITION.sample_rate"] == 250
        assert snapshot["ACQUISITION.channel_count"] == 9
        assert _data(client, "/config/runtime")["applied"]["acquisition"] == accepted["revision"]
        assert _data(client, "/engine/status")["running"] is True
        source_status = _data(client, "/acquisition/status")
        assert source_status["sample_rate"] is None
        assert source_status["channel_count"] is None
        assert source_status["metadata_available"] is False
        assert client.post("/api/v1/engine/stop").status_code == 200


def _references(snapshot):
    return [{"slot": binding.slot, "profile_id": binding.profile_id} for binding in snapshot.bindings]


def _connect_current(client):
    selected = _data(client, "/config/modules/acquisition")
    return client.post("/api/v1/acquisition/connect", json={
        "operation_id": str(uuid4()),
        "configuration": {
            "expected_revision": selected["revision"], "settings": selected["settings"],
            "bindings": [{key: binding[key] for key in (
                "slot", "selection", "parameters", "configuration_version", "processing_input",
            ) if key in binding} for binding in selected["bindings"]],
        },
    })


def _apply_plan(client, plan):
    operation_id = str(uuid4())
    response = client.post("/api/v1/config/apply", json={"plan_id": plan["plan_id"], "operation_id": operation_id})
    assert response.status_code == 202, response.text
    deadline = monotonic() + 5
    while True:
        result = _data(client, f"/config/operations/{operation_id}")
        if result["status"] not in ("queued", "applying"):
            assert result["status"] == "succeeded", result
            return result
        assert monotonic() < deadline, result
        sleep(.01)


def _assert_invalid_activation(client, environment, snapshot, operation, *, plan_status=200):
    repository = client.app.state.container.configuration_service.repository
    previous = repository.get_module("acquisition")
    path = environment.root / "activation" / "acquisition.toml"
    original = path.read_bytes()
    request = {"settings": {}, "module": "acquisition", "bindings": _references(snapshot), "expected_revision": previous.revision}
    plan = client.post("/api/v1/config/apply-plan", json=request)
    assert plan.status_code == plan_status, plan.text
    if plan_status == 200:
        assert plan.json()["data"]["errors"], plan.text
    if operation == "accept":
        if plan_status == 200:
            response = client.post("/api/v1/config/apply", json={
                "plan_id": plan.json()["data"]["plan_id"], "operation_id": str(uuid4())})
            assert response.status_code == 422, response.text
            assert response.json()["code"] == "VALIDATION_ERROR"
        assert path.read_bytes() == original
    else:
        repository.save_module(snapshot, expected_revision=previous.revision)
        response = client.post("/api/v1/engine/start")
        assert response.status_code == 200, response.text
        response = _connect_current(client)
        assert response.status_code == 200, response.text
        assert response.json()["data"]["status"] == "failed", response.text
        assert _data(client, "/engine/status")["running"] is True
        assert _data(client, "/engine/status")["acquisition_alive"] is False
    if operation == "accept":
        environment.builder.assert_not_called()
    else:
        environment.builder.assert_called_once()


@pytest.mark.parametrize("missing_slot", [
    None, "acquisition.source", "processing.decoder", "processing.epoch_detector",
    "stimulation.paradigm", "actuation.effector",
], ids=["empty", "acquisition", "decoder", "epoch", "paradigm", "effector"])
def test_engine_start_does_not_require_any_module_selection(startup_environment, missing_slot):
    environment = startup_environment
    with TestClient(create_app()) as client:
        previous = _select_profiles(client)
        service = client.app.state.container.configuration_service
        for module, activation in previous.modules.items():
            if activation is None:
                continue
            references = [{"slot": binding.slot, "profile_id": binding.profile_id} for binding in activation.bindings
                          if missing_slot is not None and binding.slot != missing_slot]
            response = client.post("/api/v1/config/apply-plan", json={"settings": {},
                "module": module, "bindings": references, "expected_revision": activation.revision})
            assert response.status_code == 200, response.text
            _apply_plan(client, response.json()["data"])
        if missing_slot == "actuation.effector":
            service.repository.save_module(service.propose("actuation", [], settings=service.default_module_settings("actuation")), expected_revision=None)
        started = client.post("/api/v1/engine/start")
        assert started.status_code == 200, started.text
        environment.builder.assert_called_once()
        status = _data(client, "/engine/status")
        assert status["running"] is True
        assert status["acquisition_alive"] is status["processor_alive"] is status["actuation_alive"] is False
        if missing_slot in (None, "acquisition.source"):
            response = _connect_current(client)
            assert response.status_code == 200, response.text
            assert response.json()["data"]["status"] == "failed", response.text
            assert "acquisition.source" in response.json()["data"]["error"]
        assert client.post("/api/v1/engine/stop").status_code == 200


@pytest.mark.parametrize("operation", ["accept", "start"])
def test_unavailable_source_profiles_can_be_selected_but_not_started(startup_environment, monkeypatch, operation):
    with TestClient(create_app()) as client:
        _select_profiles(client)
        service = client.app.state.container.configuration_service
        previous = service.repository.get_module("acquisition")
        source = next(binding for binding in previous.bindings if binding.kind == "acquisition")
        original_get = service.catalog.get

        def get(kind, selection):
            definition = original_get(kind, selection)
            return replace(definition, available=False) if kind == "acquisition" else definition

        monkeypatch.setattr(service.catalog, "get", get)
        response = client.post("/api/v1/config/plugin-profiles", json={
            "kind": "acquisition", "selection": source.selection.to_dict(),
            "name": "Portable source", "parameters": source.parameters,
        })
        assert response.status_code == 201, response.text
        profile = response.json()["data"]
        response = client.put(f"/api/v1/config/plugin-profiles/{profile['profile_id']}", json={
            "name": "Portable source saved", "parameters": profile["parameters"],
            "expected_revision": profile["revision"],
        })
        assert response.status_code == 200, response.text
        saved = response.json()["data"]
        bindings = tuple(replace(binding, profile_id=saved["profile_id"], profile_revision=saved["revision"])
                         if binding.kind == "acquisition" else binding for binding in previous.bindings)
        snapshot = ModuleActivation("acquisition", previous.settings, bindings)
        if operation == "accept":
            response = client.post("/api/v1/config/apply-plan", json={"settings": {},
                "module": "acquisition", "bindings": _references(snapshot), "expected_revision": previous.revision,
            })
            assert response.status_code == 200, response.text
            assert response.json()["data"]["errors"] == []
            _apply_plan(client, response.json()["data"])
        else:
            service.repository.save_module(snapshot, expected_revision=previous.revision)
        assert client.post("/api/v1/engine/start").status_code == 200
        response = _connect_current(client)
        assert response.status_code == 200, response.text
        assert response.json()["data"]["status"] == "failed", response.text
        assert "unavailable" in response.json()["data"]["error"]
        status = _data(client, "/engine/status")
        assert status["running"] is True and status["acquisition_alive"] is False


@pytest.mark.parametrize("operation", ["accept", "start"])
@pytest.mark.parametrize("invalid", ["parameters", "schema_version"])
def test_invalid_profile_parameters_or_schema_block_accept_and_start(startup_environment, operation, invalid):
    with TestClient(create_app()) as client:
        _select_profiles(client)
        service = client.app.state.container.configuration_service
        previous = service.repository.get_module("acquisition")
        source = next(binding for binding in previous.bindings if binding.kind == "acquisition")
        profile = service.repository.get_profile(source.profile_id)
        changed = (replace(profile, parameters={**profile.parameters, "sample_rate": 0})
                   if invalid == "parameters" else replace(profile, configuration_version=profile.configuration_version + 1))
        saved = service.repository.save_profile(changed, expected_revision=profile.revision)
        bindings = tuple(replace(binding, parameters=saved.parameters, configuration_version=saved.configuration_version,
                                 profile_revision=saved.revision)
                         if binding.kind == "acquisition" else binding for binding in previous.bindings)
        snapshot = ModuleActivation("acquisition", previous.settings, bindings)
        _assert_invalid_activation(client, startup_environment, snapshot, operation, plan_status=422)


def test_inactive_output_plugins_can_retain_portable_selections(startup_environment, monkeypatch):
    with TestClient(create_app()) as client:
        _select_profiles(client)
        service = client.app.state.container.configuration_service
        original_get = service.catalog.get
        def get(kind, selection):
            definition = original_get(kind, selection)
            return replace(definition, available=False) if kind in {"paradigm", "trigger", "effector"} else definition
        monkeypatch.setattr(service.catalog, "get", get)
        for kind, slot, alias in (("trigger", "stimulation.trigger", "neuroscan_scan45_trigger"),
                                  ("effector", "actuation.effector", "logger")):
            definition = next(item for item in _data(client, f"/config/plugin-catalog/{kind}") if item["plugin_type"] == alias)
            response = client.post("/api/v1/config/plugin-profiles", json={
                "kind": kind, "selection": definition["selection"], "name": f"Portable {kind}", "parameters": {}})
            assert response.status_code == 201, response.text
            module = slot.split(".")[0]
            previous = service.repository.get_module(module)
            references = _references(previous) if previous else []
            references.append({"slot": slot, "profile_id": response.json()["data"]["profile_id"]})
            plan = client.post("/api/v1/config/apply-plan", json={"settings": {},
                "module": module, "bindings": references, "expected_revision": previous.revision if previous else None})
            assert plan.status_code == 200, plan.text
            assert plan.json()["data"]["errors"] == []
            _apply_plan(client, plan.json()["data"])
        started = client.post("/api/v1/engine/start")
        assert started.status_code == 200, started.text
        startup_environment.builder.assert_called_once()
        assert client.post("/api/v1/engine/stop").status_code == 200


def test_server_logging_covers_startup_and_shutdown(startup_environment, tmp_path):
    startup_environment.settings.save_server({"LOGGING.file_path": str(tmp_path / "server.log")},
        expected_revision=startup_environment.settings.read_server()[1])
    app = create_app()

    with TestClient(app) as client:
        runtime = app.state.server_logging
        assert runtime.is_running
        assert client.get("/healthz").json() == {"status": "ok"}
        logging.getLogger("omnibci.test_server").info("Server logging probe")

    assert not runtime.is_running
    content = runtime.file_path.read_text(encoding="utf-8")
    assert "OmniBCI API server started" in content
    assert "Server logging probe" in content
    assert "OmniBCI API server stopped" in content


def test_server_logging_closes_when_startup_fails(startup_environment, tmp_path, monkeypatch):
    from omnibci.presentation import main

    startup_environment.settings.save_server({"LOGGING.file_path": str(tmp_path / "failed-startup.log")},
        expected_revision=startup_environment.settings.read_server()[1])

    async def failed_container(**kwargs):
        logging.getLogger("omnibci.test_server").error("Container startup failed")
        raise RuntimeError("Container unavailable")

    monkeypatch.setattr(main, "build_container", failed_container)
    app = create_app()
    with pytest.raises(RuntimeError, match="Container unavailable"), TestClient(app):
        pass

    runtime = app.state.server_logging
    assert not runtime.is_running
    assert "Container startup failed" in runtime.file_path.read_text(encoding="utf-8")


def test_server_logging_closes_when_shutdown_fails(startup_environment, tmp_path, monkeypatch):
    startup_environment.settings.save_server({"LOGGING.file_path": str(tmp_path / "failed-shutdown.log")},
        expected_revision=startup_environment.settings.read_server()[1])

    async def failed_shutdown():
        logging.getLogger("omnibci.test_server").error("Engine shutdown failed")
        raise RuntimeError("Shutdown unavailable")

    app = create_app()
    with pytest.raises(RuntimeError, match="Shutdown unavailable"), TestClient(app):
        runtime = app.state.server_logging
        monkeypatch.setattr(app.state.engine_service, "shutdown", failed_shutdown)

    assert not runtime.is_running
    assert "Engine shutdown failed" in runtime.file_path.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_lifespan_error_stops_services_before_logging(startup_environment, monkeypatch):
    app = create_app()

    async def shutdown():
        assert app.state.server_logging.is_running

    stop = AsyncMock(side_effect=shutdown)
    with pytest.raises(RuntimeError, match="Lifespan interrupted"):
        async with app.router.lifespan_context(app):
            monkeypatch.setattr(app.state.engine_service, "shutdown", stop)
            raise RuntimeError("Lifespan interrupted")

    stop.assert_awaited_once()
    assert not app.state.server_logging.is_running


def test_saved_logging_edit_during_startup_still_requires_server_restart(
    startup_environment, monkeypatch, tmp_path,
):
    from omnibci.presentation import main

    initial_log = tmp_path / "loaded.log"
    changed_log = tmp_path / "changed.log"
    startup_environment.settings.save_server({"LOGGING.file_path": str(initial_log)},
        expected_revision=startup_environment.settings.read_server()[1])
    create_logging = main.create_server_logging
    loaded = {}

    def edit_after_logging_is_configured(*args, **kwargs):
        service = create_logging(*args, **kwargs)
        loaded["path"] = service.file_path
        startup_environment.settings.save_server({"LOGGING.file_path": str(changed_log)},
            expected_revision=startup_environment.settings.read_server()[1])
        return service

    monkeypatch.setattr(main, "create_server_logging", edit_after_logging_is_configured)
    app = create_app()
    with TestClient(app) as client:
        assert app.state.server_logging.file_path == loaded["path"]
        response = client.post("/api/v1/config/apply-plan", json={
            "module": "engine", "bindings": [], "expected_revision": None,
            "settings": {"NEUROENGINE.queue_size": 42},
        })
        assert response.status_code == 200, response.text
        assert not response.json()["data"]["errors"]
        entries = client.get("/api/v1/config").json()["data"]["entries"]
        entry = next(item for item in entries if item["key"] == "LOGGING.file_path")
        assert entry["value"] == str(changed_log)
        assert entry["active_value"] == str(initial_log)
        assert entry["restart_required"]

    assert loaded["path"].is_file()
    assert not list(tmp_path.glob("changed*"))
