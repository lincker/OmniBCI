#!/usr/bin/env python
# -*- coding: utf-8 -*-
# @Author   : lincker <haolin.d.wu@gmail.com>
# @Date     : 2025-04-28 10:39
"""
Host settings and in-memory worker snapshots.

Host settings come from settings.toml. Plugin Profiles and accepted activations
are managed separately by the application services. Spawned workers install a
resolved snapshot before loading plugins; their reads never fall back to disk.
"""
import logging
from copy import deepcopy
from pathlib import Path

import tomlkit


class OmniBCISettings:
    def __init__(self, toml_file: str | Path = 'settings.toml'):
        self._config_path = Path(toml_file)
        self._data = tomlkit.document()
        self._runtime_snapshot: dict | None = None
        self.load_config()

    def load_config(self):
        """Load host settings without reading retired application profiles."""
        if not self._config_path.exists():
            raise FileNotFoundError(f"Settings file does not exist: {self._config_path}")
        with open(self._config_path, "r", encoding="utf-8") as f:
            self._data = tomlkit.load(f)

    @staticmethod
    def _raw_get(doc, key: str):
        value = doc
        try:
            for k in key.split('.'):
                value = value[k]
        except (KeyError, TypeError):
            return None
        return value.unwrap() if hasattr(value, 'unwrap') else value

    # Distinguish an omitted fallback from an explicit None.
    _MISSING = object()

    def get(self, key: str, default=_MISSING):
        """Read the worker snapshot or host settings using a dotted key."""
        if self._runtime_snapshot is not None:
            if key in self._runtime_snapshot:
                return deepcopy(self._runtime_snapshot[key])
            if default is not self._MISSING:
                return default
            raise NameError(f'settings key not found: {key}')
        value = self._raw_get(self._data, key)
        if value is not None:
            return value
        if default is not self._MISSING:
            return default
        logging.error(f'settings can not find key: {key}')
        raise NameError(f'settings key not found: {key}')

    def set(self, key: str, value):
        """Update a host setting in memory using a dotted key."""
        keys = key.split('.')
        data_ref = self._data
        for k in keys[:-1]:
            if k not in data_ref:
                data_ref[k] = tomlkit.table()
            data_ref = data_ref[k]
        data_ref[keys[-1]] = value

    def save(self):
        """Persist host settings, preserving TOML formatting and comments."""
        self._config_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self._config_path, "w", encoding="utf-8") as f:
            tomlkit.dump(self._data, f)

    @classmethod
    def _flatten(cls, doc, prefix: str = "") -> dict:
        """Flatten TOML while excluding retired application profile metadata."""
        flat = {}
        for k, v in doc.items():
            if not prefix and k in {"META", "PROFILES"}:
                continue
            dotted = f"{prefix}{k}"
            if hasattr(v, "items") and not isinstance(v, (str, list)):
                flat.update(cls._flatten(v, prefix=f"{dotted}."))
            else:
                flat[dotted] = v.unwrap() if hasattr(v, "unwrap") else v
        return flat

    def snapshot(self) -> dict:
        """Copy host settings or the installed worker configuration."""
        if self._runtime_snapshot is not None:
            return deepcopy(self._runtime_snapshot)
        return deepcopy(self._flatten(self._data))

    def use_snapshot(self, snapshot: dict) -> None:
        """Install resolved worker settings without changing any saved files.

        Existing plugin imports retain their reference to this settings object.
        """
        self._runtime_snapshot = deepcopy(snapshot)


settings = OmniBCISettings(
    toml_file=Path(__file__).parent.joinpath('settings.toml')
)
