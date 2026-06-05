"""
NeuroEngineBuilder — 组装工厂。

职责：创建所有 multiprocessing 基础设施（Queues、Processes、DataManager、
EventBus），然后将它们注入 NeuroEngine 并返回完整可用的实例。

NeuroEngine 自身不知道如何构造任何依赖；builder 是唯一持有这些
"配方"的地方。需要不同配置（不同 queue_size、不同进程实现）时，
只需修改或替换 builder，NeuroEngine 和 NeuroEngineService 无需改动。
"""
from __future__ import annotations

import multiprocessing as mp
from dataclasses import dataclass, field

from omnibci.infrastructure.acquisition.worker import AcquisitionWorker
from omnibci.infrastructure.ipc.data_manager import DataManager
from omnibci.infrastructure.ipc.event_bus import EventBus
from omnibci.infrastructure.engine.engine import NeuroEngine
from omnibci.infrastructure.engine.processor.processor import SignalProcessingWorker
from omnibci.infrastructure.engine.server.management import ServerMessageManagement


@dataclass
class NeuroEngineConfig:
    """
    Configuration knobs for NeuroEngineBuilder — the single place that holds
    the engine's tunables.  Defaults mirror dataclass values; production reads
    them from settings via :meth:`from_settings`, so deep modules no longer
    call ``settings.get`` themselves.
    """
    queue_size: int = 20                    # max items in each mp.Queue
    message_server_ip: str = "127.0.0.1"    # result-push server bind host
    message_server_port: int = 8000         # result-push server bind port
    sample_rate: int = 1000                 # Hz, passed to the SignalProcessor
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
          5. ServerMessageManagement  (registers EventBus listeners)
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
        acquisitor_process = AcquisitionWorker(
            command_queue=acquisition_command_queue,
            data_manager=data_manager,
        )
        signal_processor_process = SignalProcessingWorker(
            command_queue=processor_command_queue,
            data_manager=data_manager,
            event_bus=event_bus,
            sample_rate=cfg.sample_rate,
        )

        # 5. Result push server (host/port injected — no settings.get inside)
        server_message_management = ServerMessageManagement(
            event_bus,
            host=cfg.message_server_ip,
            port=cfg.message_server_port,
        )

        # 6. Assemble engine
        return NeuroEngine(
            event_bus=event_bus,
            data_manager=data_manager,
            acquisitor_process=acquisitor_process,
            acquisition_command_queue=acquisition_command_queue,
            signal_processor_process=signal_processor_process,
            processor_command_queue=processor_command_queue,
            server_message_management=server_message_management,
            debug=cfg.debug,
        )
