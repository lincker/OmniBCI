"""Atomic, compare-and-swap TOML storage for plugin profiles and activation."""
from __future__ import annotations

import errno
import math
import os
import re
import tempfile
import time
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from threading import RLock

import tomlkit

from omnibci.application.plugin_profiles import (
    PLUGIN_KINDS,
    ActivationSnapshot,
    PluginProfile,
    PluginProfileNotFoundError,
    PluginProfileStorageError,
    PluginProfileValidationError,
    ProfileRevisionConflict,
    validate_profile_id,
)
from omnibci.infrastructure.config_paths import config_root


class TomlPluginProfileRepository:
    def __init__(self, root: Path | None = None, *, lock_timeout_s: float = 5.0):
        self.root = (root if root is not None else config_root()).resolve()
        if (isinstance(lock_timeout_s, bool) or not isinstance(lock_timeout_s, (int, float))
                or not math.isfinite(lock_timeout_s) or lock_timeout_s <= 0):
            raise ValueError("lock_timeout_s must be positive and finite")
        self._lock_timeout_s = lock_timeout_s
        self._thread_lock = RLock()

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
    def _access(self, *, write: bool = False):
        with self._thread_lock:
            try:
                if not self.root.exists() and not write:
                    yield
                    return
                self.root.mkdir(parents=True, exist_ok=True)
                lock_path = self._safe(self.root / ".plugin-config.lock")
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

    def _profile_path(self, profile_id: str) -> Path:
        validate_profile_id(profile_id)
        return self._safe(self.root / "plugin-profiles" / f"{profile_id}.toml")

    def _read(self, path: Path) -> dict:
        self._safe(path)
        try:
            return tomlkit.parse(path.read_text(encoding="utf-8")).unwrap()
        except (ValueError, TypeError) as exc:
            raise PluginProfileValidationError(f"Invalid TOML in {path.name}: {exc}") from exc

    def _read_profile(self, path: Path, *, kind: str | None = None) -> PluginProfile | None:
        if not path.exists():
            raise PluginProfileNotFoundError(path.stem)
        data = self._read(path)
        if kind is not None and data.get("kind") != kind:
            return None
        profile = PluginProfile.from_dict(data)
        if path.stem != profile.profile_id:
            raise PluginProfileValidationError(f"Profile identity does not match {path.name}")
        if profile.revision < 1:
            raise PluginProfileValidationError("Stored profile revision must be >= 1")
        return profile

    def _write(self, path: Path, data: dict) -> None:
        self._safe(path)
        content = tomlkit.dumps(data)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
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
        with self._access():
            directory = self._safe(self.root / "plugin-profiles")
            profiles = [self._read_profile(path, kind=kind) for path in sorted(directory.glob("*.toml"))]
            return sorted((item for item in profiles if item is not None),
                          key=lambda item: (item.name.casefold(), item.profile_id))

    def get_profile(self, profile_id: str) -> PluginProfile:
        path = self._profile_path(profile_id)
        with self._access():
            return self._read_profile(path)

    @staticmethod
    def _expected_profile_revision(expected_revision: int | None) -> None:
        if expected_revision is not None and (type(expected_revision) is not int or expected_revision < 1):
            raise PluginProfileValidationError("expected_revision must be a positive integer or None")

    def save_profile(self, profile: PluginProfile, expected_revision: int | None = None) -> PluginProfile:
        self._expected_profile_revision(expected_revision)
        profile = replace(profile)
        path = self._profile_path(profile.profile_id)
        with self._access(write=True):
            previous = self._read_profile(path) if path.exists() else None
            if (previous.revision if previous is not None else None) != expected_revision:
                raise ProfileRevisionConflict("Profile changed; reload before saving")
            if previous is not None and (
                previous.kind != profile.kind
                or previous.selection.package_id != profile.selection.package_id
                or previous.selection.contribution_id != profile.selection.contribution_id
            ):
                raise PluginProfileValidationError("A profile's owning capability cannot change")
            saved = replace(profile, revision=1 if previous is None else previous.revision + 1)
            self._write(path, saved.to_dict())
            return saved

    def delete_profile(self, profile_id: str, expected_revision: int) -> None:
        self._expected_profile_revision(expected_revision)
        if expected_revision is None:
            raise PluginProfileValidationError("expected_revision is required for deletion")
        path = self._profile_path(profile_id)
        with self._access():
            previous = self._read_profile(path)
            if previous.revision != expected_revision:
                raise ProfileRevisionConflict("Profile changed; reload before deleting")
            path.unlink()

    def _read_activation(self) -> ActivationSnapshot | None:
        path = self._safe(self.root / "activation.toml")
        return ActivationSnapshot.from_dict(self._read(path)) if path.exists() else None

    def get_activation(self) -> ActivationSnapshot | None:
        with self._access():
            return self._read_activation()

    def save_activation(self, snapshot: ActivationSnapshot,
                        expected_revision: str | None = None) -> ActivationSnapshot:
        if expected_revision is not None and (
            not isinstance(expected_revision, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_revision)
        ):
            raise PluginProfileValidationError("expected_revision must be a SHA-256 revision or None")
        snapshot = ActivationSnapshot.from_dict(snapshot.to_dict())
        with self._access(write=True):
            previous = self._read_activation()
            if (previous.revision if previous is not None else None) != expected_revision:
                raise ProfileRevisionConflict("Activation changed; reload before saving")
            self._write(self.root / "activation.toml", snapshot.to_dict())
            return snapshot
