"""Stop producers and drain recorded results before disposing the engine."""
import asyncio
import logging

from omnibci.application.commands.stimulation_commands import StopStimulationCommand

logger = logging.getLogger(__name__)


class BackendShutdownService:
    def __init__(self, *, engine, stimulation, stimulation_process, actuation_process,
                 recording, configuration):
        self.engine = engine
        self.stimulation = stimulation
        self.stimulation_process = stimulation_process
        self.actuation_process = actuation_process
        self.recording = recording
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

            async def attempt(label, function, *, asynchronous=False):
                try:
                    if asynchronous:
                        await function()
                    else:
                        await asyncio.to_thread(function)
                except Exception as exc:
                    logger.exception("Backend shutdown failed: %s", label)
                    errors.append(f"{label}: {exc}")

            if self.configuration is not None:
                report("waiting_for_configuration")
                await attempt("configuration", self.configuration.wait_for_idle, asynchronous=True)
            report("stopping_stimulation")
            await attempt("stimulation", lambda: self.stimulation.handle_stop_stimulation(StopStimulationCommand())
                          if self.stimulation.is_active else None)
            # Manager cleanup is still attempted after a failed domain stop.
            for label, manager in (("stimulation process", self.stimulation_process),
                                   ("actuation process", self.actuation_process)):
                if manager is not None:
                    report("stopping_" + label.replace(" ", "_"))
                    await attempt(label, manager.stop)
            if self.recording is not None:
                report("draining_recording")
                await attempt("recording drain", lambda: self.recording.stop()
                              if self.recording.active_session_id is not None else None)
            report("stopping_engine")
            await attempt("engine", self.engine.shutdown, asynchronous=True)
            self._complete = not errors
            if self._complete:
                report("stopped")
            return errors
