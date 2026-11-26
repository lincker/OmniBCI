"""Stop producers and drain pending results before disposing the engine."""
import asyncio
import logging
from contextlib import nullcontext

logger = logging.getLogger(__name__)


class ServerShutdownService:
    def __init__(self, *, engine, configuration, stimulation=None, actuation=None):
        self.stimulation = stimulation
        self.actuation = actuation
        self.engine = engine
        self.configuration = configuration
        self._lock = asyncio.Lock()
        self._complete = False

    def resume(self):
        """Allow a later shutdown attempt after the user returns to the UI."""
        self._complete = False

    async def prepare(self, *, progress=None) -> list[str]:
        def report(phase):
            if progress is not None:
                try:
                    progress(phase)
                except Exception:
                    logger.warning("Unable to report shutdown progress", exc_info=True)

        async with self._lock:
            if self._complete:
                report("stopped")
                return []
            errors = []

            async def attempt(label, function):
                try:
                    await function()
                except Exception as exc:
                    logger.exception("Server shutdown failed: %s", label)
                    errors.append(f"{label}: {exc}")

            if self.configuration is not None:
                report("waiting_for_configuration")
                await attempt("configuration", self.configuration.wait_for_idle)
            guard = self.engine.admission.stop_transition("all") if self.stimulation is not None or self.actuation is not None else nullcontext()
            async with guard:
                if self.stimulation is not None:
                    report("stopping_stimulation")
                    await attempt("stimulation", lambda: asyncio.to_thread(self.stimulation.stop))
                if self.actuation is not None:
                    report("stopping_actuation")
                    await attempt("actuation", self.actuation.stop)
                report("stopping_engine")
                await attempt("engine", self.engine.shutdown)
            self._complete = not errors
            if self._complete:
                report("stopped")
            return errors
