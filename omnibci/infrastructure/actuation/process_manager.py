"""ActuationProcessManager — optional subprocess launcher for single-machine deployments.

Multi-machine deployment
------------------------
Set ``ACTUATION.auto_spawn = false`` in settings.toml and start the actuation
process manually on the device machine:

    python scripts/run_actuation.py

Single-machine deployment
--------------------------
Leave ``ACTUATION.auto_spawn = true``. With ``ACTUATION.enabled = true`` the
backend spawns the Actuation subprocess when the engine starts and terminates
it when the engine stops (see NeuroEngineService).

In both modes the IPC socket architecture (Actuation → NeuroEngine) is
unchanged — this class only manages the OS-level process lifecycle. Mirrors
StimulationProcessManager.
"""
from __future__ import annotations

import logging
import multiprocessing as mp

logger = logging.getLogger(__name__)


def _run_actuation(effector: str | None = None) -> None:
    """Actuation subprocess entry point.  Runs in the spawned child process.

    *effector* — when provided, overrides ``ACTUATION.effector`` in the
    subprocess's in-memory settings before the Actuator loads plugins.
    The override is subprocess-local: the settings file on disk is unchanged.
    """
    if effector:
        from omnibci.conf import settings
        settings.set("ACTUATION.effector", effector)
    from omnibci.infrastructure.actuation.application import Actuation
    Actuation().run()


class ActuationProcessManager:
    """Manages the Actuation OS process.

    When *auto_spawn* is False all public methods are no-ops, so the same
    engine-lifecycle code path works for both deployment modes (and for
    ``ACTUATION.enabled = false``, where the composition root passes
    auto_spawn=False).
    """

    def __init__(self, auto_spawn: bool = True) -> None:
        self._auto_spawn = auto_spawn
        self._process: mp.Process | None = None
        # Effector the *running* subprocess was spawned with (None = config default).
        self._effector: str | None = None

    # ── Public API ─────────────────────────────────────────────────────────

    def start(self, effector: str | None = None) -> None:
        """Spawn the Actuation subprocess (no-op if auto_spawn=False or already running).

        *effector* is forwarded to the subprocess and overrides the
        ``ACTUATION.effector`` config value in-process only.
        """
        if not self._auto_spawn:
            logger.debug("auto_spawn=False — Actuation process must be started manually")
            return
        if self.is_alive:
            if effector == self._effector:
                logger.debug(
                    "Actuation process already running requested effector (pid=%s, effector=%s)",
                    self._process.pid, effector or "<config>",
                )
                return
            logger.info(
                "Actuation effector change %s → %s; restarting subprocess",
                self._effector or "<config>", effector or "<config>",
            )
            self.stop()
        self._process = mp.Process(
            target=_run_actuation,
            args=(effector,),
            name="ActuationProcess",
            daemon=True,
        )
        self._process.start()
        self._effector = effector
        logger.info("Actuation subprocess spawned (pid=%s, effector=%s)",
                    self._process.pid, effector or "<config>")

    def stop(self) -> None:
        """Terminate the Actuation subprocess if it is running."""
        if not self._auto_spawn:
            return
        if self._process is not None and self._process.is_alive():
            self._process.terminate()
            self._process.join(timeout=3.0)
            logger.info("Actuation subprocess stopped (pid=%s)", self._process.pid)
        self._process = None
        self._effector = None

    @property
    def is_alive(self) -> bool:
        return self._process is not None and self._process.is_alive()

    @property
    def current_effector(self) -> str | None:
        """Effector the running subprocess was spawned with (None if stopped or config default)."""
        return self._effector if self.is_alive else None
