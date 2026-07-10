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
        if self._process_manager is not None:
            self._process_manager.start(paradigm=cmd.paradigm)
        self._put_event(EventType.StartStimulation, data=cmd.paradigm)
        self._active = True
        logger.info("StartStimulation event dispatched (paradigm=%s)", cmd.paradigm)

    def handle_stop_stimulation(self, cmd: StopStimulationCommand) -> None:
        self._put_event(EventType.StopStimulation)
        if self._process_manager is not None:
            self._process_manager.stop()
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
