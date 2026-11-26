#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Stimulation process entry point.

Run from the project root with the venv active:

    python scripts/run_stimulation.py

The process:
  1. Fetches its configuration from --server (or OMNIBCI_SERVER_URL)
  2. Initializes its pinned Trigger, Paradigm, and Renderer
  3. Authenticates the prepared Stimulation session and its execution receipt
  4. Forwards trial/result commands to the running paradigm
  5. On disconnection, stops rendering and clears the trigger before re-fetching
  6. On StopStimulation, tears down the GL window and exits

Press Ctrl-C to exit.
"""
import logging
import sys
import os

# Ensure the project root is on sys.path when run directly
_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _root not in sys.path:
    sys.path.insert(0, _root)

from omnibci.infrastructure.stimulation.application import Stimulation
from omnibci.infrastructure.node_client import node_server_argument, run_configured_node

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("run_stimulation")


def main(argv=None) -> None:
    server = node_server_argument("stimulation", argv)
    logger.info("Starting Stimulation process...")
    try:
        run_configured_node("stimulation", Stimulation, server_url=server)
    except KeyboardInterrupt:
        logger.info("Stimulation process stopped by user.")


if __name__ == "__main__":
    main()
