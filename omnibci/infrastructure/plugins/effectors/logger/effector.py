"""
LogEffector — hardware-free Effector for end-to-end smoke tests.

With ``ACTUATION.effector = 'logger'`` the full closed loop
(TrialCompleted → ActuationCommand → play → ActuationAck) can be exercised
without any device attached: each command is simply logged.
"""
import logging
from typing import Any, Mapping

from omnibci.domain.ports.effector import Effector
from omnibci.sdk import PluginContext, PluginParameters

logger = logging.getLogger(__name__)


class LogEffector(Effector):
    plugin_type = "logger"

    def __init__(self, *, parameters: PluginParameters | None = None,
                 context: PluginContext | None = None):
        self.config_revision = context.config_revision if context is not None else None

    def setup(self):
        logger.info("[LogEffector] setup — ready (no hardware)")

    def teardown(self):
        logger.info("[LogEffector] teardown")

    def play(self, command: Mapping[str, Any]) -> None:
        logger.info("[LogEffector] play: target_id=%s event_id=%s confidence=%s",
                    command.get("target_id"), command.get("event_id"),
                    command.get("confidence"))
