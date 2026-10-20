"""Configuration remains repairable through the real application lifespan."""
import logging
from dataclasses import replace
from threading import Event
from time import monotonic, sleep
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from omnibci import bootstrap
from omnibci.application.plugin_profiles import ActivationSnapshot
from omnibci.conf import OmniBCISettings
from omnibci.presentation.main import create_app
from tests.plugin_configuration_helpers import accept_profiles


class MemoryEngine:
    """A graph with real readiness and shutdown barriers, without hardware."""

    def __init__(self):
        self.started = Event()
        self.stopped = Event()
        self.shutdown_complete = False
        self.event_bus = SimpleNamespace(register_event_listener=lambda *args: None)
        worker = SimpleNamespace(is_alive=lambda: not self.stopped.is_set())
        self.acquisition_worker = worker
        self.signal_processing_worker = worker
        self.peer_gateway = SimpleNamespace(peer=lambda name: SimpleNamespace(is_connected=False))

    @property
    def should_stop(self):
        return self.stopped.is_set()

    @should_stop.setter
    def should_stop(self, value):
        if value:
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
    import omnibci.conf as conf

    path = tmp_path / "settings.toml"
    path.write_text('''[ACQUISITION]
signal_source = "file_reader"
data_file_path = "recordings/startup.cnt"
sample_rate = 256
channel_number = 5
file_reader_chunk_size = 17
trigger = ""
[ALGORITHM]
decoder = "THUBCI_SSVEP_classic40FBCCA"
epoch_detector = "DEFAULT_EPOCH_DETECTOR"
window_duration = 4.25
offset_time = 0.15
baseline_duration = 0.4
filters = []
[STIMULATION]
paradigm = "STIMULATION_THUBCI_SSVEP_classic40"
auto_spawn = false
[ACTUATION]
enabled = false
auto_spawn = false
effector = ""
[NEUROENGINE]
queue_size = 29
''', encoding="utf-8")
    settings = OmniBCISettings(path)
    monkeypatch.setattr(conf, "settings", settings)
    root = tmp_path / "module-config"
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
    return accept_profiles(client.app.state.container.plugin_profile_service, [
        ("acquisition.source", "acquisition", "file_reader", {
            "data_file_path": "recordings/startup.cnt", "sample_rate": 256,
            "channel_number": 5, "file_reader_chunk_size": 17,
        }),
        ("processing.decoder", "decoder", "THUBCI_SSVEP_classic40FBCCA", {
            "window_duration": 4.25, "offset_time": 0.15,
        }),
        ("processing.epoch_detector", "epoch_detector", "DEFAULT_EPOCH_DETECTOR", {}),
        ("stimulation.paradigm", "paradigm", "STIMULATION_THUBCI_SSVEP_classic40", {}),
    ])


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
    settings_bytes = environment.settings._config_path.read_bytes()

    with TestClient(create_app()) as client:
        _assert_management_available(client)
        assert _data(client, "/config/activation") is None
        assert _data(client, "/config/plugin-profiles") == []
        assert _data(client, "/config/runtime")["accepted_revision"] is None
        environment.builder.assert_not_called()

    assert environment.settings._config_path.read_bytes() == settings_bytes


