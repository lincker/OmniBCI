#!/usr/bin/env python
# -*- coding: utf-8 -*-
# @Author   : lincker <haolin.d.wu@gmail.com>
# @Date     : 2025-04-28 10:39
# import os
import tomli
from pathlib import Path

# base_dir = os.path.dirname(os.path.abspath(__file__))

class OmniBCISettings:
    def __init__(self, toml_file: str = 'settings.toml'):
        self._config_file = toml_file
        self._data = {}
        self.load_config()

    def load_config(self):
        """动态加载配置"""
        config_path = Path(self._config_file)

        if not config_path.exists():
            raise FileNotFoundError(f"配置文件 {self._config_file} 不存在")

        with open(config_path, "rb") as f:
            self._data = tomli.load(f)

        # 将配置转换为属性
        for section, values in self._data.items():
            if isinstance(values, dict):
                setattr(self, section, type(section, (), values)())
            else:
                setattr(self, section, values)

    def get(self, key: str, default=None):
        """获取配置值，支持点分隔符"""
        keys = key.split('.')
        value = self._data

        for k in keys:
            if isinstance(value, dict) and k in value:
                value = value[k]
            else:
                return default

        return value

    def set(self, key: str, value):
        """设置配置值，支持点分隔符"""
        keys = key.split('.')
        data = self._data

        for k in keys[:-1]:
            if k not in data or not isinstance(data[k], dict):
                data[k] = {}
            data = data[k]

        data[keys[-1]] = value

    def save(self):
        """保存配置到文件"""
        config_path = Path(self._config_file)
        with open(config_path, "wb") as f:
            tomli.dump(self._data, f)

settings = OmniBCISettings(
    toml_file=Path(__file__).parent.joinpath('settings.toml')
)

# settings = Dynaconf(
#     # 环境变量前缀
#     envvar_prefix="OMNIBCI",
#     # 指定配置文件路径
#     settings_files=[
#         os.path.join(base_dir, 'settings.toml'),
#     ],
#     # 启用多环境支持
#     environment=True,
#     # 默认环境（当未指定时使用）
#         # default_env="development",
# #     ROOT_PATH_FOR_DYNACONF=os.path.dirname(os.path.abspath(__file__))
# )

# `envvar_prefix` = export envvars with `export DYNACONF_FOO=bar`.
# `settings_files` = Load these files in the order.
