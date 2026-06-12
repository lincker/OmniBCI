"""
StimulationAppService — Application Service.

Manages stimulation paradigm lifecycle through domain events.
"""

import logging

from omnibci.shared.events import EventType, Event
from omnibci.application.commands.stimulation_commands import (
    StartStimulationCommand,
    StopStimulationCommand,
    StartStimTrialCommand,
)
from omnibci.application.ports.engine_port import EnginePort

logger = logging.getLogger(__name__)


class StimulationAppService:
    """Application service for the Stimulation bounded context."""

    def __init__(self, engine: EnginePort) -> None:
        self._engine = engine
        self._active = False

    def handle_start_stimulation(self, cmd: StartStimulationCommand) -> None:
        self._put_event(EventType.StartStimulation, data=cmd.paradigm)
        self._active = True
        logger.info("StartStimulation event dispatched (paradigm=%s)", cmd.paradigm)

    def handle_stop_stimulation(self, cmd: StopStimulationCommand) -> None:
        self._put_event(EventType.StopStimulation)
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
