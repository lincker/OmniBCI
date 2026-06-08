"""
SignalProcessingWorker — multiprocessing 包装器。

在独立子进程中运行 SignalProcessor：加载算法插件（decoder + epoch detector），
创建并启动一个 BCISession，把会话生命周期绑定到本进程。
"""
from __future__ import annotations

import logging
import multiprocessing as mp
import os
from typing import Optional

import omnibci
from omnibci.domain.session import BCISession
from omnibci.domain.ports.algorithm import Decoder, EpochDetector, Filter
from omnibci.infrastructure.neuroengine.processing.signal_processor import SignalProcessor

logger = logging.getLogger(__name__)


class SignalProcessingWorker(mp.Process):
    """
    Multiprocessing wrapper for SignalProcessor.

    Creates a BCISession with a fresh ID and passes it into SignalProcessor
    so the session lifecycle is tied to this process.
    """

    def __init__(self, command_queue, data_manager=None, event_bus=None,
                 sample_rate: int = 1000, baseline_duration: float = 0.0) -> None:
        super().__init__()
        self.data_manager = data_manager
        self.event_bus = event_bus
        self.command_queue = command_queue
        # Injected in the parent process; pickled into the child on .start().
        self.sample_rate = sample_rate
        self.baseline_duration = baseline_duration
        self.signal_processor: Optional[SignalProcessor] = None

    def run(self) -> None:
        decoder, detector, filters = self._load_algorithm()
        self._run_processor(decoder, detector, filters, self.sample_rate)

    def _load_algorithm(self):
        algorithm_plugin_path = os.path.join(
            next(iter(omnibci.__path__)), 'infrastructure', 'plugins', 'algorithms'
        )
        from omnibci.shared.loader import PluginLoader
        from omnibci.shared.factory import ObjectFactory

        PluginLoader.load_from_dir(
            algorithm_plugin_path,
            base_package='omnibci.infrastructure.plugins.algorithms',
        )
        decoder = ObjectFactory.create(Decoder, config_key='ALGORITHM.decoder')
        detector = ObjectFactory.create(EpochDetector, config_key='ALGORITHM.epoch_detector')
        filters = self._load_filters()

        # sample_rate is injected at construction (self.sample_rate), no settings read here.
        return decoder, detector, filters

    def _load_filters(self):
        """
        Build the ordered preprocessing Filter chain from ``ALGORITHM.filters``
        (a list of plugin_type ids, applied in order, e.g.
        ``["BASELINE_CORRECTION"]``). Missing/empty config → no filters, so the
        pipeline behaves exactly as ``decoder.decode(window)``.

        Filters are stateless pure transforms (no ``for_new_epoch``), so a single
        instance per type is shared across all epochs.
        """
        from omnibci.conf import settings

        try:
            names = settings.get('ALGORITHM.filters')
        except Exception:
            names = None
        if not names:
            return []

        registry = Filter._target_registry
        filters = []
        for name in names:
            cls = registry.get(name)
            if cls is None:
                logger.error("[Filter] Unknown filter '%s'. Available: %s",
                             name, list(registry.keys()))
                continue
            filters.append(cls())
            logger.info("[Filter] Loaded '%s'", name)
        return filters

    def _run_processor(self, decoder, detector, filters, sample_rate: int) -> None:
        session = BCISession()
        session.start()
        logger.info("BCISession started: %s", session.session_id)

        self.signal_processor = SignalProcessor(
            decoder=decoder,
            detector=detector,
            command_queue=self.command_queue,
            data_manager=self.data_manager,
            event_bus=self.event_bus,
            session=session,
            sample_rate=sample_rate,
            baseline_duration=self.baseline_duration,
            filters=filters,
        )
        self.signal_processor.run()
