#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Actuation process entry point.

Run from the project root with the venv active (typically on the device
machine in a multi-machine deployment, with ACTUATION.auto_spawn = false):

    python scripts/run_actuation.py

The process:
  1. Fetches its configuration from --server (or OMNIBCI_SERVER_URL)
  2. Loads its pinned Effector and registers the Actuation session and operation
     before accepting an enable request
  3. Executes ActuationCommand events via Effector.execute(command),
     acking each (ActuationAck) or reporting failure (ActuationFault)
  4. Publishes a heartbeat (ActuationState) so the server's link watchdog
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
from omnibci.infrastructure.node_client import node_server_argument, run_configured_node

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("run_actuation")


def main(argv=None) -> None:
    server = node_server_argument("actuation", argv)
    logger.info("Starting Actuation process...")
    try:
        run_configured_node("actuation", Actuation, server_url=server)
    except KeyboardInterrupt:
        logger.info("Actuation process interrupted — exiting")


if __name__ == "__main__":
    main()
