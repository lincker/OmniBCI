"""
SignalProcessingWorker loads the selected pipeline in a dedicated process.

It starts in standby until its signal pipeline has been prepared.
"""

import logging
import multiprocessing as mp
from copy import deepcopy
from pathlib import Path
from typing import Optional

from omnibci.infrastructure.plugins.execution_history import PluginExecutionHistory
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
                 *, settings_snapshot: dict,
                 startup_reporter: StartupReporter | None = None,
                 runtime_counters=None,
                 execution_history_root: Path | None = None) -> None:
        super().__init__()
        self.data_manager = data_manager
        self.event_bus = event_bus
        self.command_queue = command_queue
        # Injected in the parent process; pickled into the child on .start().
        self.baseline_duration = baseline_duration
        self.settings_snapshot = deepcopy(settings_snapshot)
        self.signal_processor: Optional[SignalProcessor] = None
        self.startup_reporter = startup_reporter
        self.runtime_counters = runtime_counters
        self.execution_history_root = execution_history_root.resolve() if execution_history_root is not None else None

    def run(self) -> None:
        try:
            self._startup_stage = "processor"
            self._run_processor()
        except BaseException as exc:
            if self.startup_reporter is not None:
                self.startup_reporter.failed(self._startup_stage, exc)
            raise
        finally:
            if self.startup_reporter is not None:
                self.startup_reporter.close()

    def _run_processor(self) -> None:
        record_execution = (PluginExecutionHistory(self.execution_history_root).record
                            if self.execution_history_root is not None else None)
        self.signal_processor = SignalProcessor(
            decoder=None,
            detector=None,
            command_queue=self.command_queue,
            data_manager=self.data_manager,
            event_bus=self.event_bus,
            baseline_duration=self.baseline_duration,
            filters=[],
            generation=self.settings_snapshot.get("CONFIG.generation"),
            processing_revision=self.settings_snapshot.get("CONFIG.module_revisions", {}).get("processing"),
            acquisition_revision=self.settings_snapshot.get("CONFIG.module_revisions", {}).get("acquisition"),
            engine_revision=self.settings_snapshot.get("CONFIG.engine_revision"),
            settings_snapshot=self.settings_snapshot,
            runtime_counters=self.runtime_counters,
            record_execution=record_execution,
        )
        if self.startup_reporter is not None:
            self.startup_reporter.ready()
        self._startup_stage = "run"
        logger.info("SignalProcessor in standby — awaiting signal preparation")
        self.signal_processor.run()
