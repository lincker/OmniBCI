"""Atomic TOML persistence for module profiles in a user-writable directory."""
import os
import tempfile
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
from threading import RLock

import tomlkit
from platformdirs import user_config_path

from omnibci.application.configuration import (
    ConfigProfile, ConfigProfileNotFoundError, ConfigProfileValidationError, ConfigProfileStorageError,
    validate_module, validate_profile_id,
)


def config_root() -> Path:
    override = os.environ.get("OMNIBCI_CONFIG_DIR")
    if override:
        path = Path(override).expanduser()
        if not path.is_absolute():
            raise ConfigProfileValidationError("OMNIBCI_CONFIG_DIR must be an absolute path")
        return path.resolve()
    return user_config_path("OmniBCI", appauthor=False, roaming=False)


class TomlConfigProfileRepository:
    def __init__(self, root: Path | None = None):
        self.root = (root if root is not None else config_root()).resolve()
        self._lock = RLock()

    @contextmanager
    def _access(self):
        with self._lock:
            try:
                yield
            except ConfigProfileStorageError:
                raise
            except OSError as exc:
                raise ConfigProfileStorageError(f"Cannot access local configuration: {exc}") from exc

    def _safe(self, path: Path) -> Path:
        if not path.resolve().is_relative_to(self.root):
            raise ConfigProfileValidationError("Profile path escapes the configuration directory")
        return path

    def _read(self, path: Path) -> dict:
        self._safe(path)
        try:
            return tomlkit.parse(path.read_text(encoding="utf-8")).unwrap()
        except (ValueError, TypeError) as exc:
            raise ConfigProfileValidationError(f"Invalid TOML in {path.name}: {exc}") from exc

    def _read_profile(self, path: Path) -> ConfigProfile:
        try:
            profile = ConfigProfile(**self._read(path))
        except TypeError as exc:
            raise ConfigProfileValidationError(f"Invalid profile {path.name}: {exc}") from exc
        if (path.stem != profile.profile_id or path.parent.name != profile.plugin_type
                or path.parent.parent.name != profile.module):
            raise ConfigProfileValidationError(f"Profile identity does not match {path.name}")
        return profile

    def _find(self, module: str, profile_id: str) -> Path:
        validate_module(module)
        validate_profile_id(profile_id)
        paths = list((self.root / "profiles" / module).glob(f"*/{profile_id}.toml"))
        if not paths:
            raise ConfigProfileNotFoundError(profile_id)
        if len(paths) != 1:
            raise ConfigProfileValidationError(f"Duplicate profile ID: {profile_id}")
        return self._safe(paths[0])

    def _write(self, path: Path, data: dict) -> None:
        self._safe(path)
        content = tomlkit.dumps(data)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=path.parent,
                prefix=f".{path.name}.", delete=False,
            ) as stream:
                temporary = Path(stream.name)
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def list(self, module: str, plugin_type: str | None = None) -> list[ConfigProfile]:
        validate_module(module)
        with self._access():
            profiles = [self._read_profile(path) for path in
                        (self.root / "profiles" / module).glob("*/*.toml")]
            return sorted(
                (p for p in profiles if plugin_type is None or p.plugin_type == plugin_type),
                key=lambda p: (p.name.casefold(), p.profile_id),
            )

    def get(self, module: str, profile_id: str) -> ConfigProfile:
        with self._access():
            return self._read_profile(self._find(module, profile_id))

    def save(self, profile: ConfigProfile) -> None:
        with self._access():
            try:
                previous = self.get(profile.module, profile.profile_id)
            except ConfigProfileNotFoundError:
                previous = None
            if previous and previous.plugin_type != profile.plugin_type:
                raise ConfigProfileValidationError("A profile's plugin_type cannot change")
            path = self.root / "profiles" / profile.module / profile.plugin_type / f"{profile.profile_id}.toml"
            self._write(path, asdict(profile))

    def delete(self, module: str, profile_id: str) -> None:
        with self._access():
            self._find(module, profile_id).unlink()

    def _selection(self) -> dict:
        path = self.root / "active.toml"
        if not path.exists():
            return {"schema_version": 1, "active": {}}
        data = self._read(path)
        if type(data.get("schema_version")) is not int or data["schema_version"] != 1:
            raise ConfigProfileValidationError("Unsupported selection schema_version")
        if not isinstance(data.get("active"), dict):
            raise ConfigProfileValidationError("Invalid active profile selection")
        for module, profile_id in data["active"].items():
            validate_module(module)
            validate_profile_id(profile_id)
        return data

    def get_active(self, module: str) -> str | None:
        validate_module(module)
        with self._access():
            return self._selection()["active"].get(module)

    def set_active(self, module: str, profile_id: str | None) -> None:
        validate_module(module)
        with self._access():
            if profile_id is not None:
                self.get(module, profile_id)
            data = self._selection()
            if profile_id is None:
                data["active"].pop(module, None)
            else:
                data["active"][module] = profile_id
            self._write(self.root / "active.toml", data)
