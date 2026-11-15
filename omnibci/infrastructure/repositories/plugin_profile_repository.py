"""Atomic, compare-and-swap TOML storage for plugin profiles and activation."""
from __future__ import annotations

import errno
import math
import os
import re
import shutil
import tempfile
import time
from contextlib import ExitStack, contextmanager
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock

import tomlkit

from omnibci.application.activation_settings import MODULES, SECTIONS
from omnibci.application.plugin_profiles import (
    PLUGIN_KINDS, ActivationSet, ModuleActivation, ModuleActivationRevision,
    PluginProfile, PluginProfileNotFoundError, PluginProfileStorageError,
    PluginProfileValidationError, ProfileRevision, ProfileRevisionConflict,
    SystemSettings, validate_profile_id,
)
from omnibci.infrastructure.config_paths import config_root


class TomlPluginProfileRepository:
    def __init__(self, root: Path | None = None, *, lock_timeout_s: float = 5.0):
        self.root = (root if root is not None else config_root()).resolve()
        if (isinstance(lock_timeout_s, bool) or not isinstance(lock_timeout_s, (int, float))
                or not math.isfinite(lock_timeout_s) or lock_timeout_s <= 0):
            raise ValueError("lock_timeout_s must be positive and finite")
        self._lock_timeout_s = lock_timeout_s
        self._thread_locks = {section: RLock() for section in ("profiles", *SECTIONS)}

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
                directory = self._safe(self.root / "locks")
                directory.mkdir(parents=True, exist_ok=True)
                lock_path = self._safe(directory / f"{section}.lock")
                with lock_path.open("a+b") as stream:
                    if os.name == "nt":
                        import msvcrt
                        stream.seek(0, os.SEEK_END)
                        if stream.tell() == 0:
                            stream.write(b"\0")
                            stream.flush()

                        def acquire():
                            stream.seek(0)
                            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)

                        def release():
                            stream.seek(0)
                            msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        import fcntl

                        def acquire():
                            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

                        def release():
                            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)

                    deadline = time.monotonic() + self._lock_timeout_s
                    while True:
                        try:
                            acquire()
                            break
                        except OSError as exc:
                            if exc.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                                raise
                            if time.monotonic() >= deadline:
                                raise PluginProfileStorageError("Timed out waiting for configuration lock") from exc
                            time.sleep(0.01)
                    try:
                        yield
                    finally:
                        release()
            except PluginProfileStorageError:
                raise
            except OSError as exc:
                raise PluginProfileStorageError(f"Cannot access plugin configuration: {exc}") from exc
        finally:
            lock.release()

    def _profile_path(self, profile_id: str) -> Path:
        validate_profile_id(profile_id)
        return self._safe(self.root / "plugin-profiles" / profile_id / "current.toml")

    def _section_path(self, section: str) -> Path:
        if not isinstance(section, str) or section not in SECTIONS:
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
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="", dir=path.parent,
                                             prefix=f".{path.name}.", delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

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

    def _read_system(self) -> SystemSettings | None:
        path = self._section_path("system")
        return SystemSettings.from_dict(self._read(path)) if path.exists() else None

    def get_system(self) -> SystemSettings | None:
        with self._access("system"):
            return self._read_system()

    def save_system(self, settings: SystemSettings, expected_revision: str | None = None) -> SystemSettings:
        self._expected_revision(expected_revision)
        settings = SystemSettings.from_dict(settings.to_dict())
        with self._access("system", write=True):
            previous = self._read_system()
            if (previous.revision if previous else None) != expected_revision:
                raise ProfileRevisionConflict("System settings changed; reload before saving")
            self._write(self._section_path("system"), settings.to_dict())
            return settings

    def remove_system(self, *, expected_revision: str) -> None:
        self._expected_revision(expected_revision)
        with self._access("system"):
            previous = self._read_system()
            if previous is None or previous.revision != expected_revision:
                raise ProfileRevisionConflict("System settings changed; reload before restoring")
            self._section_path("system").unlink()

    def get_activation_set(self) -> ActivationSet:
        if not self.root.exists():
            return ActivationSet({}, None)
        with ExitStack() as stack:
            for section in SECTIONS:
                stack.enter_context(self._access(section))
            return ActivationSet({module: self._read_module(module) for module in MODULES}, self._read_system())
