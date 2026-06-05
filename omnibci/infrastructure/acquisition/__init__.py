"""
Acquisition 基础设施子模块。

  - acquisition.py  Acquisition 领域服务 + AcquisitionCommand/AcquisitionState
                    + CommandQueue/DataSink 端口契约
  - worker.py       AcquisitionWorker（multiprocessing 包装，加载 DataSource 插件）
  - service.py      向后兼容垫片（旧 import 路径），勿在此新增代码
"""
