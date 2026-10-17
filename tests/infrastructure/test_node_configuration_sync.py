"""Real publication and TCP admission reject an old run after an engine restart."""
import asyncio
import json
from threading import Event as ThreadEvent
from types import SimpleNamespace

import pytest

from omnibci.application.commands.engine_commands import StartEngineCommand, StopEngineCommand
from omnibci.application.node_configuration import NodeConfiguration, materialize_node_configuration
from omnibci.application.plugin_profiles import PluginSelection
from omnibci.application.ports.plugin_configuration_catalog import PluginConfiguration
from omnibci.application.services.engine_service import NeuroEngineService
from omnibci.application.services.node_configuration_service import NodeConfigurationService
from omnibci.application.services.plugin_profile_service import PluginProfileService
from omnibci.infrastructure.ipc.network import NetworkClient, NetworkServer
from omnibci.infrastructure.ipc.peer_identity import PeerIdentity
from omnibci.infrastructure.repositories.plugin_profile_repository import TomlPluginProfileRepository
from omnibci.shared.events import Event, EventType


_SLOTS = {
    "acquisition": "acquisition.source", "decoder": "processing.decoder",
    "epoch_detector": "processing.epoch_detector", "paradigm": "stimulation.paradigm",
    "trigger": "stimulation.trigger", "effector": "actuation.effector",
}


class MetadataCatalog:
    """The node catalog has no entries for capabilities owned by another role."""

    def __init__(self, kinds, *, backend=False):
        self.definitions = {}
        self.calls = []
        for kind in kinds:
            properties = {}
            if kind == "acquisition":
                properties = {"sample_rate": {"type": "integer", "default": 500},
                              "channel_number": {"type": "integer", "default": 2}}
            elif kind in {"paradigm", "effector"}:
                properties = {"gain": {"type": "number", "default": 1.0}}
            self.definitions[kind] = PluginConfiguration(
                selection=PluginSelection(f"test.{kind}", kind, "1.0.0"), kind=kind,
                plugin_type=f"test_{kind}", label=kind, description=None,
                configuration_version=1,
                schema={"type": "object", "additionalProperties": False, "properties": properties},
                available=not backend or kind not in {"paradigm", "trigger", "effector"},
            )

    def get(self, kind, selection):
        self.calls.append(kind)
        definition = self.definitions[kind]
        assert definition.selection == selection
        return definition


class HardwareFreeGraph:
    """Exercise the actual engine-service thread and generation lifecycle."""

    shutdown_complete = True

    def __init__(self, snapshot):
        self._started = ThreadEvent()
        self._stopped = ThreadEvent()
        self.acquisition_worker = SimpleNamespace(
            is_alive=lambda: self._started.is_set() and not self._stopped.is_set(),
        )
        self.signal_processing_worker = self.acquisition_worker

    @property
    def should_stop(self):
        return self._stopped.is_set()

    @should_stop.setter
    def should_stop(self, value):
        if value:
            self._stopped.set()

    def run(self):
        self._started.set()
        self._stopped.wait()

    def wait_until_started(self, timeout):
        if not self._started.wait(timeout):
            raise TimeoutError("Test graph did not start")


async def wait_for(predicate):
    async def poll():
        while not predicate():
            await asyncio.sleep(0.001)
    await asyncio.wait_for(poll(), 2)


def identity(snapshot):
    return PeerIdentity(snapshot["CONFIG.node_role"], snapshot["CONFIG.revision"], snapshot["CONFIG.generation"])


