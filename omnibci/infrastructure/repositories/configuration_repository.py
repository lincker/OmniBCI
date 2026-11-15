#!/usr/bin/env python
# -*- coding: utf-8 -*-
# @Author   : lincker <haolin.d.wu@gmail.com>
# @Date     : 2025-04-28 10:39
"""Atomic TOML storage for Server, Engine, module configurations and Profiles."""
from __future__ import annotations

import hashlib
import json
import math
import re
import shutil
from contextlib import ExitStack, contextmanager
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock

import tomlkit

from omnibci.application.configuration.definitions import (
    MODULES, SERVER_STARTUP_DEFAULTS, server_startup_settings, validate_server_settings,
)
from omnibci.application.configuration.models import (
    PLUGIN_KINDS, ActivationSet, ModuleActivation, ModuleActivationRevision,
    PluginProfile, ProfileRevision,
    EngineSettings, validate_profile_id,
)
from omnibci.application.configuration.errors import (
    ConfigKeyNotAllowedError, ConfigValidationError, ConfigProfileValidationError,
    ConfigProfileConflictError, PluginProfileNotFoundError, PluginProfileStorageError,
    PluginProfileValidationError, ProfileRevisionConflict,
)
from omnibci.infrastructure.config_paths import config_root
from omnibci.infrastructure.repositories.configuration_storage import atomic_write, file_lock


