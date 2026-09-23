
import multiprocessing as mp
from copy import deepcopy
from dataclasses import dataclass

from omnibci.infrastructure.acquisition.worker import AcquisitionWorker
from omnibci.infrastructure.ipc.data_manager import DataManager
from omnibci.infrastructure.ipc.event_bus import EventBus
from omnibci.infrastructure.neuroengine.engine import NeuroEngine
from omnibci.infrastructure.neuroengine.processing.worker import SignalProcessingWorker
from omnibci.infrastructure.neuroengine.gateway import PeerGateway
from omnibci.infrastructure.neuroengine.gateway.peers import (
    stimulation_peer_config,
    actuation_peer_config,
)
from omnibci.infrastructure.neuroengine.actuation_translator import ActuationCommandTranslator
from omnibci.infrastructure.neuroengine.actuation_feedback import ActuationFeedbackHandler
from omnibci.infrastructure.process_lifecycle import StartupChannel, StartupReporter


@dataclass
class NeuroEngineConfig:
    """
    Configuration for NeuroEngineBuilder. Defaults mirror dataclass values; reads
    them from settings.
    """
    queue_size: int = 100                    # max items in each mp.Queue
    message_server_ip: str = "127.0.0.1"    # result-push server bind host
    message_server_port: int = 8000         # result-push server bind port
    sample_rate: int = 1000                 # Hz, passed to the SignalProcessor
    baseline_duration: float = 0.2          # seconds of pre-trigger baseline (0 = none)
    actuation_enabled: bool = False          # open an actuation listen port + wiring
    actuation_server_ip: str = "127.0.0.1"  # actuation channel bind host
    actuation_server_port: int = 8001        # actuation channel bind port
    actuation_watchdog_timeout_s: float = 1.0  # silent-link → failsafe (Phase 4)
    debug: bool = False                     # asyncio debug mode for the engine loop
    startup_timeout_s: float = 5.0          # configured worker initialization deadline

    @classmethod
    def from_settings(cls, settings_snapshot: dict | None = None) -> "NeuroEngineConfig":
        """Build config from settings.toml, falling back only for missing values."""
        from omnibci.conf import settings
        source = settings if settings_snapshot is None else settings_snapshot

        def _get(key, default):
            value = source.get(key, default)
            return default if value is None else value

        return cls(
            queue_size=int(_get("NEUROENGINE.queue_size", cls.queue_size)),
            message_server_ip=str(_get("NEUROENGINE.message_server_ip", cls.message_server_ip)),
            message_server_port=int(_get("NEUROENGINE.message_server_port", cls.message_server_port)),
            sample_rate=int(_get("ACQUISITION.sample_rate", cls.sample_rate)),
            baseline_duration=float(_get("ALGORITHM.baseline_duration", cls.baseline_duration)),
            actuation_enabled=bool(_get("ACTUATION.enabled", cls.actuation_enabled)),
            actuation_server_ip=str(_get("ACTUATION.server_ip", cls.actuation_server_ip)),
            actuation_server_port=int(_get("ACTUATION.server_port", cls.actuation_server_port)),
            actuation_watchdog_timeout_s=float(
                _get("ACTUATION.watchdog_timeout_s", cls.actuation_watchdog_timeout_s)),
            debug=bool(_get("debug", cls.debug)),
            startup_timeout_s=float(_get("NEUROENGINE.startup_timeout_s", cls.startup_timeout_s)),
        )


