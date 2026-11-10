"""Best-effort acquisition diagnostics on a dedicated bounded process queue."""

import logging
import os
from queue import Empty
from threading import Event, Thread
from time import monotonic


LOGGER_NAME = "omnibci.infrastructure.acquisition"


class AcquisitionLogForwarder(logging.Handler):
    """Forward acquisition records without blocking on a full or broken queue."""

    def __init__(self, queue, *, source_id: str, generation: str | None):
        super().__init__(logging.INFO)
        self.queue = queue
        self.source_id = source_id
        self.generation = generation
        self.dropped_records = 0
        self._logger = logging.getLogger(LOGGER_NAME)
        self._previous = None

    def __enter__(self):
        self._previous = self._logger.level, self._logger.propagate
        # An unavailable consumer must not make the child wait for its queue
        # feeder during process exit. Final diagnostics are best-effort.
        cancel_join = getattr(self.queue, "cancel_join_thread", None)
        if callable(cancel_join):
            cancel_join()
        self._logger.setLevel(logging.INFO)
        self._logger.propagate = False
        self._logger.addHandler(self)
        return self

    def __exit__(self, *exc_info):
        self._logger.removeHandler(self)
        self._logger.setLevel(self._previous[0])
        self._logger.propagate = self._previous[1]
        self.close()

    def emit(self, record):
        try:
            message = record.getMessage()
            if record.exc_info:
                message += "\n" + logging.Formatter().formatException(record.exc_info)
            # Only simple, bounded values cross the process boundary. Do not
            # send exception objects, logging handlers, or arbitrary log args.
            message = message[:4096]
            self.queue.put_nowait({
                "name": record.name,
                "levelno": record.levelno,
                "levelname": record.levelname,
                "created": record.created,
                "msecs": record.msecs,
                "process": os.getpid(),
                "msg": "source_id=%s generation=%s worker_pid=%s %s" % (
                    str(self.source_id)[:256], str(self.generation)[:256], os.getpid(), message,
                ),
                "args": (),
            })
        except Exception:
            # Logging must never fail acquisition or recursively use stderr.
            self.dropped_records += 1


class AcquisitionLogListener:
    """Deliver child diagnostics to the backend's existing logging handlers."""

    def __init__(self, queue):
        self.queue = queue
        self._thread = None
        self._stopping = Event()
        self._deadline = None
        self.read_errors = 0
        self.write_errors = 0

    @property
    def stopped(self):
        return self._thread is None or not self._thread.is_alive()

    def start(self):
        if self._thread is not None:
            if not self.stopped:
                return
            raise RuntimeError("A stopped acquisition log listener cannot restart")
        self._thread = Thread(target=self._consume, name="omnibci-acquisition-log-listener", daemon=True)
        try:
            self._thread.start()
        except BaseException:
            self._thread = None
            raise

    def stop(self, timeout_s=1.0):
        if self._deadline is None:
            self._deadline = monotonic() + timeout_s
        self._stopping.set()
        if self._thread is not None:
            self._thread.join(timeout_s + 0.05)
        return self.stopped

    def _consume(self):
        while True:
            if self._stopping.is_set() and monotonic() >= self._deadline:
                return
            try:
                payload = self.queue.get(timeout=0.05)
            except Empty:
                if self._stopping.is_set():
                    return
                continue
            except Exception:
                self.read_errors += 1
                return
            try:
                name = payload["name"]
                if name != LOGGER_NAME and not name.startswith(LOGGER_NAME + "."):
                    continue
                logger = logging.getLogger(name)
                if logger.isEnabledFor(payload["levelno"]):
                    logger.handle(logging.makeLogRecord(payload))
            except Exception:
                self.write_errors += 1
