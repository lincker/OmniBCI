"""
StimulationAppService — Application Service.

Manages stimulation paradigm lifecycle through domain events.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from omnibci.shared.events import EventType, Event
from omnibci.application.commands.stimulation_commands import (
    StartStimulationCommand,
    StopStimulationCommand,
    StartStimTrialCommand,
)
from omnibci.application.ports.engine_port import EnginePort

if TYPE_CHECKING:
    from omnibci.infrastructure.stimulation.process_manager import StimulationProcessManager

logger = logging.getLogger(__name__)


class StimulationAppService:
    """Application service for the Stimulation bounded context.

    *process_manager* is optional so that existing unit tests that construct
    this service without it continue to work unchanged.  When provided,
    ``start`` spawns (or skips, depending on *auto_spawn*) the Stimulation
    subprocess before dispatching the domain event.
    """

    def __init__(
        self,
        engine: EnginePort,
        process_manager: "StimulationProcessManager | None" = None,
    ) -> None:
        self._engine          = engine
        self._process_manager = process_manager
        self._active          = False

    def handle_start_stimulation(self, cmd: StartStimulationCommand) -> None:
        process_ready = False
        self._active = False
        try:
            if self._process_manager is not None:
                # Managed mode blocks here until IPC + renderer/paradigm setup
                # has reported READY.  External mode remains an immediate no-op.
                self._process_manager.start(paradigm=cmd.paradigm)
                process_ready = True
            self._put_event(EventType.StartStimulation, data=cmd.paradigm)
        except BaseException:
            # Process spawn and engine dispatch form one application-level
            # transaction.  If the engine rejects the command, do not leave an
            # orphan renderer behind.  Cleanup failure is secondary: preserve
            # the dispatch exception that explains why start failed.
            if self._process_manager is not None and process_ready:
                try:
                    self._process_manager.stop()
                except BaseException:
                    logger.exception(
                        "Failed to roll back Stimulation process after dispatch error"
                    )
            self._active = False
            raise
        self._active = True
        logger.info("StartStimulation event dispatched (paradigm=%s)", cmd.paradigm)

    def handle_stop_stimulation(self, cmd: StopStimulationCommand) -> None:
        try:
            self._put_event(EventType.StopStimulation)
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
                self._active = False
            raise
        else:
            try:
                if self._process_manager is not None:
                    self._process_manager.stop()
            finally:
                self._active = False
        logger.info("StopStimulation event dispatched")

    def handle_start_trial(self, cmd: StartStimTrialCommand) -> None:
        # JSON-friendly payload: it crosses the engine→stimulation socket, where
        # body_to_event reconstructs only plain JSON (no dataclass), so the
        # stimulation side reads this dict directly.
        payload = {"n_trials": cmd.n_trials, "trial_params": cmd.trial_params}
        self._put_event(EventType.StartStimTrial, data=payload)
        logger.info("StartStimTrial event dispatched (n_trials=%s)", cmd.n_trials)

    @property
    def is_active(self) -> bool:
        return self._active

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
