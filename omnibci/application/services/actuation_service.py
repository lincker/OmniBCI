"""Own the Effector session, controller activation and confirmed device settlement."""
import asyncio
import logging

from omnibci.application.commands.actuation_commands import intent_from_trial
from omnibci.application.configuration.errors import ConfigProfileConflictError, ProfileRevisionConflict
from omnibci.application.ports.ipc_port import ACTUATION_PEER
from omnibci.application.ports.output_process import ActuationProcess
from omnibci.application.services.output_session import OutputSession
from omnibci.shared.events import Event, EventType

logger = logging.getLogger(__name__)


class ActuationAppService(OutputSession[ActuationProcess]):
    role = ACTUATION_PEER
    _enabled_connection = None
    _activation = None
    _settled_connection = None

    @property
    def current_effector(self):
        return self._process_manager.current_effector if self._process_manager else None

    def _accepts_intents(self):
        with self._state_lock:
            if not self._enabled or self._snapshot is None:
                return False
            managed = self._snapshot.get(self.auto_spawn_key, True)
            enabled_connection = self._enabled_connection
        return bool(self.is_connected and (managed or self._host.peer_connection_token(self.role) == enabled_connection))

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
        return self._accepts_intents()

    def status(self, engine_ready):
        # Read process state outside _state_lock so IPC loop eligibility checks
        # (_accepts_intents) never block on _process_manager._lifecycle_lock.
        process_alive = self.process_alive
        effector = self.current_effector
        with self._state_lock:
            snapshot = self._snapshot
            enabled = self._enabled
        managed = snapshot is not None and snapshot.get(self.auto_spawn_key, True)
        if managed and enabled and not process_alive:
            self._observe_managed_exit(process_alive=False)
        connected = self.is_connected
        active = bool(enabled and process_alive) if managed else self._accepts_intents()
        return {"active": active, "connected": connected,
                "engine_linked": bool(engine_ready and active and self._can_forward()),
                "process_alive": process_alive, "effector": effector}

    def _can_forward(self):
        try:
            with self._admission.command(self.role):
                return self._accepts_intents()
        except ProfileRevisionConflict:
            return False

    def on_trial_completed(self, event, engine_current):
        try:
            with self._admission.command(self.role):
                if not self._accepts_intents() or not engine_current():
                    return
                with self._state_lock:
                    session = self._snapshot["CONFIG.generation"]
                    activation = self._activation
                connection = self._host.peer_connection_token(self.role)
                intent = intent_from_trial(event.data)

                def eligible():
                    return self._activation is activation and engine_current() and self._can_forward()

                self._host.forward_to_peer(self.role, Event(EventType.ActuationCommand, intent),
                                           session, connection, eligible)
        except ProfileRevisionConflict:
            return
        except (ValueError, RuntimeError) as exc:
            logger.warning("Actuation trial forwarding failed: %s", exc)

    async def start(self, selected):
        return await asyncio.to_thread(self._start, selected)

    def _start(self, selected):
        with self._admission.transition(self.role):
            snapshot = self._prepare(selected)
            if self.is_active:
                return "started"
            if snapshot.get(self.auto_spawn_key, True):
                try:
                    if self._process_manager is None:
                        raise RuntimeError("No managed Actuation process is available")
                    self._process_manager.start(settings_snapshot=snapshot)
                except BaseException as exc:
                    try:
                        self._release_session()
                    except BaseException as cleanup:
                        raise RuntimeError(f"Actuation startup failed: {exc}. Cleanup failed: {cleanup}") from exc
                    raise
            else:
                connection = self._host.peer_connection_token(self.role)
                if not connection:
                    return "prepared"
                self._stop_confirmed = False
                self._settled_connection = None
                try:
                    self._control(EventType.ResumeActuation, snapshot, "resumed")
                    if self._host.peer_connection_token(self.role) != connection:
                        raise RuntimeError("Actuation connection changed during activation")
                except Exception as exc:
                    self._admission.quarantine(str(exc), self.role)
                    raise
                self._enabled_connection = connection
            with self._state_lock:
                self._enabled = True
                self._activation = object()
            return "started"

    def _control(self, command, snapshot, *flags):
        result = self._command_results.request(command)
        data = result.data
        if (not isinstance(data, dict) or data.get("generation") != snapshot["CONFIG.generation"]
                or data.get("engine_revision") != snapshot["CONFIG.engine_revision"]
                or data.get("module_revision") != snapshot["CONFIG.module_revisions"][self.role]
                or any(data.get(flag) is not True for flag in flags)):
            raise RuntimeError("Actuation did not confirm the requested boundary")

    async def stop(self):
        await asyncio.to_thread(self._stop, release=False)

    async def release(self):
        await asyncio.to_thread(self._release_session)

    def _clear(self):
        with self._state_lock:
            self._activation = self._enabled_connection = self._settled_connection = None
            super()._clear()

    def _release_session(self):
        self._stop(release=True)

    def _stop(self, *, release):
        with self._admission.stop_transition_sync(self.role):
            snapshot = self.applied_snapshot
            if snapshot is None:
                self._clear()
                return
            managed = snapshot.get(self.auto_spawn_key, True)
            if release and not managed and self.is_connected:
                raise ConfigProfileConflictError("Disconnect the settled external Actuation node before replacement")
            with self._state_lock:
                self._enabled = False
                self._enabled_connection = None
                self._activation = None
            try:
                if managed:
                    settle_error = None
                    try:
                        if self.process_alive and self.is_connected:
                            self._settle(snapshot)
                    except BaseException as exc:
                        settle_error = exc
                    try:
                        if self._process_manager:
                            self._process_manager.stop()
                    except BaseException as stop_exc:
                        if settle_error is not None:
                            settle_error.add_note(f"Actuation process stop also failed: {stop_exc}")
                            raise settle_error
                        raise
                    if settle_error is not None:
                        raise settle_error
                    self._host.close_peer_sync(self.role)
                elif self._keep_external_online(snapshot, release):
                    self._admission.clear_quarantine(self.role)
                    return
            except BaseException as exc:
                self._admission.quarantine(str(exc), self.role)
                raise
            self._clear()

    def _settle(self, snapshot):
        self._control(EventType.SettleActuation, snapshot, "paused", "execution_complete", "outputs_settled")

    def _keep_external_online(self, snapshot, release):
        if self._host.cancel_unaccepted_peer(self.role):
            return False
        connection = self._host.peer_connection_token(self.role)
        if connection is None:
            if self._stop_confirmed and self._host.close_settled_peer(self.role, self._settled_connection):
                return False
            raise RuntimeError("Reconnect the external Actuation node to confirm settlement")
        if release:
            raise ConfigProfileConflictError("Disconnect the external Actuation node before replacement")
        if not self._stop_confirmed or self._settled_connection != connection:
            self._stop_confirmed = False
            self._settle(snapshot)
            if self._host.peer_connection_token(self.role) != connection:
                raise RuntimeError("Actuation connection changed during settlement")
            self._settled_connection = connection
            self._stop_confirmed = True
        return True
