#!/usr/bin/env python
# -*- coding: utf-8 -*-
# @Author   : lincker <haolin.d.wu@gmail.com>
# @Date     : 2026-01-04 09:59
from typing import Dict, Type, Any
import logging

class Registerable:
    """
    通用自注册 Mixin。
    它不直接包含注册表，而是依赖继承链中的 `_target_registry` 属性。
    """
    # 子类必须定义这两个字段
    plugin_type: str = None        # 插件在配置文件中的唯一 ID (例如 "cisco", "bfs")
    _target_registry: Dict = None  # 指向要存入的特定字典 (由 ReaderBase 等基类定义)

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)

        # 1. 如果是基类本身（没有定义 plugin_type），直接跳过
        if cls.plugin_type is None:
            return

        # 2. 检查基类是否配置了注册表存储位置
        if cls._target_registry is None:
            raise NotImplementedError(f"Class {cls.__name__} must define '_target_registry' via its Base class.")

        # 3. 检查 ID 冲突
        # 处理重复注册 ---
        if cls.plugin_type in cls._target_registry:
            existing_cls = cls._target_registry[cls.plugin_type]

            # 1. 如果是完全同一个对象 (内存地址相同)，直接忽略，无需重复注册
            if existing_cls is cls:
                return

            # 2. 如果类名相同 (通常是测试环境下重新加载导致的)，允许覆盖
            if existing_cls.__name__ == cls.__name__:
                # print(f"[Registry] Warning: Overwriting ID '{cls.plugin_type}' with new version of {cls.__name__}")
                pass
            else:
                # 3. 如果 ID 相同但类名不同，那才是真正的冲突，必须报错
                raise ValueError(
                    f"Duplicate ID '{cls.plugin_type}'. Conflict: {existing_cls.__name__} vs {cls.__name__}")
        # 4. 注册到指定的字典中
        logging.info(f"[Register] {cls.plugin_type} -> {cls.__name__} (Registry size: {len(cls._target_registry) + 1})")
        cls._target_registry[cls.plugin_type] = cls