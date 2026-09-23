"""
AcquisitionWorker runs a configured signal source in its own process.

Plugin loading and source construction remain outside the domain layer.
"""
import multiprocessing as mp
from copy import deepcopy
from typing import Optional

from omnibci.domain.ports.acquisition import SignalSource
from omnibci.infrastructure.process_lifecycle import StartupReporter
from omnibci.infrastructure.acquisition.acquisition import (
    Acquisition,
    CommandQueue,
    DataSink,
)


class AcquisitionWorker(mp.Process):
    """
    Runs Acquisition in a dedicated subprocess.

    Responsibilities:
      - Load the DataSource plugin specified in config.
      - Construct the Acquisition domain service.
      - Forward commands from the command_queue to Acquisition.execute().
    """

    def __init__(
        self,
        command_queue: CommandQueue,
        data_manager: Optional[DataSink] = None,
        event_bus=None,
        settings_snapshot: dict | None = None,
        startup_reporter: StartupReporter | None = None,
    ) -> None:
        super().__init__()
        self.data_source: SignalSource
        self.acquisitor: Optional[Acquisition] = None
        self.command_queue = command_queue
        self.data_manager = data_manager
        # EventBus serialization retains only its multiprocessing queue.
        self.event_bus = event_bus
        self.settings_snapshot = deepcopy(settings_snapshot)
        self.startup_reporter = startup_reporter
        self._startup_stage = "settings"

    def run(self) -> None:
        try:
            if self.settings_snapshot is not None:
                from omnibci.conf import settings
                settings.use_snapshot(self.settings_snapshot)
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
        self.acquisitor = Acquisition(
            data_source=self.data_source,
            command_queue=self.command_queue,
            data_manager=self.data_manager,
            event_bus=self.event_bus,
            generation=(self.settings_snapshot or {}).get("CONFIG.generation"),
            config_revision=(self.settings_snapshot or {}).get("CONFIG.revision"),
        )
        if self.startup_reporter is not None:
            self.startup_reporter.ready()
        self._startup_stage = "run"
        self.acquisitor.run()

    def _load_data_source(self) -> None:
        from omnibci.conf import settings
        from omnibci.infrastructure.plugins.runtime_configuration import load_configured_plugin
        snapshot = self.settings_snapshot if self.settings_snapshot is not None else settings.snapshot()
        self.data_source = load_configured_plugin(
            snapshot, "acquisition.source", "acquisition", SignalSource,
            legacy_key="ACQUISITION.signal_source",
        )