class TomlConfigurationRepository:
    def __init__(self, root: Path | None = None, *, lock_timeout_s: float = 5.0):
        self.root = (root if root is not None else config_root()).resolve()
        if (isinstance(lock_timeout_s, bool) or not isinstance(lock_timeout_s, (int, float))
                or not math.isfinite(lock_timeout_s) or lock_timeout_s <= 0):
            raise ValueError("lock_timeout_s must be positive and finite")
        self._lock_timeout_s = lock_timeout_s
        self._thread_locks = {section: RLock() for section in ("settings", "profiles", *MODULES)}

    def _safe(self, path: Path) -> Path:
        if not path.resolve().is_relative_to(self.root):
            raise PluginProfileValidationError("Configuration path escapes the storage directory")
        current = path
        while current != self.root:
            if current.is_symlink():
                raise PluginProfileValidationError("Configuration paths must not contain symlinks")
            current = current.parent
        return path

    @contextmanager
    def _access(self, section: str, *, write: bool = False):
        lock = self._thread_locks[section]
        if not lock.acquire(timeout=self._lock_timeout_s):
            raise PluginProfileStorageError("Timed out waiting for configuration lock")
        try:
            try:
                if not self.root.exists() and not write:
                    yield
                    return
                lock_path = self._safe(self.root / "locks" / f"{section}.lock")
                with file_lock(lock_path, timeout_s=self._lock_timeout_s):
                    yield
            except PluginProfileStorageError:
                raise
            except PluginProfileValidationError as exc:
                if section == "settings":
                    raise ConfigProfileValidationError(str(exc)) from exc
                raise
            except OSError as exc:
                raise PluginProfileStorageError(f"Cannot access configuration: {exc}") from exc
        finally:
            lock.release()

    def _profile_path(self, profile_id: str) -> Path:
        validate_profile_id(profile_id)
        return self._safe(self.root / "plugin-profiles" / profile_id / "current.toml")

    def _section_path(self, section: str) -> Path:
        if not isinstance(section, str) or section not in MODULES:
            raise PluginProfileValidationError("Unsupported configuration section")
        return self._safe(self.root / "activation" / f"{section}.toml")

    @staticmethod
    def _module(module: str) -> None:
        if not isinstance(module, str) or module not in MODULES:
            raise PluginProfileValidationError("Unsupported configuration module")

    def _read(self, path: Path) -> dict:
        self._safe(path)
        try:
            return tomlkit.parse(path.read_text(encoding="utf-8")).unwrap()
        except (ValueError, TypeError) as exc:
            raise PluginProfileValidationError(f"Invalid TOML in {path.name}: {exc}") from exc

    def _read_profile(self, path: Path) -> PluginProfile:
        if not path.exists():
            raise PluginProfileNotFoundError(path.parent.name)
        profile = PluginProfile.from_dict(self._read(path))
        if path.parent.name != profile.profile_id or profile.revision < 1:
            raise PluginProfileValidationError("Stored profile identity or revision is invalid")
        return profile

    def _write(self, path: Path, data: dict) -> None:
        self._safe(path)
        content = tomlkit.dumps(data)
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(path, content)

    def list_profiles(self, kind: str | None = None) -> list[PluginProfile]:
        if kind is not None and (not isinstance(kind, str) or kind not in PLUGIN_KINDS):
            raise PluginProfileValidationError("Unsupported plugin kind")
        with self._access("profiles"):
            directory = self._safe(self.root / "plugin-profiles")
            profiles = []
            for path in directory.glob("*/current.toml"):
                if kind is not None and self._read(path).get("kind") != kind:
                    continue
                profiles.append(self._read_profile(path))
            return sorted(profiles, key=lambda item: (item.name.casefold(), item.profile_id))

    def get_profile(self, profile_id: str) -> PluginProfile:
        path = self._profile_path(profile_id)
        with self._access("profiles"):
            return self._read_profile(path)

    @staticmethod
    def _expected_profile_revision(expected_revision: int | None) -> None:
        if expected_revision is not None and (type(expected_revision) is not int or expected_revision < 1):
            raise PluginProfileValidationError("expected_revision must be a positive integer or None")

    @staticmethod
    def _expected_revision(expected_revision: str | None) -> None:
        if expected_revision is not None and (
            not isinstance(expected_revision, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_revision)
        ):
            raise PluginProfileValidationError("expected_revision must be a SHA-256 revision or None")

    def save_profile(self, profile: PluginProfile, expected_revision: int | None = None) -> PluginProfile:
        self._expected_profile_revision(expected_revision)
        profile = replace(profile)
        path = self._profile_path(profile.profile_id)
        with self._access("profiles", write=True):
            previous = self._read_profile(path) if path.exists() else None
            if (previous.revision if previous else None) != expected_revision:
                raise ProfileRevisionConflict("Profile changed; reload before saving")
            if previous is not None and (
                previous.kind != profile.kind
                or previous.selection.package_id != profile.selection.package_id
                or previous.selection.contribution_id != profile.selection.contribution_id
            ):
                raise PluginProfileValidationError("A profile's owning capability cannot change")
            saved = replace(profile, revision=1 if previous is None else previous.revision + 1)
            history = self._safe(path.parent / "history")
            # A crash before current.toml was replaced may leave uncommitted history.
            for item in history.glob("*.toml"):
                self._safe(item)
                if item.stem.isdigit() and int(item.stem) >= saved.revision:
                    item.unlink()
            history_path = self._safe(history / f"{saved.revision}.toml")
            entry = ProfileRevision(saved.profile_id, saved.revision, datetime.now(timezone.utc).isoformat(), saved)
            self._write(history_path, entry.to_dict())
            try:
                self._write(path, saved.to_dict())
            except BaseException:
                if not path.exists() or self._read_profile(path).revision != saved.revision:
                    history_path.unlink(missing_ok=True)
                raise
            return saved

    def delete_profile(self, profile_id: str, expected_revision: int) -> None:
        self._expected_profile_revision(expected_revision)
        if expected_revision is None:
            raise PluginProfileValidationError("expected_revision is required for deletion")
        path = self._profile_path(profile_id)
        with ExitStack() as stack:
            stack.enter_context(self._access("profiles"))
            for module in MODULES:
                stack.enter_context(self._access(module))
            previous = self._read_profile(path)
            if previous.revision != expected_revision:
                raise ProfileRevisionConflict("Profile changed; reload before deleting")
            for module in MODULES:
                activation = self._read_module(module)
                if activation and any(item.profile_id == profile_id for item in activation.bindings):
                    raise ProfileRevisionConflict("Profile is selected; replace its binding first")
            for item in path.parent.rglob("*"):
                self._safe(item)
            shutil.rmtree(path.parent)

    def list_profile_history(self, profile_id: str) -> list[ProfileRevision]:
        path = self._profile_path(profile_id)
        with self._access("profiles"):
            profile = self._read_profile(path)
            result = []
            for item in self._safe(path.parent / "history").glob("*.toml"):
                entry = ProfileRevision.from_dict(self._read(item))
                if entry.profile_id != profile_id or item.stem != str(entry.revision):
                    raise PluginProfileValidationError("Profile history identity does not match its path")
                if entry.revision <= profile.revision:
                    result.append(entry)
            return sorted(result, key=lambda item: item.revision, reverse=True)

    def get_profile_revision(self, profile_id: str, revision: int) -> ProfileRevision | None:
        self._expected_profile_revision(revision)
        if revision is None:
            raise PluginProfileValidationError("revision is required")
        return next((item for item in self.list_profile_history(profile_id) if item.revision == revision), None)

    def _read_module(self, module: str) -> ModuleActivation | None:
        path = self._section_path(module)
        if not path.exists():
            return None
        activation = ModuleActivation.from_dict(self._read(path))
        if activation.module != module:
            raise PluginProfileValidationError("Activation module does not match its path")
        return activation

    def get_module(self, module: str) -> ModuleActivation | None:
        self._module(module)
        with self._access(module):
            return self._read_module(module)

    def save_module(self, activation: ModuleActivation, expected_revision: str | None = None) -> ModuleActivation:
        self._expected_revision(expected_revision)
        activation = ModuleActivation.from_dict(activation.to_dict())
        with self._access(activation.module, write=True):
            previous = self._read_module(activation.module)
            if (previous.revision if previous else None) != expected_revision:
                raise ProfileRevisionConflict("Module changed; reload before saving")
            self._write(self._section_path(activation.module), activation.to_dict())
            return activation

    def remove_module(self, module: str, *, expected_revision: str) -> None:
        self._module(module)
        self._expected_revision(expected_revision)
        with self._access(module):
            previous = self._read_module(module)
            if previous is None or previous.revision != expected_revision:
                raise ProfileRevisionConflict("Module changed; reload before restoring")
            self._section_path(module).unlink()

    def record_module_history(self, activation: ModuleActivation, entry_id: str,
                              restored_from: str | None = None) -> ModuleActivationRevision:
        entry = ModuleActivationRevision(activation.module, entry_id, datetime.now(timezone.utc).isoformat(),
                                         activation, restored_from)
        path = self._safe(self.root / "activation" / "history" / activation.module / f"{entry_id}.toml")
        with self._access(activation.module, write=True):
            if path.exists():
                previous = ModuleActivationRevision.from_dict(self._read(path))
                if (previous.entry_id != entry_id or previous.module != activation.module
                        or previous.activation.to_dict() != activation.to_dict()
                        or previous.restored_from != restored_from):
                    raise ProfileRevisionConflict("History operation already records different configuration")
                return previous
            self._write(path, entry.to_dict())
            return entry

    def list_module_history(self, module: str) -> list[ModuleActivationRevision]:
        self._module(module)
        directory = self._safe(self.root / "activation" / "history" / module)
        with self._access(module):
            result = []
            for path in directory.glob("*.toml"):
                entry = ModuleActivationRevision.from_dict(self._read(path))
                if entry.module != module or path.stem != entry.entry_id:
                    raise PluginProfileValidationError("Module history identity does not match its path")
                result.append(entry)
            return sorted(result, key=lambda item: (item.saved_at, item.entry_id), reverse=True)

    def get_module_history_entry(self, module: str, entry_id: str) -> ModuleActivationRevision | None:
        validate_profile_id(entry_id)
        return next((item for item in self.list_module_history(module) if item.entry_id == entry_id), None)

    def _settings_document(self):
        path = self.root / "settings.toml"
        if path.is_symlink():
            raise ConfigProfileValidationError("Settings file must not be a symlink")
        self._safe(path)
        if not path.exists():
            return tomlkit.document()
        try:
            return tomlkit.parse(path.read_text(encoding="utf-8"))
        except (ValueError, TypeError) as exc:
            raise ConfigProfileValidationError(f"Invalid settings in {path}: {exc}") from exc

    @staticmethod
    def _server_values(document):
        values = {}
        for section, fields in document.unwrap().items():
            if section == "engine":
                continue
            prefix = f"{section}."
            if (not hasattr(fields, "items")
                    or not any(key.startswith(prefix) for key in SERVER_STARTUP_DEFAULTS)):
                raise ConfigProfileValidationError(f"Unsupported Server settings section: {section}")
            values.update({f"{section}.{key}": value for key, value in fields.items()})
        try:
            return server_startup_settings(validate_server_settings(values, allow_empty=True))
        except (ConfigKeyNotAllowedError, ConfigValidationError) as exc:
            raise ConfigProfileValidationError(f"Invalid Server settings: {exc}") from exc

    @staticmethod
    def _server_revision(values):
        content = json.dumps(values, sort_keys=True, separators=(",", ":"), allow_nan=False)
        return hashlib.sha256(content.encode("utf-8")).hexdigest()

    def read_server(self) -> tuple[dict, str]:
        """Read only Server-owned values; other configuration content is independent."""
        with self._access("settings"):
            values = self._server_values(self._settings_document())
            return values, self._server_revision(values)

    def save_server(self, updates: dict, *, expected_revision: str) -> str:
        """Compare Server values under the shared file lock and preserve Engine settings."""
        self._expected_revision(expected_revision)
        if expected_revision is None:
            raise ConfigProfileValidationError("expected_revision is required for Server settings")
        clean = validate_server_settings(updates)
        with self._access("settings", write=True):
            document = self._settings_document()
            values = self._server_values(document)
            if self._server_revision(values) != expected_revision:
                raise ConfigProfileConflictError("Server settings changed; refresh before saving again")
            for key, value in clean.items():
                section, name = key.split(".", 1)
                if section not in document:
                    document[section] = tomlkit.table()
                document[section][name] = value
            values.update(clean)
            self._write(self.root / "settings.toml", document)
            return self._server_revision(values)

    @staticmethod
    def _engine_value(document) -> EngineSettings | None:
        if "engine" not in document:
            return None
        values = document["engine"]
        try:
            return EngineSettings(values.unwrap() if hasattr(values, "unwrap") else values)
        except PluginProfileValidationError as exc:
            raise ConfigProfileValidationError(f"Invalid Engine settings: {exc}") from exc

    def get_engine(self) -> EngineSettings | None:
        with self._access("settings"):
            return self._engine_value(self._settings_document())

    def save_engine(self, settings: EngineSettings, expected_revision: str | None = None) -> EngineSettings:
        settings = EngineSettings.from_dict(settings.to_dict())
        self._write_engine(settings, expected_revision)
        return settings

    def remove_engine(self, *, expected_revision: str) -> None:
        self._write_engine(None, expected_revision)

    def _write_engine(self, settings: EngineSettings | None, expected_revision: str | None):
        self._expected_revision(expected_revision)
        with self._access("settings", write=True):
            document = self._settings_document()
            previous = self._engine_value(document)
            if ((previous is None and settings is None)
                    or (previous.revision if previous is not None else None) != expected_revision):
                raise ProfileRevisionConflict("Engine settings changed; reload before saving")
            if settings is None:
                del document["engine"]
            else:
                document["engine"] = settings.settings
            self._write(self.root / "settings.toml", document)

    def get_activation_set(self) -> ActivationSet:
        if not self.root.exists():
            return ActivationSet({}, None)
        with ExitStack() as stack:
            for section in MODULES:
                stack.enter_context(self._access(section))
            return ActivationSet({module: self._read_module(module) for module in MODULES}, self.get_engine())
