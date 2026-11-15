"""
StimulationAppService — Application Service.

Manages stimulation paradigm lifecycle through domain events.
"""
from __future__ import annotations

import logging
from contextlib import nullcontext
from omnibci.application.runtime_admission import admitted
from typing import TYPE_CHECKING

from omnibci.shared.events import EventType, Event
from omnibci.application.commands.stimulation_commands import (
    StartStimulationCommand,
    StopStimulationCommand,
    StartStimTrialCommand,
)
from omnibci.application.ports.engine_port import EnginePort
from omnibci.application.ports.engine_port import STIMULATION_PEER
from omnibci.application.services.command_result_service import CommandDeliveryError, CommandTimeoutError

if TYPE_CHECKING:
    from omnibci.application.services.command_result_service import CommandResultBroker
    from omnibci.infrastructure.stimulation.process_manager import StimulationProcessManager

logger = logging.getLogger(__name__)


class StimulationAppService:
    """Own stimulation startup readiness and explicit output shutdown."""

    def __init__(
        self,
        engine: EnginePort,
        process_manager: "StimulationProcessManager | None" = None,
        command_results: "CommandResultBroker | None" = None,
        admission=None,
    ) -> None:
        self._admission = admission
        self._engine          = engine
        self._process_manager = process_manager
        self._command_results = command_results
        self._active_identity = None
        self._stop_managed = None

    def handle_start_stimulation(self, cmd: StartStimulationCommand, *, settings_snapshot: dict) -> None:
        with self._admission.transition("stimulation") if self._admission is not None else nullcontext():
            self._start_stimulation(settings_snapshot=settings_snapshot)

    def _start_stimulation(self, *, settings_snapshot: dict) -> None:
        if not self._engine.is_engine_ready:
            raise RuntimeError("Start the engine before starting stimulation")
        process_ready = False
        self._active_identity = None
        snapshot = settings_snapshot
        try:
            self._engine.ensure_output_ready_sync("stimulation", snapshot)
            if self._process_manager is not None:
                # Managed mode blocks here until IPC + renderer/paradigm setup
                # has reported READY.  External mode remains an immediate no-op.
                self._process_manager.start(settings_snapshot=snapshot)
                process_ready = True
                if not self._process_manager.auto_spawn:
                    if not self._engine.is_peer_connected(STIMULATION_PEER):
                        raise RuntimeError(
                            "External stimulation process is not connected"
                        )
        except BaseException:
            # Keep a failed readiness check from leaving a locally owned renderer.
            if self._process_manager is not None and process_ready:
                try:
                    self._process_manager.stop()
                except BaseException:
                    logger.exception(
                        "Failed to roll back Stimulation process after startup error"
                    )
            self._active_identity = None
            raise
        self._active_identity = self._identity(snapshot)
        logger.info("Stimulation startup confirmed")

    def handle_stop_stimulation(self, cmd: StopStimulationCommand) -> None:
        with self._admission.stop_transition_sync("stimulation") if self._admission is not None else nullcontext():
            if self._stop_managed is None:
                self._stop_managed = self._managed()
            try:
                self._stop_stimulation(cmd)
            except Exception as exc:
                if self._admission is not None:
                    self._admission.quarantine(str(exc), "stimulation")
                raise
            self._stop_managed = None
            if self._admission is not None:
                self._admission.clear_quarantine("stimulation")

    def _stop_stimulation(self, cmd: StopStimulationCommand) -> None:
        if not self._engine.is_engine_ready and self._managed() and self._process_manager is not None:
            self._process_manager.stop()
            self._active_identity = None
            return
        try:
            if (
                self._command_results is not None
                and (
                    self._process_manager is None
                    or not self._managed()
                )
            ):
                # An externally-managed process cannot be joined locally, so
                # require its own correlated receipt before reporting success.
                self._command_results.request(EventType.StopStimulation)
            else:
                self._put_event(EventType.StopStimulation)
        except (CommandDeliveryError, CommandTimeoutError):
            # A stop that was not sent or never confirmed does not release the
            # external renderer. Preserve ownership until a retry is confirmed.
            raise
        except BaseException:
            # The engine may already be down, but the locally-owned renderer
            # still has to stop.  Never replace the dispatch error with a
            # secondary process-cleanup failure.
            try:
                if self._process_manager is not None:
                    self._process_manager.stop()
            except BaseException:
                logger.exception(
                    "Failed to stop Stimulation process after dispatch error"
                )
            finally:
                self._active_identity = None
            raise
        else:
            try:
                if self._process_manager is not None:
                    self._process_manager.stop()
            finally:
                self._active_identity = None
        logger.info("StopStimulation event dispatched")

    @admitted(scopes="stimulation")
    def handle_start_trial(self, cmd: StartStimTrialCommand) -> None:
        if not self.is_active:
            raise RuntimeError("Start stimulation before starting a trial")
        # JSON-friendly payload: it crosses the engine→stimulation socket, where
        # body_to_event reconstructs only plain JSON (no dataclass), so the
        # stimulation side reads this dict directly.
        payload = {"n_trials": cmd.n_trials, "trial_params": cmd.trial_params}
        if self._command_results is not None:
            # The stimulation consumer answers only when the first requested
            # trial has actually entered present_trial(), not when it is queued.
            self._command_results.request(EventType.StartStimTrial, payload)
        else:
            self._put_event(EventType.StartStimTrial, data=payload)
        logger.info("StartStimTrial event dispatched (n_trials=%s)", cmd.n_trials)

    @property
    def is_active(self) -> bool:
        if self._active_identity is None or not self._engine.is_engine_ready:
            return False
        if self._identity(self._engine.applied_snapshot) != self._active_identity:
            return False
        if self._process_manager is not None:
            if self._managed():
                return self._process_manager.is_alive
            return bool(self._engine.is_peer_connected(STIMULATION_PEER))
        return True

    @staticmethod
    def _identity(snapshot):
        snapshot = snapshot if isinstance(snapshot, dict) else {}
        return (snapshot.get("CONFIG.generation"),
                snapshot.get("CONFIG.module_revisions", {}).get("stimulation"))

    def _managed(self) -> bool:
        if self._stop_managed is not None:
            return self._stop_managed
        snapshot = self._engine.applied_snapshot
        if isinstance(snapshot, dict) and "STIMULATION.auto_spawn" in snapshot:
            return snapshot["STIMULATION.auto_spawn"]
        return self._process_manager is not None and self._process_manager.auto_spawn

    @property
    def process_alive(self) -> bool:
        """True when the Stimulation subprocess is running (auto_spawn mode only)."""
        if self._process_manager is None:
            return False
        return self._process_manager.is_alive

    @property
    def current_paradigm(self) -> "str | None":
        """Paradigm the running subprocess was spawned with (auto_spawn mode only)."""
        if self._process_manager is None:
            return None
        return self._process_manager.current_paradigm

    def _put_event(self, event_type: EventType, data=None) -> None:
        self._engine.dispatch(Event(event_type, data))
