"""Exclude configuration transitions from complete user command operations."""
from contextlib import contextmanager, nullcontext
from contextvars import ContextVar
from functools import wraps
from inspect import iscoroutinefunction
from threading import RLock

from omnibci.application.plugin_profiles import ProfileRevisionConflict


class RuntimeAdmission:
    """Hold only counters under the lock; never hold a thread lock across await."""

    def __init__(self):
        self._lock = RLock()
        self._active = 0
        self._transitioning = False
        self._quarantine: str | None = None
        self._depth = ContextVar(f"command_depth_{id(self)}", default=0)
        self._internal = ContextVar(f"transition_owner_{id(self)}", default=False)

    @contextmanager
    def command(self, *, recovery=False):
        if self._internal.get() or self._depth.get():
            yield
            return
        with self._lock:
            if self._transitioning:
                raise ProfileRevisionConflict("Configuration update in progress; retry after it completes")
            if self._quarantine is not None and not recovery:
                raise ProfileRevisionConflict("Runtime requires recovery; stop the engine before resuming commands")
            self._active += 1
        token = self._depth.set(1)
        try:
            yield
        finally:
            self._depth.reset(token)
            with self._lock:
                self._active -= 1

    @contextmanager
    def transition(self):
        with self._lock:
            if self._transitioning or self._active:
                raise ProfileRevisionConflict("Another runtime command is in progress; retry after it completes")
            self._transitioning = True
        token = self._internal.set(True)
        try:
            yield
        except BaseException as exc:
            if getattr(exc, "recovery_required", False):
                self.quarantine(str(exc))
            raise
        finally:
            self._internal.reset(token)
            with self._lock:
                self._transitioning = False

    @property
    def is_transition_owner(self):
        return self._internal.get()

    @property
    def recovery_required(self) -> bool:
        with self._lock:
            return self._quarantine is not None

    def quarantine(self, reason: str):
        with self._lock:
            self._quarantine = reason

    def clear_quarantine(self):
        with self._lock:
            self._quarantine = None


def admitted(method=None, *, recovery=False):
    """A service command owns admission until all its dependent commands finish."""
    if method is None:
        return lambda function: admitted(function, recovery=recovery)
    def guard(service):
        admission = getattr(service, "_admission", None)
        return admission.command(recovery=recovery) if admission is not None else nullcontext()

    if iscoroutinefunction(method):
        @wraps(method)
        async def asynchronous(self, *args, **kwargs):
            with guard(self):
                return await method(self, *args, **kwargs)
        return asynchronous

    @wraps(method)
    def synchronous(self, *args, **kwargs):
        with guard(self):
            return method(self, *args, **kwargs)
    return synchronous