@pytest.mark.parametrize("include_host_settings", [False, True])
def test_invalid_initial_settings_can_be_repaired_without_restarting_backend(
    startup_environment, include_host_settings,
):
    environment = startup_environment
    environment.settings.set("ACQUISITION.sample_rate", "invalid")
    environment.settings.save()
    with TestClient(create_app()) as client:
        _assert_management_available(client)
        assert _data(client, "/config/activation") is None
        runtime = _data(client, "/config/runtime")
        assert runtime["accepted_revision"] is None
        assert runtime["applied_revision"] is None
        rejected = client.post("/api/v1/engine/start")
        assert rejected.status_code == 409, rejected.text
        assert rejected.json()["code"] == "ENGINE_NOT_READY"
        environment.builder.assert_not_called()

        bindings = []
        selections = (
            ("acquisition.source", "acquisition", "file_reader", {
                "data_file_path": "recordings/repaired.cnt", "sample_rate": 250,
                "channel_number": 9, "file_reader_chunk_size": 32,
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
        response = client.post("/api/v1/config/apply-plan", json={
            "bindings": bindings, "expected_revision": None,
            "include_host_settings": include_host_settings,
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
        accepted = _data(client, "/config/activation")
        assert accepted["revision"] == plan["target_revision"]
        environment.builder.assert_not_called()

        response = client.post("/api/v1/engine/start")
        assert response.status_code == 200, response.text
        environment.builder.assert_called_once()
        snapshot = environment.builder.call_args.kwargs["settings_snapshot"]
        assert snapshot["CONFIG.revision"] == accepted["revision"]
        assert snapshot["ACQUISITION.sample_rate"] == 250
        assert snapshot["ACQUISITION.channel_number"] == 9
        assert _data(client, "/config/runtime")["applied_revision"] == accepted["revision"]
        assert _data(client, "/engine/status")["running"] is True
        source_status = _data(client, "/acquisition/status")
        assert source_status["sample_rate"] is None
        assert source_status["channel_count"] is None
        assert source_status["metadata_available"] is False
        assert client.post("/api/v1/engine/stop").status_code == 200


def _references(snapshot):
    return [{"slot": binding.slot, "profile_id": binding.profile_id} for binding in snapshot.bindings]


def _assert_invalid_activation(client, environment, snapshot, operation, *, plan_status=200):
    repository = client.app.state.container.plugin_profile_service.repository
    previous = repository.get_activation()
    path = environment.root / "activation.toml"
    original = path.read_bytes()
    request = {"bindings": _references(snapshot), "expected_revision": previous.revision,
               "include_host_settings": True}
    plan = client.post("/api/v1/config/apply-plan", json=request)
    assert plan.status_code == plan_status, plan.text
    if plan_status == 200:
        assert plan.json()["data"]["errors"], plan.text
    if operation == "accept":
        response = client.put("/api/v1/config/activation", json=request)
        assert response.status_code == 422, response.text
        assert response.json()["code"] == "VALIDATION_ERROR"
        assert path.read_bytes() == original
    else:
        repository.save_activation(snapshot, expected_revision=previous.revision)
        response = client.post("/api/v1/engine/start")
        assert response.status_code == 409, response.text
        assert response.json()["code"] == "ENGINE_NOT_READY"
        assert _data(client, "/engine/status")["running"] is False
    environment.builder.assert_not_called()


@pytest.mark.parametrize("missing_slot", [
    None, "acquisition.source", "processing.decoder", "processing.epoch_detector",
    "stimulation.paradigm", "actuation.effector",
], ids=["empty", "acquisition", "decoder", "epoch", "paradigm", "enabled-effector"])
def test_engine_start_requires_only_acquisition_selection(startup_environment, missing_slot):
    environment = startup_environment
    with TestClient(create_app()) as client:
        previous = _select_profiles(client)
        if missing_slot == "actuation.effector":
            environment.settings.set("ACTUATION.enabled", True)
            environment.settings.save()
        bindings = tuple(binding for binding in previous.bindings
                         if missing_slot is not None and binding.slot != missing_slot)
        snapshot = ActivationSnapshot(environment.settings.snapshot(), bindings)
        request = {"bindings": _references(snapshot), "expected_revision": previous.revision,
                   "include_host_settings": True}
        plan = client.post("/api/v1/config/apply-plan", json=request)
        assert plan.status_code == 200, plan.text
        assert plan.json()["data"]["errors"] == []
        accepted = client.put("/api/v1/config/activation", json=request)
        assert accepted.status_code == 200, accepted.text
        started = client.post("/api/v1/engine/start")
        if missing_slot in (None, "acquisition.source"):
            assert started.status_code == 409, started.text
            assert "acquisition.source" in started.json()["message"]
            environment.builder.assert_not_called()
        else:
            assert started.status_code == 200, started.text
            environment.builder.assert_called_once()
            assert client.post("/api/v1/engine/stop").status_code == 200


@pytest.mark.parametrize("operation", ["accept", "start"])
def test_unavailable_source_profiles_can_be_saved_but_not_activated(startup_environment, monkeypatch, operation):
    with TestClient(create_app()) as client:
        _select_profiles(client)
        service = client.app.state.container.plugin_profile_service
        previous = service.repository.get_activation()
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
        snapshot = ActivationSnapshot(previous.settings, bindings)
        _assert_invalid_activation(client, startup_environment, snapshot, operation)


@pytest.mark.parametrize("operation", ["accept", "start"])
@pytest.mark.parametrize("invalid", ["parameters", "schema_version"])
def test_invalid_profile_parameters_or_schema_block_accept_and_start(startup_environment, operation, invalid):
    with TestClient(create_app()) as client:
        _select_profiles(client)
        service = client.app.state.container.plugin_profile_service
        previous = service.repository.get_activation()
        source = next(binding for binding in previous.bindings if binding.kind == "acquisition")
        profile = service.repository.get_profile(source.profile_id)
        changed = (replace(profile, parameters={**profile.parameters, "sample_rate": 0})
                   if invalid == "parameters" else replace(profile, configuration_version=profile.configuration_version + 1))
        saved = service.repository.save_profile(changed, expected_revision=profile.revision)
        bindings = tuple(replace(binding, parameters=saved.parameters, configuration_version=saved.configuration_version,
                                 profile_revision=saved.revision)
                         if binding.kind == "acquisition" else binding for binding in previous.bindings)
        snapshot = ActivationSnapshot(previous.settings, bindings)
        _assert_invalid_activation(client, startup_environment, snapshot, operation, plan_status=422)


def test_inactive_output_plugins_can_retain_portable_selections(startup_environment, monkeypatch):
    with TestClient(create_app()) as client:
        _select_profiles(client)
        service = client.app.state.container.plugin_profile_service
        previous = service.repository.get_activation()
        bindings = _references(previous)
        original_get = service.catalog.get

        def get(kind, selection):
            definition = original_get(kind, selection)
            return replace(definition, available=False) if kind in {"paradigm", "trigger", "effector"} else definition

        monkeypatch.setattr(service.catalog, "get", get)
        for kind, slot, alias in (("trigger", "stimulation.trigger", "neuroscan_scan45_trigger"),
                                  ("effector", "actuation.effector", "logger")):
            definition = next(item for item in _data(client, f"/config/plugin-catalog/{kind}")
                              if item["plugin_type"] == alias)
            response = client.post("/api/v1/config/plugin-profiles", json={
                "kind": kind, "selection": definition["selection"], "name": f"Portable {kind}", "parameters": {},
            })
            assert response.status_code == 201, response.text
            bindings.append({"slot": slot, "profile_id": response.json()["data"]["profile_id"]})
        request = {"bindings": bindings, "expected_revision": previous.revision}
        plan = client.post("/api/v1/config/apply-plan", json=request)
        assert plan.status_code == 200, plan.text
        assert plan.json()["data"]["errors"] == []
        accepted = client.put("/api/v1/config/activation", json=request)
        assert accepted.status_code == 200, accepted.text
        started = client.post("/api/v1/engine/start")
        assert started.status_code == 200, started.text
        startup_environment.builder.assert_called_once()
        assert client.post("/api/v1/engine/stop").status_code == 200


def test_backend_logging_covers_startup_and_shutdown(startup_environment, tmp_path):
    startup_environment.settings.set("LOGGING.level", "INFO")
    startup_environment.settings.set("LOGGING.file_path", str(tmp_path / "backend.log"))
    startup_environment.settings.save()
    app = create_app()

    with TestClient(app) as client:
        runtime = app.state.backend_logging
        assert runtime.is_running
        assert client.get("/healthz").json() == {"status": "ok"}
        logging.getLogger("omnibci.test_backend").info("Backend logging probe")

    assert not runtime.is_running
    content = runtime.file_path.read_text(encoding="utf-8")
    assert "OmniBCI API server started" in content
    assert "Backend logging probe" in content
    assert "OmniBCI API server stopped" in content


def test_backend_logging_closes_when_startup_fails(startup_environment, tmp_path, monkeypatch):
    from omnibci.presentation import main

    startup_environment.settings.set("LOGGING.file_path", str(tmp_path / "failed-startup.log"))
    startup_environment.settings.save()

    async def failed_container(**kwargs):
        logging.getLogger("omnibci.test_backend").error("Container startup failed")
        raise RuntimeError("Container unavailable")

    monkeypatch.setattr(main, "build_container", failed_container)
    app = create_app()
    with pytest.raises(RuntimeError, match="Container unavailable"), TestClient(app):
        pass

    runtime = app.state.backend_logging
    assert not runtime.is_running
    assert "Container startup failed" in runtime.file_path.read_text(encoding="utf-8")


def test_backend_logging_closes_when_shutdown_fails(startup_environment, tmp_path, monkeypatch):
    startup_environment.settings.set("LOGGING.file_path", str(tmp_path / "failed-shutdown.log"))
    startup_environment.settings.save()

    async def failed_shutdown():
        logging.getLogger("omnibci.test_backend").error("Engine shutdown failed")
        raise RuntimeError("Shutdown unavailable")

    app = create_app()
    with pytest.raises(RuntimeError, match="Shutdown unavailable"), TestClient(app):
        runtime = app.state.backend_logging
        monkeypatch.setattr(app.state.engine_service, "shutdown", failed_shutdown)

    assert not runtime.is_running
    assert "Engine shutdown failed" in runtime.file_path.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_lifespan_error_stops_services_before_logging(startup_environment, monkeypatch):
    app = create_app()

    async def shutdown():
        assert app.state.backend_logging.is_running

    stop = AsyncMock(side_effect=shutdown)
    with pytest.raises(RuntimeError, match="Lifespan interrupted"):
        async with app.router.lifespan_context(app):
            monkeypatch.setattr(app.state.engine_service, "shutdown", stop)
            raise RuntimeError("Lifespan interrupted")

    stop.assert_awaited_once()
    assert not app.state.backend_logging.is_running


def test_saved_logging_edit_during_startup_still_requires_backend_restart(
    startup_environment, monkeypatch, tmp_path,
):
    from omnibci.presentation import main

    initial_log = tmp_path / "loaded.log"
    changed_log = tmp_path / "changed.log"
    startup_environment.settings.set("LOGGING.file_path", str(initial_log))
    startup_environment.settings.save()
    create_logging = main.create_backend_logging
    loaded = {}

    def edit_after_logging_is_configured(*args, **kwargs):
        service = create_logging(*args, **kwargs)
        loaded["path"] = service.file_path
        startup_environment.settings.set("LOGGING.file_path", str(changed_log))
        startup_environment.settings.save()
        return service

    monkeypatch.setattr(main, "create_backend_logging", edit_after_logging_is_configured)
    app = create_app()
    with TestClient(app) as client:
        assert app.state.backend_logging.file_path == loaded["path"]
        _select_profiles(client)
        accepted = _data(client, "/config/activation")
        response = client.post("/api/v1/config/apply-plan", json={
            "bindings": [{"slot": binding["slot"], "profile_id": binding["profile_id"]}
                         for binding in accepted["bindings"]],
            "expected_revision": accepted["revision"], "include_host_settings": True,
            "refresh_slots": [],
        })
        assert response.status_code == 422, response.text
        assert "Backend restart required" in response.json()["message"]
        assert "LOGGING.file_path" in response.json()["message"]

    assert loaded["path"].is_file()
    assert not list(tmp_path.glob("changed*"))
