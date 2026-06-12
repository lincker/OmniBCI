
import multiprocessing as mp
from dataclasses import dataclass

from omnibci.infrastructure.acquisition.worker import AcquisitionWorker
from omnibci.infrastructure.ipc.data_manager import DataManager
from omnibci.infrastructure.ipc.event_bus import EventBus
from omnibci.infrastructure.neuroengine.engine import NeuroEngine
from omnibci.infrastructure.neuroengine.processing.worker import SignalProcessingWorker
from omnibci.infrastructure.neuroengine.gateway import PeerGateway
from omnibci.infrastructure.neuroengine.gateway.peers import (
    stimulation_peer_config,
    effector_peer_config,
)
from omnibci.infrastructure.neuroengine.effector_translator import EffectorCommandTranslator
from omnibci.infrastructure.neuroengine.effector_feedback import EffectorFeedbackHandler


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
    effector_enabled: bool = False          # open an effector listen port + wiring
    effector_server_ip: str = "127.0.0.1"  # effector channel bind host
    effector_server_port: int = 8001        # effector channel bind port
    effector_watchdog_timeout_s: float = 1.0  # silent-link → failsafe (Phase 4)
    debug: bool = False                     # asyncio debug mode for the engine loop

    @classmethod
    def from_settings(cls) -> "NeuroEngineConfig":
        """Build config from settings.toml, falling back to dataclass defaults."""
        from omnibci.conf import settings

        def _get(key, default):
            try:
                value = settings.get(key)
            except Exception:
                return default
            return value if value is not None else default

        return cls(
            queue_size=int(_get("NEUROENGINE.queue_size", cls.queue_size)),
            message_server_ip=str(_get("NEUROENGINE.message_server_ip", cls.message_server_ip)),
            message_server_port=int(_get("NEUROENGINE.message_server_port", cls.message_server_port)),
            sample_rate=int(_get("ACQUISITION.sample_rate", cls.sample_rate)),
            baseline_duration=float(_get("ALGORITHM.baseline_duration", cls.baseline_duration)),
            effector_enabled=bool(_get("NEUROENGINE.effector_enabled", cls.effector_enabled)),
            effector_server_ip=str(_get("NEUROENGINE.effector_server_ip", cls.effector_server_ip)),
            effector_server_port=int(_get("NEUROENGINE.effector_server_port", cls.effector_server_port)),
            effector_watchdog_timeout_s=float(
                _get("NEUROENGINE.effector_watchdog_timeout_s", cls.effector_watchdog_timeout_s)),
            debug=bool(_get("debug", cls.debug)),
        )


class NeuroEngineBuilder:
    """
    Assembles a NeuroEngine from its constituent parts.

    Usage::

        engine = NeuroEngineBuilder().build()                       # config from settings
        engine = NeuroEngineBuilder(NeuroEngineConfig(queue_size=50)).build()  # explicit
    """

    def __init__(self, config: NeuroEngineConfig | None = None) -> None:
        # Default: read every tunable from settings ONCE, here.
        self._config = config or NeuroEngineConfig.from_settings()

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
        event_bus = EventBus()
        event_bus.set_event_queue(mp.Queue())

        # 2. Shared data buffer
        data_manager = DataManager(buffer=mp.Queue())

        # 3. Command queues
        acquisition_command_queue = mp.Queue(cfg.queue_size)
        processor_command_queue = mp.Queue(cfg.queue_size)

        # 4. Sub-processes (not started yet — NeuroEngine._start() does that)
        acquisition_worker = AcquisitionWorker(
            command_queue=acquisition_command_queue,
            data_manager=data_manager,
        )
        signal_processing_worker = SignalProcessingWorker(
            command_queue=processor_command_queue,
            data_manager=data_manager,
            event_bus=event_bus,
            sample_rate=cfg.sample_rate,
            baseline_duration=cfg.baseline_duration,
        )

        # 5. Peer gateway — one routed channel per external peer (host/port
        #    injected, no settings.get inside). Stimulation is the only peer for
        #    now; effector is added here later (Phase 3).
        peer_gateway = PeerGateway(event_bus)
        peer_gateway.add_peer(
            stimulation_peer_config(
                host=cfg.message_server_ip,
                port=cfg.message_server_port,
            )
        )

        # Effector peer (opt-in). When enabled, the engine listens on a second
        # port for the controller to dial in, and we wire the ACL translator
        # (decoded trial → EffectorCommand) + inbound feedback consumer.
        effector_translator = None
        effector_feedback = None
        if cfg.effector_enabled:
            peer_gateway.add_peer(
                effector_peer_config(
                    host=cfg.effector_server_ip,
                    port=cfg.effector_server_port,
                    watchdog_timeout_s=cfg.effector_watchdog_timeout_s,
                )
            )
            effector_translator = EffectorCommandTranslator(event_bus)
            effector_feedback = EffectorFeedbackHandler()

        # 6. Assemble engine
        return NeuroEngine(
            event_bus=event_bus,
            data_manager=data_manager,
            acquisition_worker=acquisition_worker,
            acquisition_command_queue=acquisition_command_queue,
            signal_processing_worker=signal_processing_worker,
            processor_command_queue=processor_command_queue,
            peer_gateway=peer_gateway,
            effector_translator=effector_translator,
            effector_feedback=effector_feedback,
            debug=cfg.debug,
        )
