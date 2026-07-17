#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Stimulation process entry point.

Run from the project root with the venv active:

    python scripts/run_stimulation.py

The process:
  1. Connects to the NeuroEngine IPC socket (settings: NEUROENGINE.message_server_*)
  2. Waits for StartStimulation events from the engine
  3. On start: creates Renderer → loads paradigm → launches RenderLoop (GL window)
  4. Forwards trial/result commands to the running paradigm
  5. On StopStimulation: tears down the GL window and returns to idle

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

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("run_stimulation")


def main() -> None:
    logger.info("Starting Stimulation process...")
    try:
        Stimulation().run()
    except KeyboardInterrupt:
        logger.info("Stimulation process stopped by user.")


if __name__ == "__main__":
    main()
