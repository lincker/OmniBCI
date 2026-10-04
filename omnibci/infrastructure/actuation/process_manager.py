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
import multiprocessing
from copy import deepcopy
from threading import RLock
from typing import Any

from omnibci.infrastructure.process_lifecycle import (
    ChildProcessState,
    StartupReporter,
    wait_for_startup_report,
)

logger = logging.getLogger(__name__)

# Managed children must inherit the caller's current environment, not a forkserver's.
mp = multiprocessing.get_context("spawn")


def _run_actuation(
    effector: str | None = None,
    stop_event: Any = None,
    startup_connection: Any = None,
    *,
    settings_snapshot: dict | None = None,
) -> None:
    """Actuation subprocess entry point.  Runs in the spawned child process.

    A pinned snapshot is required and installed before runtime imports.
    """
    reporter = (
        StartupReporter(
            startup_connection, component="Actuation",
            config_revision=(settings_snapshot or {}).get("CONFIG.revision"),
            generation=(settings_snapshot or {}).get("CONFIG.generation"),
        )
        if startup_connection is not None
        else None
    )
    try:
        if settings_snapshot is None:
            raise RuntimeError("Actuation requires a pinned settings snapshot")
        from omnibci.conf import settings
        selected = settings_snapshot.get("ACTUATION.effector")
        if effector is not None and effector != selected:
            raise RuntimeError("Apply the effector configuration before starting Actuation")
        settings.use_snapshot(settings_snapshot)
        from omnibci.infrastructure.actuation.application import Actuation
        kwargs = {"stop_event": stop_event, "settings_snapshot": settings_snapshot}
        if reporter is not None:
            kwargs["startup_reporter"] = reporter
        Actuation(**kwargs).run()
    except BaseException as exc:
        if reporter is not None:
            reporter.failed("entrypoint", exc)
        raise
    finally:
        if reporter is not None:
            reporter.close()