class NeuroEngineBuilder:
    """
    Assembles a NeuroEngine from its constituent parts.

    Usage::

        engine = NeuroEngineBuilder().build()                       # config from settings
        engine = NeuroEngineBuilder(NeuroEngineConfig(queue_size=50)).build()  # explicit
    """

    def __init__(self, config: NeuroEngineConfig | None = None,
                 *, settings_snapshot: dict | None = None) -> None:
        # Default: read every tunable from settings ONCE, here.
        self._settings_snapshot = deepcopy(settings_snapshot)
        self._config = config or NeuroEngineConfig.from_settings(self._settings_snapshot)

    def build(self) -> NeuroEngine:
        """
        Construct and wire all components; return a ready-to-run NeuroEngine.

        Build order matters:
          1. EventBus  (needs mp.Queue before use)
          2. DataManager  (shared buffer between acquisition and processor)
          3. Command queues  (one per process)
          4. Sub-processes  (receive references to shared queues/managers)
          5. PeerGateway  (one routed channel per external peer; stimulation here)
          6. NeuroEngine  (receives everything above)
        """
        cfg = self._config

        # 1. EventBus — event_queue must be mp.Queue for cross-process delivery
        event_queue = mp.Queue()
        event_bus = EventBus(event_queue)

        # 2. Shared data buffer
        data_buffer = mp.Queue()
        data_manager = DataManager(buffer=data_buffer)

        # 3. Command queues
        acquisition_command_queue = mp.Queue(cfg.queue_size)
        processor_command_queue = mp.Queue(cfg.queue_size)

        # 4. Sub-processes (not started yet — NeuroEngine._start() does that)
        snapshot = self._settings_snapshot or {}
        revision = snapshot.get("CONFIG.revision")
        generation = snapshot.get("CONFIG.generation")
        acquisition_receiver, acquisition_sender = mp.Pipe(duplex=False)
        processor_receiver, processor_sender = mp.Pipe(duplex=False)
        acquisition_reporter = StartupReporter(
            acquisition_sender, component="Acquisition",
            config_revision=revision, generation=generation,
        )
        processor_reporter = StartupReporter(
            processor_sender, component="Processing",
            config_revision=revision, generation=generation,
        )
        acquisition_worker = AcquisitionWorker(
            command_queue=acquisition_command_queue,
            data_manager=data_manager,
            event_bus=event_bus,
            settings_snapshot=self._settings_snapshot,
            startup_reporter=acquisition_reporter,
        )
        signal_processing_worker = SignalProcessingWorker(
            command_queue=processor_command_queue,
            data_manager=data_manager,
            event_bus=event_bus,
            sample_rate=cfg.sample_rate,
            baseline_duration=cfg.baseline_duration,
            settings_snapshot=self._settings_snapshot,
            startup_reporter=processor_reporter,
        )

        # 5. Peer gateway — one routed channel per external peer (host/port
        #    injected, no settings.get inside). Stimulation is the only peer for
        #    now; actuation is added here later (Phase 3).
        peer_gateway = PeerGateway(event_bus)
        peer_gateway.add_peer(
            stimulation_peer_config(
                host=cfg.message_server_ip,
                port=cfg.message_server_port,
            )
        )

        # Actuation peer (opt-in). When enabled, the engine listens on a second
        # port for the controller to dial in, and we wire the ACL translator
        # (decoded trial → ActuationCommand) + inbound feedback consumer.
        actuation_translator = None
        actuation_feedback = None
        if cfg.actuation_enabled:
            peer_gateway.add_peer(
                actuation_peer_config(
                    host=cfg.actuation_server_ip,
                    port=cfg.actuation_server_port,
                    watchdog_timeout_s=cfg.actuation_watchdog_timeout_s,
                )
            )
            actuation_translator = ActuationCommandTranslator(event_bus)
            actuation_feedback = ActuationFeedbackHandler()

        # 6. Assemble engine
        return NeuroEngine(
            event_bus=event_bus,
            data_manager=data_manager,
            acquisition_worker=acquisition_worker,
            acquisition_command_queue=acquisition_command_queue,
            signal_processing_worker=signal_processing_worker,
            processor_command_queue=processor_command_queue,
            peer_gateway=peer_gateway,
            owned_ipc_queues=(event_queue, data_buffer),
            actuation_translator=actuation_translator,
            actuation_feedback=actuation_feedback,
            debug=cfg.debug,
            startup_channels=(
                StartupChannel("Acquisition", acquisition_worker, acquisition_receiver, acquisition_reporter),
                StartupChannel("Processing", signal_processing_worker, processor_receiver, processor_reporter),
            ),
            startup_timeout_s=cfg.startup_timeout_s,
            config_revision=revision,
            generation=generation,
        )
