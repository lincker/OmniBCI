"""
SignalProcessingWorker loads the selected pipeline in a dedicated process.

It starts in standby until its signal pipeline has been prepared.
"""

import logging
import multiprocessing as mp
from copy import deepcopy
from typing import Optional

from omnibci.infrastructure.signal_processing.signal_processor import SignalProcessor
from omnibci.infrastructure.process_lifecycle import StartupReporter

logger = logging.getLogger(__name__)


class SignalProcessingWorker(mp.Process):
    """
    Multiprocessing wrapper for SignalProcessor.

    Prepares the selected pipeline on demand and publishes decoded results.
    """

    def __init__(self, command_queue, data_manager=None, event_bus=None,
                 baseline_duration: float = 0.0,
                 settings_snapshot: dict | None = None,
                 startup_reporter: StartupReporter | None = None) -> None:
        super().__init__()
        self.data_manager = data_manager
        self.event_bus = event_bus
        self.command_queue = command_queue
        # Injected in the parent process; pickled into the child on .start().
        self.baseline_duration = baseline_duration
        self.settings_snapshot = deepcopy(settings_snapshot)
        self.signal_processor: Optional[SignalProcessor] = None
        self.startup_reporter = startup_reporter
        self._startup_stage = "settings"

    def run(self) -> None:
        try:
            if self.settings_snapshot is not None:
                from omnibci.conf import settings
                settings.use_snapshot(self.settings_snapshot)
            self._startup_stage = "processor"
            self._run_processor()
        except BaseException as exc:
            if self.startup_reporter is not None:
                self.startup_reporter.failed(self._startup_stage, exc)
            raise
        finally:
            if self.startup_reporter is not None:
                self.startup_reporter.close()

    def _snapshot(self):
        if self.settings_snapshot is not None:
            return deepcopy(self.settings_snapshot)
        from omnibci.conf import settings
        return settings.snapshot()

    def _run_processor(self) -> None:
        self.signal_processor = SignalProcessor(
            decoder=None,
            detector=None,
            command_queue=self.command_queue,
            data_manager=self.data_manager,
            event_bus=self.event_bus,
            baseline_duration=self.baseline_duration,
            filters=[],
            generation=(self.settings_snapshot or {}).get("CONFIG.generation"),
            config_revision=(self.settings_snapshot or {}).get("CONFIG.revision"),
            settings_snapshot=self._snapshot(),
        )
        if self.startup_reporter is not None:
            self.startup_reporter.ready()
        self._startup_stage = "run"
        logger.info("SignalProcessor in standby — awaiting signal preparation")
        self.signal_processor.run()
