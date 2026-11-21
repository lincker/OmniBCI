
import multiprocessing as mp
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from omnibci.infrastructure.plugins.execution_history import PluginExecutionHistory
from omnibci.infrastructure.plugins.preparation import execution_receipt

from omnibci.application.configuration.definitions import ENGINE_SETTING_DEFAULTS, MODULE_SETTING_DEFAULTS, module_runtime_settings
from omnibci.infrastructure.acquisition.preview_channel import PreviewChannel
from omnibci.infrastructure.acquisition.preview_cache import PreviewCache
from omnibci.infrastructure.acquisition.logging import AcquisitionLogListener
from omnibci.infrastructure.acquisition.worker import AcquisitionWorker
from omnibci.infrastructure.ipc.data_manager import DataManager
from omnibci.infrastructure.ipc.event_bus import EventBus
from omnibci.infrastructure.neuroengine.engine import NeuroEngine
from omnibci.infrastructure.signal_processing.worker import SignalProcessingWorker
from omnibci.infrastructure.neuroengine.gateway import PeerGateway
from omnibci.infrastructure.neuroengine.actuation_translator import ActuationCommandTranslator
from omnibci.infrastructure.neuroengine.actuation_feedback import ActuationFeedbackHandler
from omnibci.infrastructure.process_lifecycle import StartupChannel, StartupReporter


@dataclass
class NeuroEngineConfig:
    """
    Host controls for NeuroEngineBuilder, resolved from its runtime snapshot.
    """
    queue_size: int = ENGINE_SETTING_DEFAULTS["NEUROENGINE.queue_size"]
    debug: bool = ENGINE_SETTING_DEFAULTS["debug"]
    startup_timeout_s: float = ENGINE_SETTING_DEFAULTS["NEUROENGINE.startup_timeout_s"]

    def __post_init__(self):
        if self.queue_size <= 0:
            raise ValueError("queue_size must be positive")

    @classmethod
    def from_settings(cls, settings_snapshot: dict) -> "NeuroEngineConfig":
        """Build host controls from an explicitly supplied runtime snapshot."""
        source = settings_snapshot

        def _get(key, default):
            value = source.get(key, default)
            return default if value is None else value

        return cls(
            queue_size=int(_get("NEUROENGINE.queue_size", cls.queue_size)),
            debug=bool(_get("debug", cls.debug)),
            startup_timeout_s=float(_get("NEUROENGINE.startup_timeout_s", cls.startup_timeout_s)),
        )


