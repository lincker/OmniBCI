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
            self._process_manager.start()
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
        self._put_event(EventType.StartStimTrial, data=cmd.target_index)
        logger.info("StartStimTrial event dispatched (target=%s)", cmd.target_index)

    @property
    def is_active(self) -> bool:
        return self._active

    def _put_event(self, event_type: EventType, data=None) -> None:
        self._engine.dispatch(Event(event_type, data))
