#!/usr/bin/env python
# -*- coding: utf-8 -*-
# @Author   : lincker <haolin.d.wu@gmail.com>
# @Date     : 2026-01-04 09:25

from core.base.stimulation import StimulusBase
from omnibci.conf import settings
from omnibci.core.base.acquisition import READER_REGISTRY, TRIGGER_REGISTRY, ReaderBase
from omnibci.core.base.algorithm import ALGORITHM_REGISTRY, EPOCH_STRATEGY_REGISTRY, AlgorithmBase
from omnibci.core.base.stimulation import STIMULUS_REGISTRY

from pathlib import Path
import importlib, sys
import logging


def load_plugins(plugin_root: str):
    # 只要递归 import 所有的 .py 文件，注册就会自动发生
    root_path = Path(plugin_root).resolve()
    if str(root_path) not in sys.path:
        logging.warning(f"System Adding {root_path} to sys.path")
        sys.path.append(str(root_path))

    for file_path in root_path.rglob("*.py"):
        # 1. 计算相对路径
        try:
            relative_path = file_path.relative_to(root_path)
        except ValueError:
            continue
        # 2. 特殊处理 __init__.py
        if file_path.name == "__init__.py":
            # 情况 A: plugins/cisco/__init__.py
            # 我们希望模块名是 "cisco"，而不是 "cisco.__init__"
            # relative_path.parent.parts 得到 ('cisco',)
            parts = relative_path.parent.parts
        else:
            # 情况 B: plugins/cisco/router.py
            # 我们希望模块名是 "cisco.router"
            parts = relative_path.with_suffix("").parts
        # 如果 parts 为空（例如加载的是 plugin_root 根目录下的 __init__.py），通常跳过
        if not parts:
            continue

        module_name = ".".join(parts)
        logging.info(f"Loading: {file_path} as module '{module_name}'")
        try:
            _ = importlib.import_module(module_name)
            logging.info(f"Loaded module {module_name}")
        except Exception as e:
            logging.error(f"Error loading {file_path}: {e}")

# def load_plugins(plugin_root: str):
#     # 只要递归 import 所有的 .py 文件，注册就会自动发生
#     root_path = Path(plugin_root).resolve()
#     if str(root_path) not in sys.path:
#         logging.warning(f"System Adding {root_path} to sys.path")
#         sys.path.append(str(root_path))
#
#     for file_path in root_path.rglob("*.py"):
#         if file_path.name == "__init__.py": continue
#
#         module_name = ".".join(file_path.relative_to(root_path).with_suffix("").parts)
#         spec = importlib.util.spec_from_file_location(module_name, file_path)
#         if spec and spec.loader:
#             try:
#                 module = importlib.util.module_from_spec(spec)
#                 spec.loader.exec_module(module)
#             except Exception as e:
#                 print(f"Error loading {file_path}: {e}")

class ReaderFactory:
    def __init__(self):
        pass
    @staticmethod
    def get_reader() -> ReaderBase | None:
        reader = None
        reader_type = settings.get('ACQUISITION.reader_type')
        if reader_type in READER_REGISTRY:
            reader = READER_REGISTRY[reader_type]()
        else:
            from logging import error
            error(f'Unknown reader type: {reader_type}')
        return reader


class TriggerFactory:
    def __init__(self):
        pass
    @staticmethod
    def get_trigger():
        trigger = None
        trigger_type = settings.get('ACQUISITION.trigger_type')
        if trigger_type in TRIGGER_REGISTRY:
            trigger = TRIGGER_REGISTRY[trigger_type]()
        else:
            from logging import error
            error(f'Unknown trigger type: {trigger_type}')
        return trigger


class AlgorithmFactory:
    def __init__(self):
        pass
    @staticmethod
    def get_algorithm() -> AlgorithmBase | None:
        algorithm = None
        algorithm_type = settings.get('ALGORITHM.algorithm')
        if algorithm_type in ALGORITHM_REGISTRY:
            algorithm = ALGORITHM_REGISTRY[algorithm_type]()
        else:
            logging.error(f'Unknown algorithm type: {algorithm_type}')
        return algorithm


class StimulusFactory:
    def __init__(self):
        pass
    @staticmethod
    def get_stimulus() -> StimulusBase:
        stimulus = None
        stimulus_type = settings.get('STIMULATION.stimulus')
        if stimulus_type in ALGORITHM_REGISTRY:
            stimulus = STIMULUS_REGISTRY[stimulus_type]()
        else:
            logging.error(f'Unknown stimulus type: {stimulus_type}')
        return stimulus


class EpochStrategyFactory:
    def __init__(self):
        pass
    @staticmethod
    def get_strategy():
        strategy = None
        strategy_type = settings.get('ALGORITHM.strategy')
        if strategy_type in EPOCH_STRATEGY_REGISTRY:
            strategy = EPOCH_STRATEGY_REGISTRY[strategy_type]()
        else:
            logging.error(f'Unknown strategy type: {strategy_type}')
        return strategy