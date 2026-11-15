"""Exclude overlapping module transitions from complete user operations."""
import asyncio
from contextlib import asynccontextmanager, contextmanager, nullcontext
from contextvars import ContextVar
from functools import wraps
from inspect import iscoroutinefunction
from threading import Condition, RLock

from omnibci.application.activation_settings import MODULES
from omnibci.application.plugin_profiles import ProfileRevisionConflict


class RuntimeAdmission:
    """Reserve scopes under a short lock; never hold a thread lock across await."""

    def __init__(self):
        self._lock = RLock()
        self._condition = Condition(self._lock)
        self._owners = {}
        self._stopping = {}
        self._quarantine = {}
        self._waiters = set()
        self._owner = ContextVar(f"runtime_owner_{id(self)}", default=None)

    def _scopes(self, scopes):
        if scopes is None:
            owner = self._owners.get(self._owner.get())
            return owner[0] if owner is not None else frozenset(MODULES)
        scopes = {scopes} if isinstance(scopes, str) else set(scopes)
        if scopes & {"all", "system"}:
            return frozenset(MODULES)
        if scopes - set(MODULES):
            raise ValueError("Unsupported runtime scope")
        return frozenset(scopes)

    def _nested(self, scopes, *, transition=False):
        owner = self._owners.get(self._owner.get())
        if owner is None:
            return False
        if not scopes <= owner[0] or transition and owner[1] != "transition":
            raise ProfileRevisionConflict("Nested operation exceeds its reserved runtime scope")
        return True

    def _check(self, scopes, *, transition, recovery=False):
        if any(scopes & held for held in self._stopping.values()):
            raise ProfileRevisionConflict("Module stop is in progress; retry after it completes")
        if any(scopes & held and (transition or kind == "transition")
               for held, kind in self._owners.values()):
            raise ProfileRevisionConflict("Another operation owns the requested runtime modules")
        if not recovery and self.requires_recovery(scopes):
            raise ProfileRevisionConflict("Runtime module requires recovery; stop it before resuming commands")

    @property
    def _active(self):
        with self._lock:
            return sum(kind == "command" for _, kind in self._owners.values())

    @contextmanager
    def command(self, scopes=None, *, recovery=False):
        with self._lock:
            scopes = self._scopes(scopes)
            nested = self._nested(scopes)
            if not nested:
                self._check(scopes, transition=False, recovery=recovery)
                owner = object()
                self._owners[owner] = scopes, "command"
        if nested:
            yield
            return
        context = self._owner.set(owner)
        try:
            yield
        finally:
            self._owner.reset(context)
            self.release_reservation(owner)

    def reserve_transition(self, scopes=None, *, recovery=False):
        """Reserve before publishing queued work so Stop cannot pass it."""
        with self._lock:
            scopes = self._scopes(scopes)
            self._check(scopes, transition=True, recovery=recovery)
            owner = object()
            self._owners[owner] = scopes, "transition"
            return owner

    def release_reservation(self, owner):
        with self._condition:
            self._owners.pop(owner, None)
            self._condition.notify_all()
            for loop, event in tuple(self._waiters):
                loop.call_soon_threadsafe(event.set)

    @contextmanager
    def transition(self, scopes=None, *, reservation=None, recovery=False):
        with self._lock:
            scopes = self._scopes(scopes)
            nested = reservation is None and self._nested(scopes, transition=True)
            if not nested:
                if reservation is None:
                    reservation = self.reserve_transition(scopes, recovery=recovery)
                elif self._owners.get(reservation) != (scopes, "transition"):
                    raise RuntimeError("Runtime reservation does not own the requested modules")
        if nested:
            yield
            return
        context = self._owner.set(reservation)
        try:
            yield
        except BaseException as exc:
            if getattr(exc, "recovery_required", False):
                self.quarantine(str(exc), scopes)
            raise
        finally:
            self._owner.reset(context)
            self.release_reservation(reservation)

    def _begin_stop(self, scopes):
        with self._lock:
            scopes = self._scopes(scopes)
            if self._nested(scopes):
                return scopes, None
            stopping = object()
            self._stopping[stopping] = scopes
            return scopes, stopping

    def _reserve_stopped(self, scopes):
        if any(scopes & held for held, _ in self._owners.values()):
            return None
        reservation = object()
        self._owners[reservation] = scopes, "transition"
        return reservation

    @contextmanager
    def stop_transition_sync(self, scopes=None):
        """Worker-thread Stop waits without holding the runtime state lock."""
        scopes, stopping = self._begin_stop(scopes)
        if stopping is None:
            yield
            return
        reservation = None
        try:
            with self._condition:
                while reservation is None:
                    reservation = self._reserve_stopped(scopes)
                    if reservation is None:
                        self._condition.wait()
            with self.transition(scopes, reservation=reservation, recovery=True):
                yield
        finally:
            with self._lock:
                self._stopping.pop(stopping, None)
            if reservation is not None:
                self.release_reservation(reservation)

    @asynccontextmanager
    async def stop_transition(self, scopes=None):
        """Close admission, let existing owners settle, then stop exclusively."""
        scopes, stopping = self._begin_stop(scopes)
        if stopping is None:
            yield
            return
        waiter = (asyncio.get_running_loop(), asyncio.Event())
        reservation = None
        with self._lock:
            self._waiters.add(waiter)
        try:
            while True:
                with self._lock:
                    reservation = self._reserve_stopped(scopes)
                    if reservation is not None:
                        break
                    waiter[1].clear()
                await waiter[1].wait()
            with self.transition(scopes, reservation=reservation, recovery=True):
                yield
        finally:
            with self._lock:
                self._waiters.discard(waiter)
                self._stopping.pop(stopping, None)
            if reservation is not None:
                self.release_reservation(reservation)

    @property
    def is_transition_owner(self):
        with self._lock:
            owner = self._owners.get(self._owner.get())
            return owner is not None and owner[1] == "transition"

    def owns(self, scopes):
        with self._lock:
            owner = self._owners.get(self._owner.get())
            return owner is not None and self._scopes(scopes) <= owner[0]

    @property
    def recovery_required(self) -> bool:
        with self._lock:
            return bool(self._quarantine)

    @property
    def recovery_status(self):
        with self._lock:
            return dict(self._quarantine)

    def requires_recovery(self, scopes):
        with self._lock:
            scopes = self._scopes(scopes)
            return bool(scopes and ("system" in self._quarantine or scopes & self._quarantine.keys()))

    def quarantine(self, reason: str, scopes=None):
        with self._lock:
            scopes = self._scopes(scopes)
            if scopes == frozenset(MODULES):
                self._quarantine["system"] = reason
            else:
                self._quarantine.update({module: reason for module in scopes})

    def clear_quarantine(self, scopes=None):
        with self._lock:
            if scopes == "system":
                self._quarantine.pop("system", None)
                return
            if scopes is None or scopes == "all":
                self._quarantine.clear()
                return
            for module in self._scopes(scopes):
                self._quarantine.pop(module, None)


def admitted(method=None, *, scopes=None, recovery=False):
    """An application command owns its modules until dependent commands finish."""
    if method is None:
        return lambda function: admitted(function, scopes=scopes, recovery=recovery)

    def guard(service, args, kwargs):
        admission = getattr(service, "_admission", None)
        selected = scopes(service, *args, **kwargs) if callable(scopes) else scopes
        return admission.command(selected, recovery=recovery) if admission is not None else nullcontext()

    if iscoroutinefunction(method):
        @wraps(method)
        async def asynchronous(self, *args, **kwargs):
            with guard(self, args, kwargs):
                return await method(self, *args, **kwargs)
        return asynchronous

    @wraps(method)
    def synchronous(self, *args, **kwargs):
        with guard(self, args, kwargs):
            return method(self, *args, **kwargs)
    return synchronous