@pytest.mark.parametrize(("role", "kinds", "editable_kind", "event_type"), [
    ("stimulation", {"paradigm", "trigger"}, "paradigm", EventType.StartStimTrial),
    ("actuation", {"effector"}, "effector", EventType.ActuationCommand),
])
def test_restarted_engine_rejects_cached_node_configuration_and_accepts_fresh_fetch(
    tmp_path, role, kinds, editable_kind, event_type,
):
    backend_catalog = MetadataCatalog(_SLOTS, backend=True)
    profiles = PluginProfileService(
        TomlPluginProfileRepository(tmp_path / "profiles"), backend_catalog,
        settings_source=lambda: {
            "STIMULATION.auto_spawn": False, "ACTUATION.auto_spawn": False, "ACTUATION.enabled": True,
        },
    )
    saved = {kind: profiles.create(kind, definition.selection, kind, {})
             for kind, definition in backend_catalog.definitions.items()}
    accepted = profiles.propose([{"slot": _SLOTS[kind], "profile_id": profile.profile_id}
                                 for kind, profile in saved.items()])
    profiles.accept(accepted, expected_revision=None)
    node_catalog = MetadataCatalog(kinds)

    async def scenario():
        engine = NeuroEngineService(HardwareFreeGraph, snapshot_provider=profiles.resolve_settings)
        publication = NodeConfigurationService(engine, profiles)
        servers, clients, tasks = [], [], []
        try:
            await engine.startup()
            await engine.handle_start_engine(StartEngineCommand())
            first = publication.configuration(role)
            assert first["state"] == "running"
            cached = materialize_node_configuration(
                NodeConfiguration.from_dict(json.loads(json.dumps(first["configuration"]))), node_catalog,
            )
            assert {item["kind"] for item in cached["PLUGINS.bindings"]} == kinds
            assert set(node_catalog.calls) == kinds

            profile = saved[editable_kind]
            profiles.update(profile.profile_id, name=profile.name, parameters={"gain": 2.0},
                            expected_revision=profile.revision)
            assert profiles.repository.get_profile(profile.profile_id).parameters["gain"] == 2.0
            unchanged = publication.configuration(role)
            assert unchanged == first
            assert next(item for item in cached["PLUGINS.bindings"]
                        if item["kind"] == editable_kind)["parameters"]["gain"] == 1.0

            old_server = NetworkServer(host="127.0.0.1", port=0, identity=identity(cached), max_clients=1)
            servers.append(old_server)
            await old_server.start()
            old_port = old_server.server.sockets[0].getsockname()[1]
            old_client = NetworkClient(server_port=old_port, identity=identity(cached), reconnect=False)
            clients.append(old_client)
            old_task = asyncio.create_task(old_client.start())
            tasks.append(old_task)
            await wait_for(lambda: old_server.is_connected and old_client.is_connected)
            await old_server.stop()
            await asyncio.wait_for(old_task, 2)
            await engine.handle_stop_engine(StopEngineCommand())
            await engine.handle_start_engine(StartEngineCommand())

            fresh = materialize_node_configuration(NodeConfiguration.from_dict(
                json.loads(json.dumps(publication.configuration(role)["configuration"]))), node_catalog)
            assert fresh["CONFIG.revision"] == cached["CONFIG.revision"]
            assert fresh["CONFIG.generation"] != cached["CONFIG.generation"]
            assert fresh["PLUGINS.bindings"] == cached["PLUGINS.bindings"]
            server = NetworkServer(host="127.0.0.1", port=0, identity=identity(fresh), max_clients=1)
            servers.append(server)
            await server.start()
            port = server.server.sockets[0].getsockname()[1]
            stale_client = NetworkClient(server_port=port, identity=identity(cached), reconnect=False)
            clients.append(stale_client)
            with pytest.raises(asyncio.IncompleteReadError):
                await asyncio.wait_for(stale_client.start(), 2)
            assert not server.is_connected
            assert not stale_client.is_connected
            assert not await server.send_event(Event(event_type, {"target_id": 1}))
            await wait_for(lambda: not server._pending_clients and not server._closing_writers)

            received = []
            fresh_client = NetworkClient(server_port=port, identity=identity(fresh), reconnect=False)
            clients.append(fresh_client)
            fresh_client.set_inbound_handler(lambda event: received.append(event) is None)
            fresh_task = asyncio.create_task(fresh_client.start())
            tasks.append(fresh_task)
            await wait_for(lambda: server.is_connected and fresh_client.is_connected)
            assert await server.send_event(Event(event_type, {"target_id": 1}))
            await wait_for(lambda: received)
            assert received[0].type == event_type
            assert set(node_catalog.calls) == kinds
        finally:
            for client in clients:
                await client.stop()
            for server in servers:
                await server.stop()
            await asyncio.gather(*tasks, return_exceptions=True)
            await engine.shutdown()

    asyncio.run(scenario())
