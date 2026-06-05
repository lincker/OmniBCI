"""
AcquisitionWorker — 基础设施层。

multiprocessing 包装器：在独立子进程中运行 Acquisition 领域服务。
插件加载和 DataSource 构造集中在此，不污染领域层。
"""
import multiprocessing as mp
import os
from typing import Optional

import omnibci
from omnibci.domain.ports.acquisition import DataSourceBase
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
    ) -> None:
        super().__init__()
        self.data_source: DataSourceBase
        self.acquisitor: Optional[Acquisition] = None
        self.command_queue = command_queue
        self.data_manager = data_manager

    def run(self) -> None:
        self._load_data_source()
        self._run()

    def _run(self) -> None:
        self.acquisitor = Acquisition(
            data_source=self.data_source,
            command_queue=self.command_queue,
            data_manager=self.data_manager,
        )
        self.acquisitor.run()

    def _load_data_source(self) -> None:
        # Anchor on the omnibci package root so this works regardless of where
        # this module physically lives (decoupled from __file__ depth).
        plugin_path = os.path.join(next(iter(omnibci.__path__)), 'infrastructure', 'plugins', 'acquisition')
        from omnibci.shared.loader import PluginLoader
        from omnibci.shared.factory import ObjectFactory
        PluginLoader.load_from_dir(plugin_path, base_package='omnibci.infrastructure.plugins.acquisition')
        self.data_source = ObjectFactory.create(
            DataSourceBase, config_key='ACQUISITION.data_source'
        )
