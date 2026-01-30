# #!/usr/bin/env python
# # -*- coding: utf-8 -*-
# # @Author   : lincker <haolin.d.wu@gmail.com>
# # @Date     : 2026-01-04 09:25
#
# from interfaces.paradigm import ParadigmBase
# from omnibci.conf import settings
# from interfaces.acquisition import READER_REGISTRY, TRIGGER_REGISTRY, DataSourceBase
# from interfaces.algorithm import ALGORITHM_REGISTRY, EPOCH_STRATEGY_REGISTRY, DecoderBase
# from interfaces.paradigm import STIMULUS_REGISTRY
#
# from pathlib import Path
# import importlib, sys
# import logging
#
#
# def load_plugins(plugin_root: str):
#     # 只要递归 import 所有的 .py 文件，注册就会自动发生
#     root_path = Path(plugin_root).resolve()
#     if str(root_path) not in sys.path:
#         logging.warning(f"System Adding {root_path} to sys.path")
#         sys.path.append(str(root_path))
#
#     for file_path in root_path.rglob("*.py"):
#         # 1. 计算相对路径
#         try:
#             relative_path = file_path.relative_to(root_path)
#         except ValueError:
#             continue
#         # 2. 特殊处理 __init__.py
#         if file_path.name == "__init__.py":
#             # 情况 A: plugins/cisco/__init__.py
#             # 我们希望模块名是 "cisco"，而不是 "cisco.__init__"
#             # relative_path.parent.parts 得到 ('cisco',)
#             parts = relative_path.parent.parts
#         else:
#             # 情况 B: plugins/cisco/router.py
#             # 我们希望模块名是 "cisco.router"
#             parts = relative_path.with_suffix("").parts
#         # 如果 parts 为空（例如加载的是 plugin_root 根目录下的 __init__.py），通常跳过
#         if not parts:
#             continue
#
#         module_name = ".".join(parts)
#         logging.info(f"Loading: {file_path} as module '{module_name}'")
#         try:
#             _ = importlib.import_module(module_name)
#             logging.info(f"Loaded module {module_name}")
#         except Exception as e:
#             logging.error(f"Error loading {file_path}: {e}")
#
# # def load_plugins(plugin_root: str):
# #     # 只要递归 import 所有的 .py 文件，注册就会自动发生
# #     root_path = Path(plugin_root).resolve()
# #     if str(root_path) not in sys.path:
# #         logging.warning(f"System Adding {root_path} to sys.path")
# #         sys.path.append(str(root_path))
# #
# #     for file_path in root_path.rglob("*.py"):
# #         if file_path.name == "__init__.py": continue
# #
# #         module_name = ".".join(file_path.relative_to(root_path).with_suffix("").parts)
# #         spec = importlib.util.spec_from_file_location(module_name, file_path)
# #         if spec and spec.loader:
# #             try:
# #                 module = importlib.util.module_from_spec(spec)
# #                 spec.loader.exec_module(module)
# #             except Exception as e:
# #                 print(f"Error loading {file_path}: {e}")
#
# class ReaderFactory:
#     def __init__(self):
#         pass
#     @staticmethod
#     def get_reader() -> DataSourceBase | None:
#         data_source = None
#         data_source = settings.get('ACQUISITION.data_source')
#         if data_source in READER_REGISTRY:
#             data_source = READER_REGISTRY[data_source]()
#         else:
#             from logging import error
#             error(f'Unknown data_source type: {data_source}')
#         return data_source
#
#
# class TriggerFactory:
#     def __init__(self):
#         pass
#     @staticmethod
#     def get_trigger():
#         trigger = None
#         trigger = settings.get('ACQUISITION.trigger')
#         if trigger in TRIGGER_REGISTRY:
#             trigger = TRIGGER_REGISTRY[trigger]()
#         else:
#             from logging import error
#             error(f'Unknown trigger type: {trigger}')
#         return trigger
#
#
# class AlgorithmFactory:
#     def __init__(self):
#         pass
#     @staticmethod
#     def get_algorithm() -> DecoderBase | None:
#         algorithm = None
#         algorithm_type = settings.get('ALGORITHM.algorithm')
#         if algorithm_type in ALGORITHM_REGISTRY:
#             algorithm = ALGORITHM_REGISTRY[algorithm_type]()
#         else:
#             logging.error(f'Unknown algorithm type: {algorithm_type}')
#         return algorithm
#
#
# class StimulusFactory:
#     def __init__(self):
#         pass
#     @staticmethod
#     def get_stimulus() -> ParadigmBase:
#         paradigm = None
#         stimulus_type = settings.get('STIMULATION.paradigm')
#         if stimulus_type in STIMULUS_REGISTRY:
#             paradigm = STIMULUS_REGISTRY[stimulus_type]()
#         else:
#             logging.error(f'Unknown paradigm type: {stimulus_type}')
#         return paradigm
#
#
# class EpochStrategyFactory:
#     def __init__(self):
#         pass
#     @staticmethod
#     def get_strategy():
#         segmenter = None
#         strategy_type = settings.get('ALGORITHM.segmenter')
#         if strategy_type in EPOCH_STRATEGY_REGISTRY:
#             segmenter = EPOCH_STRATEGY_REGISTRY[strategy_type]()
#         else:
#             logging.error(f'Unknown segmenter type: {strategy_type}')
#         return segmenter