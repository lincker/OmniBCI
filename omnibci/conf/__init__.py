#!/usr/bin/env python
# -*- coding: utf-8 -*-
# @Author   : lincker <haolin.d.wu@gmail.com>
# @Date     : 2025-04-28 10:39
# import os
import logging
from pathlib import Path
from types import SimpleNamespace

import tomlkit

class OmniBCISettings:
    def __init__(self, toml_file: str | Path = 'settings.toml'):
        self._config_path = Path(toml_file)
        self._data = tomlkit.document()
        self.load_config()

    def load_config(self):
        """动态加载配置"""
        if not self._config_path.exists():
            raise FileNotFoundError(f"配置文件 {self._config_path} 不存在")
        else:
            with open(self._config_path, "r", encoding="utf-8") as f:
                self._data = tomlkit.load(f)

    def get(self, key: str, default=None):
        """获取配置值，支持点分隔符 'section.key'"""
        keys = key.split('.')
        value = self._data

        try:
            for k in keys:
                value = value[k]
            # tomlkit 的值可能是特殊类型，尝试转换为原生 Python 类型
            return value.unwrap() if hasattr(value, 'unwrap') else value
        except (KeyError, TypeError):
            logging.error(f'settings can not find key: {key}')
            raise NameError
            return default

    def set(self, key: str, value):
        """设置配置值，支持点分隔符"""
        keys = key.split('.')
        data_ref = self._data

        for k in keys[:-1]:
            if k not in data_ref:
                # 使用 tomlkit.table() 创建新的节
                data_ref[k] = tomlkit.table()
            data_ref = data_ref[k]

        data_ref[keys[-1]] = value

    def save(self):
        """保存配置到文件，保留所有格式和注释"""
        self._config_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self._config_path, "w", encoding="utf-8") as f:
            tomlkit.dump(self._data, f)

settings = OmniBCISettings(
    toml_file=Path(__file__).parent.joinpath('settings.toml')
)

