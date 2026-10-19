"""
AcquisitionWorker runs a configured signal source in its own process.

Plugin loading and source construction remain outside the domain layer.
"""
import multiprocessing as mp
from copy import deepcopy
from collections.abc import Callable
from typing import Optional

from omnibci.application.processing_source import requires_signal_preparation
from omnibci.domain.ports.acquisition import SignalSource
from omnibci.domain.ports.recording import SignalRecorder
from omnibci.infrastructure.acquisition.recording import LocalSignalRecorder
from omnibci.infrastructure.process_lifecycle import StartupReporter
from omnibci.infrastructure.acquisition.acquisition import (
    AcquisitionRuntime,
    CommandQueue,
    DataSink,
)


class AcquisitionWorker(mp.Process):
    """
    Runs AcquisitionRuntime in a dedicated subprocess.

    Responsibilities:
      - Load the DataSource plugin specified in config.
      - Construct the acquisition runtime with explicit source and preparation context.
      - Forward commands from the command_queue to AcquisitionRuntime.execute().
    """

    def __init__(
        self,
        command_queue: CommandQueue,
        data_manager: Optional[DataSink] = None,
        event_bus=None,
        settings_snapshot: dict | None = None,
        startup_reporter: StartupReporter | None = None,
        *,
        recorder_factory: Callable[[], SignalRecorder] = LocalSignalRecorder,
    ) -> None:
        super().__init__()
        self.data_source: SignalSource
        self.runtime: Optional[AcquisitionRuntime] = None
        self.command_queue = command_queue
        self.data_manager = data_manager
        # EventBus serialization retains only its multiprocessing queue.
        self.event_bus = event_bus
        self.settings_snapshot = deepcopy(settings_snapshot)
        self.startup_reporter = startup_reporter
        # Use an importable factory: spawn must construct file owners in the child.
        self._recorder_factory = recorder_factory
        self._startup_stage = "settings"

    def run(self) -> None:
        try:
            from omnibci.conf import settings
            settings.use_snapshot(self._settings_snapshot())
            self._startup_stage = "signal_source"
            self._load_data_source()
            self._startup_stage = "acquisition"
            self._run()
        except BaseException as exc:
            if self.startup_reporter is not None:
                self.startup_reporter.failed(self._startup_stage, exc)
            raise
        finally:
            if self.startup_reporter is not None:
                self.startup_reporter.close()

    def _run(self) -> None:
        snapshot = self._settings_snapshot()
        self.runtime = AcquisitionRuntime(
            data_source=self.data_source,
            command_queue=self.command_queue,
            data_manager=self.data_manager,
            event_bus=self.event_bus,
            source_id=snapshot.get("ACQUISITION.signal_source") or getattr(
                self.data_source, "plugin_type", type(self.data_source).__name__),
            preparation_required=requires_signal_preparation(snapshot),
            generation=snapshot.get("CONFIG.generation"),
            config_revision=snapshot.get("CONFIG.revision"),
            recorder=self._recorder_factory(),
        )
        if self.startup_reporter is not None:
            self.startup_reporter.ready()
        self._startup_stage = "run"
        self.runtime.run()

    def _settings_snapshot(self) -> dict:
        if self.settings_snapshot is None:
            from omnibci.conf import settings
            self.settings_snapshot = settings.snapshot()
        return self.settings_snapshot

    def _load_data_source(self) -> None:
        from omnibci.infrastructure.plugins.runtime_configuration import load_configured_plugin
        self.data_source = load_configured_plugin(
            self._settings_snapshot(), "acquisition.source", "acquisition", SignalSource,
        )
