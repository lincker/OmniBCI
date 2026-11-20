"""Real publication and TCP admission reject an old run after an engine restart."""
import asyncio
import json
from threading import Event as ThreadEvent

import pytest

from omnibci.application.commands.engine_commands import StartEngineCommand, StopEngineCommand
from omnibci.application.node_configuration import NodeConfiguration, materialize_node_configuration
from omnibci.application.plugin_identity import PluginSelection
from omnibci.domain.channel_mapping import ProcessingInput
from omnibci.application.ports.plugin_configuration_catalog import PluginConfiguration
from omnibci.application.services.engine_service import NeuroEngineService
from omnibci.application.services.node_configuration_service import NodeConfigurationService
from omnibci.application.configuration.service import ConfigurationService
from omnibci.infrastructure.ipc.network import NetworkClient, NetworkServer
from omnibci.infrastructure.ipc.peer_identity import PeerIdentity
from omnibci.infrastructure.repositories.configuration_repository import TomlConfigurationRepository
from omnibci.shared.events import Event, EventType


_SLOTS = {
    "acquisition": "acquisition.source", "decoder": "processing.decoder",
    "epoch_detector": "processing.epoch_detector", "paradigm": "stimulation.paradigm",
    "trigger": "stimulation.trigger", "effector": "actuation.effector",
}


class MetadataCatalog:
    """The node catalog has no entries for capabilities owned by another role."""

    def __init__(self, kinds, *, server=False):
        self.definitions = {}
        self.calls = []
        for kind in kinds:
            properties = {}
            if kind == "acquisition":
                properties = {"sample_rate": {"type": "integer", "default": 500},
                              "channel_count": {"type": "integer", "default": 2}}
            elif kind in {"paradigm", "effector"}:
                properties = {"gain": {"type": "number", "default": 1.0}}
            self.definitions[kind] = PluginConfiguration(
                selection=PluginSelection(f"test.{kind}", kind, "1.0.0"), kind=kind,
                plugin_type=f"test_{kind}", label=kind, description=None,
                configuration_version=1, domain=_SLOTS[kind].split(".")[0],
                schema={"type": "object", "additionalProperties": False, "properties": properties},
                available=not server or kind not in {"paradigm", "trigger", "effector"},
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
        self.prepared_outputs = {}

    def request_stop(self):
        self._stopped.set()

    def is_module_running(self, module):
        return self._started.is_set() and not self._stopped.is_set()

    def is_peer_connected(self, peer_name):
        return False

    async def ensure_output_ready(self, role, snapshot):
        self.prepared_outputs[role] = snapshot

    async def set_actuation_active(self, active):
        assert active is False

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
    return PeerIdentity(snapshot["CONFIG.node_role"], snapshot["CONFIG.module_revisions"][snapshot["CONFIG.node_role"]], snapshot["CONFIG.engine_revision"], snapshot["CONFIG.generation"])


@pytest.mark.parametrize(("role", "kinds", "editable_kind", "event_type"), [
    ("stimulation", {"paradigm", "trigger"}, "paradigm", EventType.StartStimTrial),
    ("actuation", {"effector"}, "effector", EventType.ActuationCommand),
])
def test_restarted_engine_rejects_cached_node_configuration_and_accepts_fresh_fetch(
    tmp_path, role, kinds, editable_kind, event_type,
):
    server_catalog = MetadataCatalog(_SLOTS, server=True)
    profiles = ConfigurationService(
        TomlConfigurationRepository(tmp_path / "profiles"), server_catalog,
    )
    saved = {kind: profiles.create(kind, definition.selection, kind, {},
             **({"processing_input": ProcessingInput()} if kind == "decoder" else {}))
             for kind, definition in server_catalog.definitions.items()}
    profiles.repository.save_engine(profiles.propose_engine({}), expected_revision=None)
    for module in ("acquisition", "processing", "stimulation", "actuation"):
        selected = [{"slot": _SLOTS[kind], "profile_id": profile.profile_id}
                    for kind, profile in saved.items() if _SLOTS[kind].startswith(module + ".")]
        settings = profiles.default_module_settings(module)
        if module in ("stimulation", "actuation"):
            settings[f"{module.upper()}.auto_spawn"] = False
        profiles.repository.save_module(profiles.propose(
            module, selected, settings=settings,
        ), expected_revision=None)
    node_catalog = MetadataCatalog(kinds)

    async def scenario():
        engine = NeuroEngineService(HardwareFreeGraph, snapshot_provider=lambda: profiles.materialize(profiles.repository.get_activation_set()))
        publication = NodeConfigurationService(engine, profiles)
        servers, clients, tasks = [], [], []
        try:
            await engine.startup()
            await engine.handle_start_engine(StartEngineCommand())
            await engine.prepare_output(role)
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
            await engine.prepare_output(role)

            fresh = materialize_node_configuration(NodeConfiguration.from_dict(
                json.loads(json.dumps(publication.configuration(role)["configuration"]))), node_catalog)
            assert fresh["CONFIG.module_revisions"] == cached["CONFIG.module_revisions"]
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
