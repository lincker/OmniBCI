"""
AcquisitionWorker — 基础设施层。

multiprocessing 包装器：在独立子进程中运行 Acquisition 领域服务。
插件加载和 DataSource 构造集中在此，不污染领域层。
"""
import multiprocessing as mp
import os
from typing import Optional

import omnibci
from omnibci.domain.ports.acquisition import SignalSource
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
    ) -> None:
        super().__init__()
        self.data_source: SignalSource
        self.acquisitor: Optional[Acquisition] = None
        self.command_queue = command_queue
        self.data_manager = data_manager
        # EventBus 跨进程时 __getstate__ 剥 handler、只带 mp.Queue —— 子进程
        # 仅能 put_event（与 SignalProcessingWorker 同一机制）。
        self.event_bus = event_bus

    def run(self) -> None:
        self._load_data_source()
        self._run()

    def _run(self) -> None:
        self.acquisitor = Acquisition(
            data_source=self.data_source,
            command_queue=self.command_queue,
            data_manager=self.data_manager,
            event_bus=self.event_bus,
        )
        self.acquisitor.run()

    def _load_data_source(self) -> None:
        # Anchor on the omnibci package root so this works regardless of where
        # this module physically lives (decoupled from __file__ depth).
        plugin_path = os.path.join(next(iter(omnibci.__path__)), 'infrastructure', 'plugins', 'acquisition')
        from omnibci.conf import settings
        from omnibci.infrastructure.plugins.loader import PluginLoader
        from omnibci.infrastructure.plugins.registry import ObjectFactory
        PluginLoader.load_from_dir(plugin_path, base_package='omnibci.infrastructure.plugins.acquisition')
        # 配置读取上移到调用方；工厂只按 plugin_type 查注册表并实例化。
        self.data_source = ObjectFactory.create(
            SignalSource, settings.get('ACQUISITION.signal_source')
        )
