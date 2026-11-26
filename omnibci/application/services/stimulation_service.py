"""
StimulationAppService — Application Service.

Manages stimulation paradigm lifecycle through domain events.
"""
from threading import Lock

from omnibci.application.commands.stimulation_commands import StartStimulationCommand, StopStimulationCommand, StartStimTrialCommand
from omnibci.application.ports.ipc_port import STIMULATION_PEER
from omnibci.application.ports.output_process import StimulationProcess
from omnibci.application.services.command_result_service import CommandDeliveryError, CommandTimeoutError
from omnibci.application.services.output_session import OutputSession
from omnibci.shared.events import Event, EventType


class StimulationAppService(OutputSession[StimulationProcess]):
    """Own one prepared or active stimulation session until cleanup is confirmed."""
    role = STIMULATION_PEER

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._commands_lock = Lock()

    @property
    def current_paradigm(self):
        return self._process_manager.current_paradigm if self._process_manager else None

    def status(self, engine_ready):
        # Read process state outside _state_lock so slow child join() calls
        # cannot stall callers that only inspect in-memory session state.
        process_alive = self.process_alive
        paradigm = self.current_paradigm
        with self._state_lock:
            snapshot = self._snapshot
            enabled = self._enabled
        managed = snapshot is not None and snapshot.get(self.auto_spawn_key, True)
        if managed and enabled and not process_alive:
            self._observe_managed_exit(process_alive=False)
        connected = self.is_connected
        active = bool(enabled and (process_alive if managed else connected))
        return {"active": active, "connected": connected, "process_alive": process_alive,
                "engine_linked": bool(engine_ready and active and connected and self._admitted_for_feedback()),
                "paradigm": paradigm}

    def handle_start_stimulation(self, cmd: StartStimulationCommand, *, settings_snapshot: dict):
        with self._admission.transition(STIMULATION_PEER):
            snapshot = self._prepare(settings_snapshot)
            if self.is_active:
                return "started"
            if snapshot.get("STIMULATION.auto_spawn", True):
                try:
                    if self._process_manager is None:
                        raise RuntimeError("No managed Stimulation process is configured")
                    self._process_manager.start(settings_snapshot=snapshot)
                except BaseException as exc:
                    try:
                        self.stop()
                    except BaseException as cleanup:
                        exc.add_note(f"Stimulation cleanup also failed: {cleanup}")
                    raise
            elif not self.is_connected:
                return "prepared"
            with self._state_lock:
                self._enabled = True
            return "started"

    def _release_session(self):
        self.stop()

    def stop(self):
        with self._admission.stop_transition_sync(STIMULATION_PEER):
            snapshot = self.applied_snapshot
            if snapshot is None:
                self._clear()
                return
            with self._state_lock:
                self._enabled = False
            try:
                if snapshot.get("STIMULATION.auto_spawn", True):
                    dispatch_error = None
                    try:
                        if self.is_connected:
                            self._host.dispatch(Event(EventType.StopStimulation))
                    except BaseException as exc:
                        dispatch_error = exc
                    try:
                        if self._process_manager is not None:
                            self._process_manager.stop()
                    except BaseException as stop_exc:
                        if dispatch_error is not None:
                            dispatch_error.add_note(f"Stimulation process stop also failed: {stop_exc}")
                            raise dispatch_error
                        raise
                    if dispatch_error is not None:
                        raise dispatch_error
                elif not self._stop_confirmed:
                    if self._host.cancel_unaccepted_peer(STIMULATION_PEER):
                        self._clear()
                        return
                    if not self._host.is_peer_ready(STIMULATION_PEER):
                        self._host.ensure_peer_ready_sync(STIMULATION_PEER, snapshot)
                    self._command_results.request(EventType.StopStimulation)
                    self._stop_confirmed = True
                self._host.close_peer_sync(STIMULATION_PEER)
            except BaseException as exc:
                self._admission.quarantine(str(exc), STIMULATION_PEER)
                raise
            self._clear()

    def handle_stop_stimulation(self, cmd: StopStimulationCommand):
        self.stop()

    def request(self, event_type, payload=None):
        with self._commands_lock, self._admission.command(STIMULATION_PEER):
            if not self.is_active or not self.is_connected:
                raise RuntimeError("Start stimulation before starting a trial" if event_type == EventType.StartStimTrial
                                   else "Start stimulation before sending a command")
            try:
                return self._command_results.request(event_type, payload)
            except (CommandTimeoutError, CommandDeliveryError) as exc:
                if isinstance(exc, CommandTimeoutError) or exc.execution_unknown:
                    self._admission.quarantine(str(exc), STIMULATION_PEER)
                raise

    def handle_start_trial(self, cmd: StartStimTrialCommand):
        self.request(EventType.StartStimTrial, {"n_trials": cmd.n_trials, "trial_params": cmd.trial_params})

    def _admitted_for_feedback(self):
        try:
            with self._admission.command(STIMULATION_PEER):
                return True
        except RuntimeError:
            return False

    def _forward_feedback(self, event=None):
        try:
            with self._admission.command(STIMULATION_PEER):
                if not self.is_active or not self.is_connected:
                    return False
                if event is not None:
                    self._host.dispatch(event)
                return True
        except RuntimeError:
            return False

    def on_trial_completed(self, event):
        self._forward_feedback(event)
