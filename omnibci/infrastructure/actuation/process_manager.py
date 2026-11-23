"""ActuationProcessManager — optional subprocess launcher for single-machine deployments.

Multi-machine deployment
------------------------
Set ``ACTUATION.auto_spawn = false`` in settings.toml and start the actuation
process manually on the device machine:

    python scripts/run_actuation.py

Single-machine deployment
--------------------------
Leave ``ACTUATION.auto_spawn = true``. The server spawns the subprocess when
Actuation starts and terminates it when Actuation or the engine stops.

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
from uuid import uuid4

from omnibci.infrastructure.plugin_management.preparation import configured_bindings, execution_receipt

from omnibci.application.configuration.definitions import MODULE_SETTING_DEFAULTS
from omnibci.application.configuration.definitions import module_runtime_settings
from omnibci.infrastructure.process_lifecycle import (
    ChildProcessState,
    StartupReporter,
    module_runtime_identity,
    wait_for_startup_report,
)

logger = logging.getLogger(__name__)

# Managed children must inherit the caller's current environment, not a forkserver's.
mp = multiprocessing.get_context("spawn")


def _configured_effector(snapshot: dict) -> str | None:
    bindings = configured_bindings(snapshot, slot="actuation.effector")
    if not bindings:
        return None
    return bindings[0].selection.canonical_id


def _run_actuation(
    stop_event: Any = None,
    startup_connection: Any = None,
    *,
    settings_snapshot: dict,
) -> None:
    """Actuation subprocess entry point.  Runs in the spawned child process.

    The application receives the pinned snapshot directly.
    """
    reporter = (
        StartupReporter(
            startup_connection, component="Actuation",
            module_revisions={"actuation": settings_snapshot.get("CONFIG.module_revisions", {}).get("actuation")},
            engine_revision=settings_snapshot.get("CONFIG.engine_revision"),
            generation=settings_snapshot.get("CONFIG.generation"),
        )
        if startup_connection is not None
        else None
    )
    try:
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

    Each start uses the pinned snapshot's managed or external mode.
    Desktop hosts disable process termination and retain unresponsive children.
    """

    _GRACEFUL_TIMEOUT_S = 3.0
    _TERMINATE_TIMEOUT_S = 3.0
    _KILL_TIMEOUT_S = 3.0
    _STARTUP_TIMEOUT_S = 10.0

    def __init__(
        self,
        *,
        startup_timeout_s: float = _STARTUP_TIMEOUT_S,
        allow_process_termination: bool = True,
        record_execution=None,
    ) -> None:
        if startup_timeout_s <= 0:
            raise ValueError("startup_timeout_s must be greater than zero")
        self._record_execution = record_execution
        self._auto_spawn = True
        self._settings_snapshot: dict | None = None
        self._startup_timeout_s = float(startup_timeout_s)
        self._allow_process_termination = allow_process_termination
        self._process: mp.Process | None = None
        self._stop_event: Any = None
        self._startup_receiver: Any = None
        # Effector from the running subprocess's pinned snapshot.
        self._effector: str | None = None
        self._state = ChildProcessState.STOPPED
        self._cleanup_error: str | None = None
        self._lifecycle_lock = RLock()

    # ── Public API ─────────────────────────────────────────────────────────

    def start(self, *, settings_snapshot: dict) -> None:
        """Start a managed child or release it when external mode is selected.

        The snapshot determines the selected effector and managed mode.
        """
        with self._lifecycle_lock:
            self._start_locked(settings_snapshot)

    def _start_locked(self, settings_snapshot: dict) -> None:
        if self._process is None and self._cleanup_error is not None:
            raise RuntimeError(self._cleanup_error)
        snapshot = deepcopy(settings_snapshot)
        auto_spawn = snapshot.get(
            "ACTUATION.auto_spawn", MODULE_SETTING_DEFAULTS["actuation"]["ACTUATION.auto_spawn"],
        )
        if not auto_spawn:
            self._stop_locked()
            self._auto_spawn = False
            logger.debug("auto_spawn=False — Actuation process must be started manually")
            return
        effector = _configured_effector(snapshot)
        self._auto_spawn = True
        self._refresh_state_locked()
        if self._process is not None:
            process_alive = self._process.is_alive()
            if (
                process_alive
                and self._state is ChildProcessState.READY
                and effector == self._effector
                and module_runtime_identity(snapshot, "actuation")
                == module_runtime_identity(self._settings_snapshot, "actuation")
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

        snapshot["CONFIG.operation_id"] = uuid4().hex
        expected_execution = execution_receipt(snapshot, "actuation", snapshot["CONFIG.operation_id"])
        stop_event = mp.Event()
        startup_receiver, startup_sender = mp.Pipe(duplex=False)
        process = mp.Process(
            target=_run_actuation,
            args=(stop_event, startup_sender),
            name="ActuationProcess",
            daemon=self._allow_process_termination,
            kwargs={"settings_snapshot": module_runtime_settings(snapshot, "actuation")},
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
            report = wait_for_startup_report(
                startup_receiver,
                process,
                component="Actuation",
                timeout_s=self._startup_timeout_s,
                expected_module_revisions={"actuation": snapshot.get("CONFIG.module_revisions", {}).get("actuation")},
                expected_engine_revision=snapshot.get("CONFIG.engine_revision"),
                expected_generation=snapshot.get("CONFIG.generation"),
                expected_execution=expected_execution,
            )
            if self._record_execution is not None:
                self._record_execution(report.execution)
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
        """Stop the Actuation subprocess according to the host policy.

        The cooperative stop event lets ``Actuation.serve`` run its ``finally``
        block and tear the device down.  A child that ignores that request is
        terminated, then killed when the host permits automatic termination.
        Desktop hosts instead report incomplete cleanup for a user decision.
        A live process retains its handle. An unclean or forced exit releases
        the dead process handle but retains failed ownership and blocks replacement;
        process death does not establish physical output settlement.
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
            if self._cleanup_error is not None:
                raise RuntimeError(self._cleanup_error)
            self._state = final_state
            return

        self._state = ChildProcessState.STOPPING
        pid = process.pid
        if process.is_alive():
            if self._stop_event is not None:
                self._stop_event.set()
            process.join(timeout=self._GRACEFUL_TIMEOUT_S)

        if process.is_alive():
            if not self._allow_process_termination:
                self._state = ChildProcessState.FAILED
                raise RuntimeError(
                    f"Actuation subprocess did not stop gracefully (pid={pid}); "
                    "process retained for an explicit force-exit decision"
                )
            logger.warning(
                "Actuation subprocess did not stop gracefully; terminating (pid=%s)",
                pid,
            )
            self._cleanup_error = (
                f"Actuation cleanup was not confirmed after forced termination (pid={pid}); "
                "replacement is blocked"
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
        if exitcode != 0:
            self._cleanup_error = (
                f"Actuation cleanup was not confirmed (pid={pid}, exitcode={exitcode}); "
                "replacement is blocked"
            )
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
        if self._cleanup_error is not None:
            self._state = ChildProcessState.FAILED
            raise RuntimeError(self._cleanup_error)
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
