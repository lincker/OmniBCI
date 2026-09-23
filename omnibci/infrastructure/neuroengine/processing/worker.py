"""
SignalProcessingWorker loads the selected pipeline in a dedicated process.

It starts in standby and receives session identity through StartSession.
"""

import logging
import multiprocessing as mp
from copy import deepcopy
from typing import Optional

from omnibci.domain.ports.algorithm import Decoder, EpochDetector, Filter
from omnibci.infrastructure.neuroengine.processing.signal_processor import SignalProcessor

logger = logging.getLogger(__name__)


class SignalProcessingWorker(mp.Process):
    """
    Multiprocessing wrapper for SignalProcessor.

    Starts the processor in standby (no running session); session identity is
    delivered via a start_session command from the API.
    """

    def __init__(self, command_queue, data_manager=None, event_bus=None,
                 sample_rate: int = 1000, baseline_duration: float = 0.0,
                 settings_snapshot: dict | None = None) -> None:
        super().__init__()
        self.data_manager = data_manager
        self.event_bus = event_bus
        self.command_queue = command_queue
        # Injected in the parent process; pickled into the child on .start().
        self.sample_rate = sample_rate
        self.baseline_duration = baseline_duration
        self.settings_snapshot = deepcopy(settings_snapshot)
        self.signal_processor: Optional[SignalProcessor] = None

    def run(self) -> None:
        if self.settings_snapshot is not None:
            from omnibci.conf import settings
            settings.use_snapshot(self.settings_snapshot)
        decoder, detector, filters = self._load_algorithm()
        self._run_processor(decoder, detector, filters, self.sample_rate)

    def _snapshot(self):
        if self.settings_snapshot is not None:
            return deepcopy(self.settings_snapshot)
        from omnibci.conf import settings
        return settings.snapshot()

    def _context(self, snapshot):
        from omnibci.sdk import PluginContext
        return PluginContext(sample_rate=self.sample_rate,
                             channel_names=tuple(snapshot.get("ACQUISITION.channel_names", ())),
                             config_revision=snapshot.get("CONFIG.revision"))

    def _load_algorithm(self):
        from omnibci.infrastructure.plugins.runtime_configuration import load_configured_plugin
        snapshot = self._snapshot()
        context = self._context(snapshot)
        decoder = load_configured_plugin(
            snapshot, "processing.decoder", "decoder", Decoder,
            legacy_key="ALGORITHM.decoder", context=context,
        )
        detector = load_configured_plugin(
            snapshot, "processing.epoch_detector", "epoch_detector", EpochDetector,
            legacy_key="ALGORITHM.epoch_detector", context=context,
        )
        return decoder, detector, self._load_filters(snapshot)

    def _load_filters(self, snapshot=None):
        """Every ordered slot constructs a separate selected filter instance."""
        from omnibci.infrastructure.plugins.runtime_configuration import (
            configured_bindings, load_configured_plugin,
        )
        snapshot = self._snapshot() if snapshot is None else snapshot
        context = self._context(snapshot)
        bindings = configured_bindings(snapshot)
        if bindings is not None:
            return [load_configured_plugin(snapshot, binding.slot, "filter", Filter, context=context)
                    for binding in bindings if binding.kind == "filter"]
        names = snapshot.get("ALGORITHM.filters", [])
        if not isinstance(names, list) or any(not isinstance(name, str) or not name for name in names):
            raise ValueError("ALGORITHM.filters must be an ordered list of plugin aliases")
        return [load_configured_plugin(snapshot, f"processing.filter.{index}", "filter", Filter,
                                       legacy_alias=name, context=context)
                for index, name in enumerate(names)]

    def _run_processor(self, decoder, detector, filters, sample_rate: int) -> None:
        # No self-created session id: the worker starts in "standby" and waits
        # for a start_session command carrying the REST-created session_id, so
        # the aggregate and the read model share one identity
        # (docs/session-id-unification.md). Trials decoded before the first
        # start_session are dropped by BCISession's RUNNING invariant.
        self.signal_processor = SignalProcessor(
            decoder=decoder,
            detector=detector,
            command_queue=self.command_queue,
            data_manager=self.data_manager,
            event_bus=self.event_bus,
            session=None,
            sample_rate=sample_rate,
            baseline_duration=self.baseline_duration,
            filters=filters,
            auto_start_session=False,
        )
        logger.info("SignalProcessor in standby — awaiting start_session command")
        self.signal_processor.run()
