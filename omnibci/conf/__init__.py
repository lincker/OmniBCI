#!/usr/bin/env python
# -*- coding: utf-8 -*-
# @Author   : lincker <haolin.d.wu@gmail.com>
# @Date     : 2025-04-28 10:39
"""Host defaults, user overrides, and isolated in-memory worker snapshots."""

import hashlib
import re
from copy import deepcopy
from pathlib import Path
from threading import RLock

import tomlkit

from omnibci.application.activation_settings import BACKEND_STARTUP_DEFAULTS, RUNTIME_HOST_SETTING_KEYS
from omnibci.application.config_errors import ConfigProfileConflictError, ConfigProfileValidationError
from omnibci.conf.paths import config_root
from omnibci.conf.storage import atomic_write, file_lock


class OmniBCISettings:
    def __init__(self, toml_file: str | Path | None = None, *, defaults_file: str | Path | None = None):
        if toml_file is not None and defaults_file is not None:
            raise ValueError("Use either an explicit settings file or defaults with user overrides")
        self._explicit_path = Path(toml_file) if toml_file is not None else None
        self._defaults_path = Path(defaults_file) if defaults_file is not None else Path(__file__).with_name("settings.toml")
        self._data: dict = {}
        self._pending: dict = {}
        self._revision: str | None = None
        self._loaded_path: Path | None = None
        self._runtime_snapshot: dict | None = None
        self._lock = RLock()
        # Importing the global adapter must not read user files in a spawned worker.
        if self._explicit_path is not None:
            self.load_config()

    @property
    def _config_path(self) -> Path:
        return self._explicit_path if self._explicit_path is not None else config_root() / "settings.toml"

    @staticmethod
    def _parse(content: bytes, path: Path):
        try:
            return tomlkit.parse(content.decode("utf-8"))
        except (ValueError, TypeError) as exc:
            raise ConfigProfileValidationError(f"Invalid host settings in {path}: {exc}") from exc

    @classmethod
    def _flatten(cls, doc, prefix: str = "") -> dict:
        flat = {}
        for key, value in doc.items():
            if not prefix and key in {"META", "PROFILES"}:
                continue
            dotted = f"{prefix}{key}"
            if dotted == "SIGNAL.setup":
                flat[dotted] = value.unwrap() if hasattr(value, "unwrap") else deepcopy(value)
            elif hasattr(value, "items") and not isinstance(value, (str, list)):
                flat.update(cls._flatten(value, prefix=f"{dotted}."))
            else:
                flat[dotted] = value.unwrap() if hasattr(value, "unwrap") else value
        return flat

    @staticmethod
    def _validate_host_keys(values: dict) -> None:
        unknown = set(values) - RUNTIME_HOST_SETTING_KEYS - BACKEND_STARTUP_DEFAULTS.keys()
        if unknown:
            raise ConfigProfileValidationError(
                "User host settings contain unsupported keys; configure plugins through Profiles: "
                + ", ".join(sorted(unknown)))

    @staticmethod
    def _hash(defaults: bytes, override: bytes | None) -> str:
        # Include file presence and defaults so deletion or a defaults update invalidates old edits.
        digest = hashlib.sha256(defaults)
        digest.update(b"\0missing" if override is None else b"\0present\0" + override)
        return digest.hexdigest()

    def _read(self, path: Path):
        if self._explicit_path is None and path.resolve() == self._defaults_path.resolve():
            raise ConfigProfileValidationError("User host settings must not overwrite bundled defaults")
        if path.is_symlink():
            raise ConfigProfileValidationError("Host settings must not be a symlink")
        defaults = b""
        base = {}
        if self._explicit_path is None:
            defaults = self._defaults_path.read_bytes()
            base = self._flatten(self._parse(defaults, self._defaults_path))
        content = path.read_bytes() if path.exists() else None
        if content is None and self._explicit_path is not None:
            raise FileNotFoundError(f"Settings file does not exist: {path}")
        document = self._parse(content, path) if content is not None else tomlkit.document()
        overrides = self._flatten(document)
        if self._explicit_path is None:
            self._validate_host_keys(overrides)
            if {"META", "PROFILES"} & document.keys():
                raise ConfigProfileValidationError("Retired profile metadata is not a host setting")
        return {**base, **overrides}, self._hash(defaults, content), document, defaults

    def _publish(self, values: dict, revision: str, path: Path) -> None:
        self._data = values
        self._revision = revision
        self._loaded_path = path
        self._pending.clear()

    def read_saved(self) -> tuple[dict, str]:
        """Read saved values and their revision together, outside worker loops."""
        with self._lock:
            path = self._config_path
            # An absent directory has no cooperating writer or saved overrides yet.
            if path.parent.exists():
                with file_lock(path.parent / ".host-settings.lock"):
                    values, revision, _, _ = self._read(path)
            else:
                values, revision, _, _ = self._read(path)
            self._publish(values, revision, path)
            return deepcopy(values), revision

    def saved_snapshot(self) -> dict:
        return self.read_saved()[0]

    def load_config(self):
        """Refresh saved host values; never load retired Application Profiles."""
        self.read_saved()

    def _ensure_loaded(self):
        if self._loaded_path != self._config_path:
            self.load_config()

    _MISSING = object()

    def host_get(self, key: str, default=None):
        """Read deployment values even when a worker snapshot is installed."""
        with self._lock:
            self._ensure_loaded()
            return deepcopy(self._pending.get(key, self._data.get(key, default)))

    def get(self, key: str, default=_MISSING):
        """Read only the installed snapshot in workers, otherwise cached host values."""
        if self._runtime_snapshot is not None:
            if key in self._runtime_snapshot:
                return deepcopy(self._runtime_snapshot[key])
            if default is not self._MISSING:
                return default
            raise NameError(f"settings key not found: {key}")
        with self._lock:
            self._ensure_loaded()
            values = {**self._data, **self._pending}
            if key in values:
                return deepcopy(values[key])
            if default is not self._MISSING:
                return default
            raise NameError(f"settings key not found: {key}")

    @staticmethod
    def _set(document, key: str, value) -> None:
        parts = key.split(".")
        target = document
        for part in parts[:-1]:
            if part not in target:
                target[part] = tomlkit.table()
            target = target[part]
        target[parts[-1]] = value

    def update(self, updates: dict, *, expected_revision: str) -> str:
        """Compare, persist atomically, then publish the committed host values."""
        if not isinstance(expected_revision, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_revision):
            raise ConfigProfileValidationError("expected_revision must be a SHA-256 revision")
        if self._explicit_path is None:
            self._validate_host_keys(updates)
        with self._lock:
            path = self._config_path
            with file_lock(path.parent / ".host-settings.lock"):
                values, revision, document, defaults = self._read(path)
                if revision != expected_revision:
                    raise ConfigProfileConflictError("Host settings changed; refresh before saving again")
                for key, value in updates.items():
                    self._set(document, key, value)
                content = tomlkit.dumps(document)
                values.update(deepcopy(updates))
                new_revision = self._hash(defaults, content.encode("utf-8"))
                atomic_write(path, content)
                self._publish(values, new_revision, path)
                return new_revision

    def set(self, key: str, value):
        """Stage an explicit local edit; API callers use the atomic update method."""
        with self._lock:
            self._ensure_loaded()
            if self._explicit_path is None:
                self._validate_host_keys({key: value})
            self._pending[key] = deepcopy(value)

    def save(self):
        """Save staged edits using the revision from the last load."""
        with self._lock:
            self._ensure_loaded()
            return self.update(dict(self._pending), expected_revision=self._revision)

    def snapshot(self) -> dict:
        """Copy cached host settings or the installed worker configuration."""
        if self._runtime_snapshot is not None:
            return deepcopy(self._runtime_snapshot)
        with self._lock:
            self._ensure_loaded()
            return deepcopy({**self._data, **self._pending})

    def use_snapshot(self, snapshot: dict) -> None:
        """Install resolved worker settings without reading or changing saved files."""
        with self._lock:
            self._runtime_snapshot = deepcopy(snapshot)


settings = OmniBCISettings()
