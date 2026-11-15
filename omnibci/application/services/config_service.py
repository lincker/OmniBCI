"""Backend startup settings share storage with independently applied Engine settings."""

import math
import threading
from dataclasses import dataclass
from typing import Any, Dict, List, Tuple

from omnibci.application.activation_settings import backend_startup_settings
from omnibci.conf import OmniBCISettings


@dataclass(frozen=True)
class ConfigPolicy:
    type: str
    requires: str
    description: str


# Engine and module settings are written only by their runtime application flow.
CONFIG_WHITELIST: Dict[str, ConfigPolicy] = {
    "LOGGING.level": ConfigPolicy("string", "backend-restart", "Backend logging level"),
    "LOGGING.file_path": ConfigPolicy("string", "backend-restart", "Log path; relative paths use the user log directory"),
    "NEUROENGINE.command_timeout_s": ConfigPolicy("float", "backend-restart", "Seconds to wait for a module command result"),
    "PLUGINS.user_dir": ConfigPolicy("string", "backend-restart", "Plugin directory default; OMNIBCI_PLUGIN_DIR overrides it at backend startup"),
}


def _valid_timeout(value):
    try:
        return type(value) in (int, float) and math.isfinite(value) and value > 0
    except OverflowError:
        return False


_CHECKS = {
    "LOGGING.level": lambda value: isinstance(value, str) and value in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"},
    "LOGGING.file_path": lambda value: isinstance(value, str) and bool(value.strip()),
    "NEUROENGINE.command_timeout_s": _valid_timeout,
    "PLUGINS.user_dir": lambda value: isinstance(value, str),
}
_EXPECTATIONS = {
    "LOGGING.level": "DEBUG, INFO, WARNING, ERROR, or CRITICAL",
    "LOGGING.file_path": "non-empty text",
    "NEUROENGINE.command_timeout_s": "finite positive number",
    "PLUGINS.user_dir": "text (empty selects the platform default)",
}


class ConfigKeyNotAllowedError(KeyError):
    def __init__(self, keys: List[str]):
        self.keys = keys
        super().__init__(f"Keys not in whitelist: {keys}")


class ConfigValidationError(ValueError):
    def __init__(self, bad_keys: Dict[str, str]):
        self.bad_keys = bad_keys
        super().__init__(f"Invalid config values: {sorted(bad_keys)}")


class ConfigService:
    """Save backend values while retaining the snapshot captured at startup."""

    def __init__(self, settings: OmniBCISettings, *, active_settings: dict | None = None) -> None:
        self._settings = settings
        self._active = backend_startup_settings(settings.saved_snapshot() if active_settings is None else active_settings)
        self._write_lock = threading.RLock()

    def read(self) -> Tuple[List[dict], str]:
        with self._write_lock:
            saved, revision = self._settings.read_saved()
        snapshot = backend_startup_settings(saved)
        return [{
            "key": key,
            "value": snapshot.get(key),
            "type": policy.type,
            "requires": policy.requires,
            "description": policy.description,
            "active_value": self._active[key],
            "restart_required": snapshot[key] != self._active[key],
        } for key, policy in CONFIG_WHITELIST.items()], revision

    def list_entries(self) -> List[dict]:
        return self.read()[0]

    def _validate(self, updates: Dict[str, Any]) -> Dict[str, Any]:
        if not updates:
            raise ConfigValidationError({"<body>": "updates must not be empty"})
        unknown = sorted(key for key in updates if key not in CONFIG_WHITELIST)
        if unknown:
            raise ConfigKeyNotAllowedError(unknown)
        bad = {
            key: f"expected {_EXPECTATIONS[key]}"
            for key, value in updates.items()
            if not _CHECKS[key](value)
        }
        if bad:
            raise ConfigValidationError(bad)
        return {key: float(value) if CONFIG_WHITELIST[key].type == "float" else value
                for key, value in updates.items()}

    def save(self, updates: Dict[str, Any], *, expected_revision: str) -> Tuple[List[str], bool, str]:
        clean = self._validate(updates)
        with self._write_lock:
            revision = self._settings.update(clean, expected_revision=expected_revision)
            entries, _ = self.read()
        restart = any(entry["restart_required"] for entry in entries)
        return sorted(clean), restart, revision
