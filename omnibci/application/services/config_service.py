"""Whitelist-governed host settings, saved separately from runtime activation."""

import math
import threading
from contextlib import nullcontext
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Tuple

from omnibci.conf import OmniBCISettings


@dataclass(frozen=True)
class ConfigPolicy:
    type: str
    requires: str
    description: str


# Plugin selections and parameters are edited through Plugin Profiles only.
CONFIG_WHITELIST: Dict[str, ConfigPolicy] = {
    "ALGORITHM.baseline_duration": ConfigPolicy("float", "engine-restart", "Baseline duration (s)"),
    "NEUROENGINE.queue_size": ConfigPolicy("int", "engine-restart", "Inter-process queue capacity"),
}


def _valid_baseline(value: Any) -> bool:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    try:
        return math.isfinite(value) and value >= 0
    except OverflowError:
        return False


_CHECKS = {
    "int": lambda v: isinstance(v, int) and not isinstance(v, bool) and v > 0,
    "float": _valid_baseline,
}
_EXPECTATIONS = {"int": "positive integer", "float": "finite nonnegative number"}


class ConfigKeyNotAllowedError(KeyError):
    def __init__(self, keys: List[str]):
        self.keys = keys
        super().__init__(f"Keys not in whitelist: {keys}")


class ConfigValidationError(ValueError):
    def __init__(self, bad_keys: Dict[str, str]):
        self.bad_keys = bad_keys
        super().__init__(f"Invalid config values: {sorted(bad_keys)}")


class ConfigService:
    """Save host settings without changing accepted or running configuration."""

    def __init__(self, settings: OmniBCISettings, *, change_guard: Callable | None = None) -> None:
        self._settings = settings
        self._change_guard = change_guard or (lambda requires_idle: nullcontext())
        self._write_lock = threading.RLock()

    def read(self) -> Tuple[List[dict], str]:
        with self._write_lock:
            snapshot, revision = self._settings.read_saved()
        return [{
            "key": key,
            "value": snapshot.get(key),
            "type": policy.type,
            "requires": policy.requires,
            "description": policy.description,
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
            key: f"expected {_EXPECTATIONS[CONFIG_WHITELIST[key].type]}"
            for key, value in updates.items()
            if not _CHECKS[CONFIG_WHITELIST[key].type](value)
        }
        if bad:
            raise ConfigValidationError(bad)
        # Normalize integer inputs for float settings before TOML serialization.
        return {
            key: float(value) if CONFIG_WHITELIST[key].type == "float" else value
            for key, value in updates.items()
        }

    def save(self, updates: Dict[str, Any], *, expected_revision: str) -> Tuple[List[str], bool, str]:
        clean = self._validate(updates)
        with self._write_lock, self._change_guard(False):
            revision = self._settings.update(clean, expected_revision=expected_revision)
        restart = any(CONFIG_WHITELIST[key].requires == "engine-restart" for key in clean)
        return sorted(clean), restart, revision