class ActuationProcessManager:
    """Manages the Actuation OS process.

    When *auto_spawn* is False all public methods are no-ops, so the same
    engine-lifecycle code path works for both deployment modes (and for
    ``ACTUATION.enabled = false``, where the composition root passes
    auto_spawn=False).
    """

    _GRACEFUL_TIMEOUT_S = 3.0
    _TERMINATE_TIMEOUT_S = 3.0
    _KILL_TIMEOUT_S = 3.0
    _STARTUP_TIMEOUT_S = 10.0

    def __init__(
        self,
        auto_spawn: bool = True,
        *,
        startup_timeout_s: float = _STARTUP_TIMEOUT_S,
    ) -> None:
        if startup_timeout_s <= 0:
            raise ValueError("startup_timeout_s must be greater than zero")
        self._default_auto_spawn = auto_spawn
        self._auto_spawn = auto_spawn
        self._settings_snapshot: dict | None = None
        self._startup_timeout_s = float(startup_timeout_s)
        self._process: mp.Process | None = None
        self._stop_event: Any = None
        self._startup_receiver: Any = None
        # Effector the *running* subprocess was spawned with (None = config default).
        self._effector: str | None = None
        self._state = ChildProcessState.STOPPED
        self._lifecycle_lock = RLock()

    # ── Public API ─────────────────────────────────────────────────────────

    def start(
        self, effector: str | None = None, *, settings_snapshot: dict | None = None,
    ) -> None:
        """Spawn the Actuation subprocess (no-op if auto_spawn=False or already running).

        A snapshot pins every setting and determines the selected effector and
        managed mode. A differing legacy override requires a configuration apply.
        """
        with self._lifecycle_lock:
            self._start_locked(effector, settings_snapshot=settings_snapshot)

    def _start_locked(
        self, effector: str | None = None, *, settings_snapshot: dict | None = None,
    ) -> None:
        snapshot = deepcopy(settings_snapshot)
        if snapshot is not None:
            selected = snapshot.get("ACTUATION.effector")
            if effector is not None and effector != selected:
                raise RuntimeError("Apply the effector configuration before starting Actuation")
            effector = selected
            auto_spawn = bool(snapshot.get("ACTUATION.enabled", self._default_auto_spawn)) and bool(
                snapshot.get("ACTUATION.auto_spawn", self._default_auto_spawn)
            )
        else:
            auto_spawn = self._default_auto_spawn
        if not auto_spawn:
            self._stop_locked()
            self._auto_spawn = False
            logger.debug("auto_spawn=False — Actuation process must be started manually")
            return
        self._auto_spawn = True
        self._refresh_state_locked()
        if self._process is not None:
            process_alive = self._process.is_alive()
            if (
                process_alive
                and self._state is ChildProcessState.READY
                and effector == self._effector
                and snapshot == self._settings_snapshot
            ):
                logger.debug(
                    "Actuation process already running requested effector (pid=%s, effector=%s)",
                    self._process.pid, effector or "<config>",
                )
                return
            if process_alive:
                logger.info(
                    "Actuation effector change %s → %s; restarting subprocess",
                    self._effector or "<config>", effector or "<config>",
                )
            else:
                logger.info(
                    "Reaping exited Actuation subprocess before restart (pid=%s, exitcode=%s)",
                    self._process.pid,
                    self._process.exitcode,
                )
            # stop() only clears the old handle after confirming exit.  If it
            # cannot stop the process it raises and this method must not spawn a
            # second controller for the same device.
            self._stop_locked()

        stop_event = mp.Event()
        startup_receiver, startup_sender = mp.Pipe(duplex=False)
        process_kwargs = (
            {"kwargs": {"settings_snapshot": deepcopy(snapshot)}}
            if snapshot is not None else {}
        )
        process = mp.Process(
            target=_run_actuation,
            args=(effector, stop_event, startup_sender),
            name="ActuationProcess",
            daemon=True,
            **process_kwargs,
        )
        self._state = ChildProcessState.STARTING
        try:
            process.start()
        except BaseException:
            self._state = ChildProcessState.FAILED
            for connection in (startup_receiver, startup_sender):
                try:
                    connection.close()
                except OSError:
                    pass
            close = getattr(process, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    pass
            raise

        self._process = process
        self._stop_event = stop_event
        self._startup_receiver = startup_receiver
        self._effector = effector
        self._settings_snapshot = snapshot
        # The parent must not retain the write end; otherwise a child that exits
        # before sending a report can leave the receive end looking live.
        try:
            startup_sender.close()
        except OSError as exc:
            logger.warning(
                "Failed to close parent copy of Actuation startup sender: %s",
                exc,
            )
        logger.info(
            "Actuation subprocess spawned; waiting for readiness "
            "(pid=%s, effector=%s)",
            process.pid,
            effector or "<config>",
        )
        try:
            expected_identity = ({
                "expected_revision": snapshot.get("CONFIG.revision"),
                "expected_generation": snapshot.get("CONFIG.generation"),
            } if snapshot is not None else {})
            wait_for_startup_report(
                startup_receiver,
                process,
                component="Actuation",
                timeout_s=self._startup_timeout_s,
                **expected_identity,
            )
        except BaseException as startup_exc:
            self._state = ChildProcessState.FAILED
            try:
                self._stop_locked(final_state=ChildProcessState.FAILED)
            except BaseException as cleanup_exc:
                raise startup_exc from cleanup_exc
            raise
        finally:
            self._close_startup_receiver()

        self._state = ChildProcessState.READY
        logger.info(
            "Actuation subprocess ready (pid=%s, effector=%s)",
            process.pid,
            effector or "<config>",
        )

    def stop(self) -> None:
        """Stop the Actuation subprocess, escalating only when necessary.

        The cooperative stop event lets ``Actuation.serve`` run its ``finally``
        block and tear the device down.  A child that ignores that request is
        terminated, then killed when the platform exposes ``Process.kill``.
        State is cleared only after the process is confirmed dead; retaining the
        handle on failure prevents a later ``start`` from creating a second
        controller for the same hardware.
        """
        with self._lifecycle_lock:
            self._stop_locked()

    def _stop_locked(
        self,
        *,
        final_state: ChildProcessState = ChildProcessState.STOPPED,
    ) -> None:
        process = self._process
        if process is None:
            self._close_startup_receiver()
            self._state = final_state
            return

        self._state = ChildProcessState.STOPPING
        pid = process.pid
        if process.is_alive():
            if self._stop_event is not None:
                self._stop_event.set()
            process.join(timeout=self._GRACEFUL_TIMEOUT_S)

        if process.is_alive():
            logger.warning(
                "Actuation subprocess did not stop gracefully; terminating (pid=%s)",
                pid,
            )
            process.terminate()
            process.join(timeout=self._TERMINATE_TIMEOUT_S)

        if process.is_alive():
            kill = getattr(process, "kill", None)
            if callable(kill):
                logger.warning(
                    "Actuation subprocess ignored terminate; killing (pid=%s)", pid
                )
                kill()
                process.join(timeout=self._KILL_TIMEOUT_S)

        if process.is_alive():
            message = (
                "Actuation subprocess is still alive after graceful stop, "
                f"terminate, and kill escalation (pid={pid}); refusing to "
                "discard its handle or start another controller"
            )
            logger.error(message)
            self._state = ChildProcessState.FAILED
            raise RuntimeError(message)

        exitcode = process.exitcode
        logger.info(
            "Actuation subprocess stopped (pid=%s, exitcode=%s)",
            pid,
            exitcode,
        )
        close = getattr(process, "close", None)
        if callable(close):
            try:
                close()
            except Exception as exc:  # pragma: no cover - platform defensive path
                logger.warning(
                    "Failed to close stopped Actuation process handle (pid=%s): %s",
                    pid,
                    exc,
                )
        self._process = None
        self._stop_event = None
        self._close_startup_receiver()
        self._effector = None
        self._settings_snapshot = None
        self._state = final_state

    def _close_startup_receiver(self) -> None:
        receiver = self._startup_receiver
        self._startup_receiver = None
        if receiver is None:
            return
        try:
            receiver.close()
        except OSError:
            pass

    def _refresh_state_locked(self) -> None:
        if (
            self._process is not None
            and self._state in (
                ChildProcessState.STARTING,
                ChildProcessState.READY,
            )
            and not self._process.is_alive()
        ):
            self._state = ChildProcessState.FAILED

    @property
    def auto_spawn(self) -> bool:
        """Whether the selected configuration requests a managed subprocess."""
        with self._lifecycle_lock:
            return self._auto_spawn

    @property
    def is_alive(self) -> bool:
        with self._lifecycle_lock:
            self._refresh_state_locked()
            return self._process is not None and self._process.is_alive()

    @property
    def is_ready(self) -> bool:
        with self._lifecycle_lock:
            self._refresh_state_locked()
            return (
                self._state is ChildProcessState.READY
                and self._process is not None
                and self._process.is_alive()
            )

    @property
    def state(self) -> ChildProcessState:
        with self._lifecycle_lock:
            self._refresh_state_locked()
            return self._state

    @property
    def current_effector(self) -> str | None:
        """Effector the running subprocess was spawned with (None if stopped or config default)."""
        with self._lifecycle_lock:
            self._refresh_state_locked()
            return (
                self._effector
                if (
                    self._state is ChildProcessState.READY
                    and self._process is not None
                    and self._process.is_alive()
                )
                else None
            )
