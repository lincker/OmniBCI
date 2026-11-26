"""Session identity, external preparation and retained ownership shared by output services."""
from abc import ABC, abstractmethod
from copy import deepcopy
from threading import RLock
from typing import Generic, TypeVar
from uuid import uuid4

from omnibci.application.configuration.definitions import module_runtime_settings
from omnibci.application.configuration.errors import ConfigProfileConflictError
from omnibci.application.ports.ipc_port import PeerHostPort
from omnibci.application.ports.output_process import OutputProcess
from omnibci.application.runtime_admission import RuntimeAdmission
from omnibci.application.services.command_result_service import CommandExecutor


Process = TypeVar("Process", bound=OutputProcess)


class OutputSession(ABC, Generic[Process]):
    role: str

    def __init__(self, ipc_host: PeerHostPort, process_manager: Process | None = None,
                 command_results: CommandExecutor | None = None, admission=None, *,
                 plugin_preparer=None, snapshot_provider=None):
        self.auto_spawn_key = f"{self.role.upper()}.auto_spawn"
        self._host = ipc_host
        self._process_manager = process_manager
        self._command_results = command_results
        self._admission = admission or RuntimeAdmission()
        self._plugin_preparer = plugin_preparer
        self._snapshot_provider = snapshot_provider
        self._state_lock = RLock()
        self._snapshot = None
        self._enabled = False
        self._stop_confirmed = False

    @property
    def applied_snapshot(self):
        with self._state_lock:
            return deepcopy(self._snapshot)

    def _observe_managed_exit(self, *, process_alive: bool) -> bool:
        with self._state_lock:
            exited = (self._snapshot is not None
                      and self._snapshot.get(self.auto_spawn_key, True)
                      and self._enabled and not process_alive)
        if exited and not self._admission.requires_recovery(self.role):
            self._admission.quarantine(
                f"{self.role.title()} subprocess exited unexpectedly; stop {self.role} to confirm cleanup",
                self.role,
            )
        return exited

    def owner_identity(self):
        with self._state_lock:
            snapshot = self._snapshot
            enabled = self._enabled
        if snapshot is None:
            return "stopped", None, None
        if snapshot.get(self.auto_spawn_key, True):
            alive = self.process_alive
            if enabled and alive:
                return "active", snapshot["CONFIG.generation"], snapshot["CONFIG.module_revisions"][self.role]
            if enabled and not alive:
                self._observe_managed_exit(process_alive=False)
            return "stopped", None, None
        return ("active" if self.is_active else "prepared", snapshot["CONFIG.generation"],
                snapshot["CONFIG.module_revisions"][self.role])

    @property
    def runtime_package_snapshots(self):
        snapshot = self.applied_snapshot
        return (snapshot,) if snapshot is not None else ()

    @property
    def is_connected(self):
        return self._host.is_peer_connected(self.role)

    @property
    def is_active(self):
        with self._state_lock:
            if not self._enabled or self._snapshot is None:
                return False
            managed = self._snapshot.get(self.auto_spawn_key, True)
        if managed:
            alive = self.process_alive
            if not alive:
                self._observe_managed_exit(process_alive=False)
            return alive
        return self.is_connected

    @property
    def process_alive(self):
        return self._process_manager is not None and self._process_manager.is_alive

    def _prepare(self, selected):
        revision = (selected.get("CONFIG.module_revisions") or {}).get(self.role)
        if not revision:
            raise ConfigProfileConflictError(f"Select {self.role.title()} configuration before preparation")
        current = self.applied_snapshot
        if current is not None:
            same_revision = current["CONFIG.module_revisions"][self.role] == revision
            managed_exited = current.get(self.auto_spawn_key, True) and self._enabled and not self.process_alive
            if same_revision and not managed_exited:
                self._host.ensure_peer_ready_sync(self.role, current)
                return current
            self._release_session()
        snapshot = module_runtime_settings(selected, self.role)
        snapshot.update({"CONFIG.generation": str(uuid4()), "CONFIG.operation_id": uuid4().hex,
                         "CONFIG.engine_revision": None})
        if snapshot.get(self.auto_spawn_key, True) and self._plugin_preparer is not None:
            snapshot = self._plugin_preparer(snapshot, (self.role,))
        with self._state_lock:
            self._snapshot = snapshot
        try:
            self._host.ensure_peer_ready_sync(self.role, snapshot)
        except BaseException as exc:
            try:
                self._release_session()
            except BaseException as cleanup:
                exc.add_note(f"{self.role.title()} cleanup also failed: {cleanup}")
            raise
        return snapshot

    def prepare_external(self, snapshot=None):
        with self._admission.transition(self.role, recovery=True):
            if self._admission.requires_recovery(self.role):
                snapshot = self.applied_snapshot
                if snapshot is None:
                    raise ConfigProfileConflictError(f"No retained external {self.role.title()} session to recover")
            elif snapshot is None:
                if self._snapshot_provider is None:
                    raise ConfigProfileConflictError(f"Select an external {self.role.title()} configuration before preparation")
                snapshot = self._snapshot_provider()
            if snapshot.get(self.auto_spawn_key, True):
                raise ConfigProfileConflictError(f"{self.role.title()} is managed by the server")
            self._prepare(snapshot)

    def node_configuration_snapshot(self, selected_snapshot):
        with self._admission.command(self.role, recovery=True):
            snapshot = self.applied_snapshot
            if snapshot is None:
                if self._admission.requires_recovery(self.role):
                    raise ConfigProfileConflictError(f"Stop {self.role.title()} before preparing an external session")
                return "selected", selected_snapshot()
            if (not self._host.is_peer_ready(self.role) or
                    not snapshot.get(self.auto_spawn_key, True) and
                    self._host.node_operation_id(self.role) != snapshot["CONFIG.operation_id"]):
                raise ConfigProfileConflictError(f"Prepare the retained {self.role.title()} session before reconnecting")
            return "running", snapshot

    @abstractmethod
    def _release_session(self):
        """Retire this session and release its listener before preparing another."""

    def _clear(self):
        with self._state_lock:
            self._snapshot = None
            self._enabled = False
            self._stop_confirmed = False
        self._admission.clear_quarantine(self.role)

    def on_peer_delivery_fault(self, event):
        fault = event.data
        if (isinstance(fault, dict) and fault.get("peer") == self.role
                and isinstance(fault.get("reason"), str)
                and self._host.is_current_peer_channel(self.role, fault.get("channel_id"))):
            self._admission.quarantine(fault["reason"], self.role)
            self._command_results.on_peer_delivery_fault(event)

    @property
    def execution_receipt(self):
        snapshot = self.applied_snapshot
        if snapshot is None:
            return None
        if snapshot.get(self.auto_spawn_key, True):
            return self._process_manager.execution_receipt if self._process_manager else None
        return self._host.execution_receipt(self.role)
