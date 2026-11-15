"""Stop producers and drain pending results before disposing the engine."""
import asyncio
import logging

logger = logging.getLogger(__name__)


class BackendShutdownService:
    def __init__(self, *, engine, configuration):
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
                    logger.exception("Backend shutdown failed: %s", label)
                    errors.append(f"{label}: {exc}")

            if self.configuration is not None:
                report("waiting_for_configuration")
                await attempt("configuration", self.configuration.wait_for_idle)
            report("stopping_engine")
            await attempt("engine", self.engine.shutdown)
            self._complete = not errors
            if self._complete:
                report("stopped")
            return errors
