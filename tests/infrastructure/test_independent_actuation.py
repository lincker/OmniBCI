"""Verify the public Actuation lifecycle using the bundled Effector and real sockets."""
import asyncio
import os
import signal
import sys
from uuid import uuid4

import httpx
import pytest
import uvicorn

from omnibci.application.events import TrialOutcome
from omnibci.domain.classification import Classification
from omnibci.presentation.main import create_app
from omnibci.shared.events import Event, EventType
from tests.plugin_configuration_helpers import builtin_selection


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.parametrize("managed", [True, False], ids=["managed-process", "external-launcher"])
async def test_actuation_owns_its_session_across_engine_and_operator_stops(managed, unused_tcp_port_factory, tmp_path):
    port, controller_port = unused_tcp_port_factory(), unused_tcp_port_factory()
    address = f"http://127.0.0.1:{port}"
    app = create_app(api_token="")
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", ws="none"))
    server_task = asyncio.create_task(server.serve())
    node = None

    async def until(predicate):
        async with asyncio.timeout(15):
            while not predicate():
                if server_task.done():
                    server_task.result()
                    pytest.fail("Server exited before the expected state")
                if node is not None and node.returncode is not None:
                    pytest.fail(f"External node exited with {node.returncode}")
                await asyncio.sleep(.02)

    async def disconnect_node():
        if sys.platform == "win32":
            node.terminate()
        else:
            node.send_signal(signal.SIGINT)
        await asyncio.wait_for(node.wait(), 10)

    try:
        await until(lambda: server.started)
        container = app.state.container
        owner = container.actuation_service
        profiles = container.configuration_service
        definition = profiles.catalog.get("effector", builtin_selection("effector", "logger"))
        config = {"expected_revision": None, "bindings": [{"slot": "actuation.effector",
                  "selection": definition.selection.to_dict(), "parameters": {},
                  "configuration_version": definition.configuration_version}],
                  "settings": {**profiles.default_module_settings("actuation"), "ACTUATION.auto_spawn": managed,
                               "ACTUATION.server_port": controller_port}}
        acks = []
        container.ipc_runtime.subscribe(EventType.ActuationAck, lambda event: acks.append(event.data))
        async with httpx.AsyncClient(base_url=address + "/api/v1/", timeout=25, trust_env=False) as client:
            async def start():
                response = await client.post("actuation/start", json={"operation_id": str(uuid4()), "configuration": config})
                response.raise_for_status()
                operation = response.json()["data"]
                assert operation["status"] == "succeeded", operation
                config["expected_revision"] = operation["target_revisions"]["actuation"]
                return operation["result"]["action"]

            async def post(path):
                response = await client.post(path)
                response.raise_for_status()

            async def status():
                return (await client.get("actuation/status")).json()["data"]

            assert await start() == ("started" if managed else "prepared")
            state, session, revision = owner.owner_identity()
            assert state == ("active" if managed else "prepared")
            snapshot = owner.applied_snapshot
            assert snapshot["CONFIG.engine_revision"] is None and profiles.repository.get_engine() is None
            if not managed:
                await post("actuation/prepare")
                assert await start() == "prepared"
                node = await asyncio.create_subprocess_exec(
                    sys.executable, "scripts/run_actuation.py", "--server", address,
                    env={**os.environ, "OMNIBCI_PLUGIN_DIR": str(tmp_path / "external-plugins")},
                )
                await until(lambda: owner.is_connected)
                assert not (await status())["active"]
                assert await start() == "started"
            connection = container.ipc_runtime.peer_connection_token("actuation")
            assert connection and owner.is_active and not (await status())["engine_linked"]
            assert await start() == "started"
            await post("engine/start")
            assert (await status())["engine_linked"]
            engine = container.engine_service
            def publish(generation, trial):
                engine._engine.event_bus.put_event(Event(EventType.TrialCompleted, TrialOutcome(generation, trial, Classification(target_id=1, event_id=1))))
            old_generation = engine.generation
            publish(old_generation, 1)
            await until(lambda: len(acks) == 1)
            assert acks[0]["origin"]["generation"] == old_generation
            assert acks[0]["execution"]["generation"] == session
            assert acks[0]["execution"]["operation_id"] == snapshot["CONFIG.operation_id"]
            await post("engine/stop")
            assert owner.is_active and not (await status())["engine_linked"]
            await post("engine/start")
            assert engine.generation != old_generation
            assert container.ipc_runtime.peer_connection_token("actuation") == connection
            assert owner.owner_identity() == ("active", session, revision)
            publish(old_generation, 2)
            publish(engine.generation, 3)
            await until(lambda: len(acks) >= 2)
            assert [ack["origin"]["trial_number"] for ack in acks] == [1, 3]

            await post("actuation/stop")
            stopped = await status()
            assert not stopped["active"] and not stopped["engine_linked"]
            assert stopped["connected"] is (not managed)
            if managed:
                assert owner.owner_identity() == ("stopped", None, None)
                assert not stopped["process_alive"]
                assert await start() == "started"
                assert owner.owner_identity()[1] != session
            else:
                assert owner.owner_identity() == ("prepared", session, revision)
                assert container.ipc_runtime.peer_connection_token("actuation") == connection
                publish(engine.generation, 4)
                await until(lambda: any(t["trial_number"] == 4 for t in container.results_service.snapshot()["trials"]))
                assert await start() == "started"
                assert owner.owner_identity() == ("active", session, revision)
                assert owner.applied_snapshot["CONFIG.operation_id"] == snapshot["CONFIG.operation_id"]
                publish(engine.generation, 5)
                await until(lambda: len(acks) >= 3)
                assert [ack["origin"]["trial_number"] for ack in acks] == [1, 3, 5]
            await post("actuation/stop")
            if not managed:
                await disconnect_node()
                node = None
                await until(lambda: not owner.is_connected)
                await post("actuation/stop")
                assert owner.owner_identity() == ("stopped", None, None)
                assert await start() == "prepared"
                assert owner.owner_identity()[1] != session
    finally:
        server.should_exit = True
        try:
            await server_task
        finally:
            if node is not None and node.returncode is None:
                await disconnect_node()