class NeuroEngineBuilder:
    """
    Assembles a NeuroEngine from its constituent parts.

    Usage::

        engine = NeuroEngineBuilder(settings_snapshot=snapshot).build()
        engine = NeuroEngineBuilder(config, settings_snapshot=snapshot).build()
    """

    def __init__(self, config: NeuroEngineConfig | None = None,
                 *, settings_snapshot: dict | None = None,
                 allow_process_termination: bool = True,
                 execution_history_root: Path | None = None) -> None:
        if settings_snapshot is None:
            raise ValueError("NeuroEngineBuilder requires a pinned settings snapshot")
        self._execution_history_root = execution_history_root.resolve() if execution_history_root is not None else None
        self._record_execution = (PluginExecutionHistory(self._execution_history_root).record
                                  if self._execution_history_root is not None else None)
        self._settings_snapshot = deepcopy(settings_snapshot)
        self._config = config or NeuroEngineConfig.from_settings(self._settings_snapshot)
        self._allow_process_termination = allow_process_termination

    def build(self) -> NeuroEngine:
        """
        Construct the foundation and defer business workers until module startup.
        """
        cfg = self._config
        snapshot = self._settings_snapshot
        revisions = snapshot.get("CONFIG.module_revisions") or {}
        engine_revision = snapshot.get("CONFIG.engine_revision")
        generation = snapshot.get("CONFIG.generation")
        # 1. EventBus — event_queue must be mp.Queue for cross-process delivery
        event_queue = mp.Queue()
        event_bus = EventBus(event_queue)

        # 2. Shared data buffer
        data_buffer = mp.Queue(cfg.queue_size)
        processing_active = mp.Event()
        data_manager = DataManager(buffer=data_buffer, processing_active=processing_active)
        # Trial, epoch and source sequence counters outlive individual workers.
        runtime_counters = mp.Array("q", (0, 0, -1))
        preview_queue = PreviewChannel()
        preview_cache = PreviewCache(preview_queue, generation=generation)
        acquisition_log_queue = mp.Queue(maxsize=256)
        acquisition_logs = AcquisitionLogListener(acquisition_log_queue)

        # 3. Command queues
        acquisition_command_queue = mp.Queue(cfg.queue_size)
        processor_command_queue = mp.Queue(cfg.queue_size)

        # Workers are constructed only when their module starts. Shared result
        # counters keep trial/epoch identities unique across processing restarts.
        def worker_factory(module, runtime_snapshot):
            runtime_snapshot = module_runtime_settings(runtime_snapshot, module)
            expected_execution = None
            if module == "acquisition":
                runtime_snapshot["CONFIG.operation_id"] = uuid4().hex
                expected_execution = execution_receipt(runtime_snapshot, module, runtime_snapshot["CONFIG.operation_id"])
            receiver, sender = mp.Pipe(duplex=False)
            component = {"acquisition": "Acquisition", "processing": "Signal Processing"}[module]
            reporter = StartupReporter(
                sender, component=component,
                module_revisions={module: runtime_snapshot.get("CONFIG.module_revisions", {}).get(module)},
                engine_revision=runtime_snapshot.get("CONFIG.engine_revision"),
                generation=runtime_snapshot.get("CONFIG.generation"),
            )
            try:
                if module == "acquisition":
                    worker = AcquisitionWorker(
                        command_queue=acquisition_command_queue, data_manager=data_manager,
                        event_bus=event_bus, settings_snapshot=runtime_snapshot,
                        startup_reporter=reporter, preview_queue=preview_queue,
                        log_queue=acquisition_log_queue, processing_active=processing_active,
                        runtime_counters=runtime_counters,
                    )
                else:
                    worker = SignalProcessingWorker(
                        command_queue=processor_command_queue, data_manager=data_manager,
                        event_bus=event_bus, settings_snapshot=runtime_snapshot,
                        baseline_duration=float(runtime_snapshot.get(
                            "ALGORITHM.baseline_duration",
                            MODULE_SETTING_DEFAULTS["processing"]["ALGORITHM.baseline_duration"],
                        )),
                        startup_reporter=reporter, runtime_counters=runtime_counters,
                        execution_history_root=self._execution_history_root,
                    )
                return StartupChannel(component, worker, receiver, reporter, expected_execution)
            except BaseException:
                receiver.close()
                reporter.close()
                raise

        # Output transports are prepared per module after the foundation starts.
        peer_gateway = PeerGateway(event_bus)
        actuation_translator = ActuationCommandTranslator(event_bus)
        actuation_feedback = ActuationFeedbackHandler()

        # 6. Assemble engine
        return NeuroEngine(
            event_bus=event_bus,
            data_manager=data_manager,
            record_execution=self._record_execution,
            acquisition_worker=None,
            acquisition_command_queue=acquisition_command_queue,
            signal_processing_worker=None,
            processor_command_queue=processor_command_queue,
            peer_gateway=peer_gateway,
            owned_ipc_queues=(event_queue, data_buffer, preview_queue, acquisition_log_queue),
            preview_cache=preview_cache,
            acquisition_logs=acquisition_logs,
            actuation_translator=actuation_translator,
            actuation_feedback=actuation_feedback,
            debug=cfg.debug,
            worker_factory=worker_factory,
            processing_active=processing_active,
            startup_timeout_s=cfg.startup_timeout_s,
            module_revisions=revisions, engine_revision=engine_revision,
            generation=generation,
            allow_process_termination=self._allow_process_termination,
        )
