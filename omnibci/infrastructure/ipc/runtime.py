"""Server-owned control bus and on-demand peer listeners."""
import asyncio
from concurrent.futures import Future
from contextlib import suppress
from queue import Queue
from threading import RLock, Thread, current_thread

from omnibci.application.command_contracts import command_contract
from omnibci.application.configuration.definitions import MODULE_SETTING_DEFAULTS
from omnibci.application.plugin_management.execution import bindings_fingerprint
from omnibci.infrastructure.ipc.event_bus import EventBus
from omnibci.infrastructure.ipc.peer_configs import ACTUATION, STIMULATION, actuation_peer_config, stimulation_peer_config
from omnibci.infrastructure.ipc.peer_gateway import PeerGateway
from omnibci.infrastructure.ipc.peer_identity import PeerIdentity


class IpcRuntime:
    _TIMEOUT = 10.0

    def __init__(self, *, record_execution=None):
        self._bus = EventBus(Queue())
        self._gateway = PeerGateway(self._bus)
        self._peers = {}
        self._peer_lock = asyncio.Lock()
        self._lifecycle = RLock()
        self._thread = None
        self._loop = None
        self._closing = False
        self._started = Future()
        self._finished = Future()
        self._record_execution = record_execution

    @property
    def is_ready(self):
        return self._loop is not None and not self._closing

    @property
    def shutdown_complete(self):
        return self._finished.done() and self._finished.exception() is None

    @property
    def has_owned_resources(self):
        return self._thread is not None and not self.shutdown_complete

    def start(self):
        with self._lifecycle:
            if self._closing:
                raise RuntimeError("IPC runtime is closing")
            if self._thread is None:
                self._thread = Thread(target=self._run, name="OmniBCI-IPC", daemon=True)
                self._thread.start()
            try:
                self._started.result(timeout=self._TIMEOUT)
            except BaseException as exc:
                try:
                    self.stop()
                except BaseException as cleanup:
                    exc.add_note(f"IPC runtime cleanup also failed: {cleanup}")
                raise

    def _run(self):
        try:
            asyncio.run(self._serve())
        except BaseException as exc:
            if not self._started.done():
                self._started.set_exception(exc)
            if (self._started.done() and not self._started.cancelled()
                    and self._started.exception() is exc):
                # Startup failed before readiness, and _serve()'s finally block
                # released all acquired resources without a cleanup error.
                self._finished.set_result(None)
            else:
                self._finished.set_exception(exc)
        else:
            self._finished.set_result(None)

    async def _serve(self):
        self._exit = asyncio.Event()
        started = False
        try:
            await self._bus.start()
            await self._gateway.start()
            self._loop = asyncio.get_running_loop()
            started = True
            self._started.set_result(None)
            if not self._closing:
                await self._exit.wait()
        except BaseException as exc:
            if not started and not self._started.done():
                self._started.set_exception(exc)
            raise
        finally:
            try:
                async with self._peer_lock:
                    await self._gateway.stop()
            finally:
                self._loop = None
                self._bus.put_event(None)
                await self._bus.stop()

    def stop(self):
        with self._lifecycle:
            if self._thread is None:
                return
            self._closing = True
            loop = self._loop
            if loop is not None:
                with suppress(RuntimeError):
                    loop.call_soon_threadsafe(self._exit.set)
            self._thread.join(self._TIMEOUT)
            if self._thread.is_alive():
                raise TimeoutError("IPC runtime did not stop")
            self._finished.result()

    def subscribe(self, event_type, handler):
        with self._lifecycle:
            if self._thread is None:
                self._bus.register_event_listener(event_type, handler)
            else:
                self._require_loop().call_soon_threadsafe(self._bus.register_event_listener, event_type, handler)

    def _require_loop(self):
        loop = self._loop
        if loop is None or self._closing:
            raise RuntimeError("IPC runtime is not running")
        return loop

    def dispatch(self, event):
        self._require_loop().call_soon_threadsafe(self._bus.put_event, event)

    def submit(self, coroutine):
        try:
            return asyncio.run_coroutine_threadsafe(coroutine, self._require_loop())
        except BaseException:
            coroutine.close()
            raise

    def _wait(self, coroutine):
        if current_thread() is self._thread:
            coroutine.close()
            raise RuntimeError("Cannot wait for a peer transition on its own loop")
        return self.submit(coroutine).result(timeout=self._TIMEOUT)

    async def _ensure(self, role, config, identity_key, peer_identity):
        async with self._peer_lock:
            current = self._peers.get(role)
            if current is not None and current[0] == identity_key and self.is_peer_ready(role):
                return
            if current is not None and current[0] == identity_key and current[2] is not None:
                # Restart the same transport. It retains the original external execution receipt.
                config = current[1]
            await self._gateway.replace_peer(role, config)
            self._peers[role] = identity_key, config, peer_identity

    async def ensure_peer(self, role, config, *, identity_key, peer_identity=None):
        await asyncio.wrap_future(self.submit(self._ensure(role, config, identity_key, peer_identity)))

    def ensure_peer_sync(self, role, config, *, identity_key, peer_identity=None):
        self._wait(self._ensure(role, config, identity_key, peer_identity))

    async def _prepare_peer(self, role, snapshot):
        if role not in (STIMULATION, ACTUATION):
            raise ValueError("Unsupported server peer")
        revision = (snapshot.get("CONFIG.module_revisions") or {}).get(role)
        generation = snapshot.get("CONFIG.generation")
        operation = snapshot.get("CONFIG.operation_id")
        settings = MODULE_SETTING_DEFAULTS[role] | snapshot
        host = settings[f"{role.upper()}.server_ip"]
        port = settings[f"{role.upper()}.server_port"]
        managed = settings[f"{role.upper()}.auto_spawn"]
        identity = None if managed else PeerIdentity(role, revision, snapshot.get("CONFIG.engine_revision"),
            generation, operation, bindings_fingerprint(snapshot["PLUGINS.bindings"], role))
        options = {"identity": identity, "record_execution": self._record_execution}
        watchdog = settings.get("ACTUATION.watchdog_timeout_s") if role == ACTUATION else None
        config = (actuation_peer_config(host, port, watchdog, **options) if role == ACTUATION
                  else stimulation_peer_config(host, port, **options))
        await self._ensure(role, config, (generation, operation, revision, host, port, managed, watchdog), identity)

    async def ensure_peer_ready(self, role, snapshot):
        await asyncio.wrap_future(self.submit(self._prepare_peer(role, snapshot)))

    def ensure_peer_ready_sync(self, role, snapshot):
        self._wait(self._prepare_peer(role, snapshot))

    async def _close(self, role, *, only_unaccepted=False, settled_connection=None):
        async with self._peer_lock:
            current = self._peers.get(role)
            if only_unaccepted and current is not None and not current[1].transport.seal_if_unaccepted():
                return False
            if settled_connection is not None and current and not current[1].transport.seal_if_settled(settled_connection):
                return False
            await self._gateway.replace_peer(role, None)
            self._peers.pop(role, None)
            return True

    async def close_peer(self, role):
        await asyncio.wrap_future(self.submit(self._close(role)))

    def close_peer_sync(self, role):
        self._wait(self._close(role))

    def cancel_unaccepted_peer(self, role):
        return self._wait(self._close(role, only_unaccepted=True))

    def close_settled_peer(self, role, connection):
        return connection is not None and self._wait(self._close(role, settled_connection=connection))

    def is_peer_ready(self, role):
        return self.is_ready and self._gateway.is_peer_ready(role)

    def is_peer_connected(self, role):
        try:
            return self.is_ready and self._gateway.peer(role).is_connected
        except KeyError:
            return False

    def is_current_peer_channel(self, role, channel_id):
        try:
            return self._gateway.peer(role).channel_id == channel_id
        except KeyError:
            return False

    def peer_connection_token(self, role):
        peer = self._peers.get(role)
        return (peer[1].transport.connection_token or None) if peer and self.is_peer_connected(role) else None

    def forward_to_peer(self, role, event, session, connection, eligible):
        def forward():
            peer = self._peers.get(role)
            if (peer and peer[0][0] == session and connection is not None
                    and self.peer_connection_token(role) == connection and eligible()):
                self._gateway.peer(role).offer(event, eligible)
        self._require_loop().call_soon_threadsafe(forward)

    def node_operation_id(self, role):
        peer = self._peers.get(role)
        if peer and peer[2] and self.is_peer_ready(role):
            return peer[2].operation_id
        return None

    def execution_receipt(self, role):
        peer = self._peers.get(role)
        return peer[1].transport.execution_receipt if peer and peer[2] else None

    def is_command_ready(self, event_type):
        # A terminal receipt can be queued when an output closes its socket.
        return self.is_peer_ready(command_contract(event_type).owner)
