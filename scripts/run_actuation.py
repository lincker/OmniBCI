#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Actuation process entry point.

Run from the project root with the venv active (typically on the device
machine in a multi-machine deployment, with ACTUATION.auto_spawn = false):

    python scripts/run_actuation.py

The process:
  1. Connects to the NeuroEngine actuation socket (settings: ACTUATION.server_*)
  2. Loads the pinned Effector from the accepted activation and calls setup()
  3. Executes ActuationCommand events via Effector.play(command),
     acking each (ActuationAck) or reporting failure (ActuationFault)
  4. Publishes a heartbeat (ActuationState) so the engine's link watchdog
     stays quiet

Press Ctrl-C to exit (the effector is torn down on the way out).
"""
import logging
import sys
import os

# Ensure the project root is on sys.path when run directly
_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _root not in sys.path:
    sys.path.insert(0, _root)

from omnibci.infrastructure.actuation.application import Actuation
from omnibci.infrastructure.plugins.accepted_configuration import accepted_runtime_settings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("run_actuation")


def main() -> None:
    logger.info("Starting Actuation process...")
    try:
        snapshot = accepted_runtime_settings(active_kinds={"effector"})
        from omnibci.conf import settings
        settings.use_snapshot(snapshot)
        Actuation(settings_snapshot=snapshot).run()
    except KeyboardInterrupt:
        logger.info("Actuation process interrupted — exiting")


if __name__ == "__main__":
    main()
