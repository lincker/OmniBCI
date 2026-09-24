"""Configuration remains repairable through the real application lifespan."""
from threading import Event
from time import monotonic, sleep
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from omnibci import bootstrap
from omnibci.conf import OmniBCISettings
from omnibci.presentation.main import create_app


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


def _assert_management_available(client):
    assert client.get("/healthz").json() == {"status": "ok"}
    for path in ("/config", "/config/plugin-profiles", "/config/plugin-catalog/acquisition",
                 "/config/runtime", "/engine/status", "/acquisition/status"):
        _data(client, path)


def test_dangling_retired_acquisition_selection_does_not_block_lifespan(startup_environment):
    environment = startup_environment
    environment.root.mkdir()
    old_selection = environment.root / "active.toml"
    old_selection.write_text(f'schema_version = 1\n[active]\nacquisition = "{uuid4()}"\n', encoding="utf-8")
    original = old_selection.read_bytes()
    settings_bytes = environment.settings._config_path.read_bytes()

    with TestClient(create_app()) as client:
        _assert_management_available(client)
        accepted = _data(client, "/config/activation")
        assert accepted is not None
        source = next(binding for binding in accepted["bindings"] if binding["slot"] == "acquisition.source")
        assert source["parameters"]["data_file_path"] == "recordings/startup.cnt"
        assert source["parameters"]["sample_rate"] == 256
        assert source["parameters"]["channel_number"] == 5
        assert _data(client, "/config/runtime")["accepted_revision"] == accepted["revision"]
        environment.builder.assert_not_called()

    assert old_selection.read_bytes() == original
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
        assert _data(client, "/acquisition/status")["sample_rate"] == 250
        assert client.post("/api/v1/engine/stop").status_code == 200
