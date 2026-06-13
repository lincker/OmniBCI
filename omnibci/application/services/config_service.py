"""
ConfigService — layered configuration management (默认配置 + 个性化 profile).

Policy lives here (whitelist, types, effect-on); storage primitives live in
``omnibci.conf.OmniBCISettings``.  See docs/frontend-separation.md §五.8 and
the stage-4 design discussion.

    effective = active profile overlay ⊕ default (settings.toml)
"""
import threading
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from omnibci.conf import OmniBCISettings, ProfileNameError, ProfileNotFoundError


@dataclass(frozen=True)
class ConfigPolicy:
    type: str           # "str" | "int" | "float" | "bool" | "list[str]"
    requires: str       # "hot" | "engine-restart"
    description: str


#: Config keys that map to a plugin_type and can be soft-validated against
#: the discovered plugin catalog (warn-only, never block a write).
_PLUGIN_SOFT_VALIDATE: Dict[str, str] = {
    "STIMULATION.paradigm":      "paradigm",
    "ACQUISITION.signal_source": "acquisition",
    "ALGORITHM.decoder":         "decoder",
}

#: The only keys readable/writable through the config API — for defaults AND
#: profiles alike.  Everything else in settings.toml stays internal.
CONFIG_WHITELIST: Dict[str, ConfigPolicy] = {
    "ACQUISITION.signal_source": ConfigPolicy("str", "engine-restart", "数据源插件 id"),
    "ACQUISITION.data_file_path": ConfigPolicy("str", "engine-restart", "file_reader 回放文件路径"),
    "ACQUISITION.file_reader_play_speed": ConfigPolicy("float", "engine-restart", "回放速度倍率"),
    "ACQUISITION.sample_rate": ConfigPolicy("int", "engine-restart", "采样率 (Hz)"),
    "ACQUISITION.channel_number": ConfigPolicy("int", "engine-restart", "通道数"),
    "ALGORITHM.decoder": ConfigPolicy("str", "engine-restart", "解码器插件 id"),
    "ALGORITHM.window_duration": ConfigPolicy("float", "engine-restart", "解码窗长 (s)"),
    "ALGORITHM.offset_time": ConfigPolicy("float", "engine-restart", "触发偏移 (s)"),
    "ALGORITHM.baseline_duration": ConfigPolicy("float", "engine-restart", "基线时长 (s)"),
    "ALGORITHM.filters": ConfigPolicy("list[str]", "engine-restart", "预处理滤波器链"),
    "STIMULATION.paradigm": ConfigPolicy("str", "hot", "刺激范式 plugin_type"),
    "NEUROENGINE.queue_size": ConfigPolicy("int", "engine-restart", "进程间队列容量"),
}

_CHECKS = {
    "str": lambda v: isinstance(v, str),
    "int": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "float": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "bool": lambda v: isinstance(v, bool),
    "list[str]": lambda v: isinstance(v, list) and all(isinstance(i, str) for i in v),
}

DEFAULT_SCOPE = "default"


class ConfigKeyNotAllowedError(KeyError):
    def __init__(self, keys: List[str]):
        self.keys = keys
        super().__init__(f"Keys not in whitelist: {keys}")


class ConfigValidationError(ValueError):
    def __init__(self, bad_keys: Dict[str, str]):  # key → reason
        self.bad_keys = bad_keys
        super().__init__(f"Invalid config values: {sorted(bad_keys)}")


class ProfileInUseError(RuntimeError):
    def __init__(self, name: str):
        self.name = name
        super().__init__(f"Profile {name!r} is active")


class ConfigService:
    """Whitelist-governed reads/writes over the layered settings."""

    def __init__(self, settings: OmniBCISettings) -> None:
        self._settings = settings
        self._write_lock = threading.Lock()

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    def list_entries(self) -> Tuple[List[dict], Optional[str]]:
        entries = []
        for key, pol in CONFIG_WHITELIST.items():
            default = self._settings.get_default(key)
            override = self._settings.get_override(key)
            entries.append({
                "key": key,
                "default_value": default,
                "profile_value": override,
                "effective_value": override if override is not None else default,
                "type": pol.type,
                "requires": pol.requires,
                "description": pol.description,
            })
        return entries, self._settings.active_profile

    def list_profiles(self) -> Tuple[List[dict], Optional[str]]:
        return self._settings.list_profiles(), self._settings.active_profile

    def profile_detail(self, name: str) -> dict:
        entries = self._settings.read_profile(name)  # ProfileNotFoundError propagates
        unknown = [k for k in entries if k not in CONFIG_WHITELIST]
        # Hand-edited files may carry off-whitelist keys; expose them read-only
        # so the UI can show (and the user can clean) them.
        return {"name": name, "entries": entries, "off_whitelist_keys": sorted(unknown)}

    # ------------------------------------------------------------------
    # Writes
    # ------------------------------------------------------------------

    def _validate(self, updates: Dict[str, Any]) -> Dict[str, Any]:
        if not updates:
            raise ConfigValidationError({"<body>": "updates must not be empty"})
        unknown = sorted(k for k in updates if k not in CONFIG_WHITELIST)
        if unknown:
            raise ConfigKeyNotAllowedError(unknown)
        bad = {
            k: f"expected {CONFIG_WHITELIST[k].type}"
            for k, v in updates.items()
            if not _CHECKS[CONFIG_WHITELIST[k].type](v)
        }
        if bad:
            raise ConfigValidationError(bad)
        # Soft-validate plugin_type keys against discovered plugin lists (warn only).
        for key, category in _PLUGIN_SOFT_VALIDATE.items():
            if key in updates:
                self._soft_validate_plugin(str(updates[key]), category, key)
        # Normalise int→float for float keys so the toml type stays stable.
        return {
            k: (float(v) if CONFIG_WHITELIST[k].type == "float" else v)
            for k, v in updates.items()
        }

    @staticmethod
    def _soft_validate_plugin(plugin_type: str, category: str, config_key: str) -> None:
        """Warn (never raise) if plugin_type is not in the discovered list for category."""
        try:
            from omnibci.infrastructure.plugins.plugin_catalog import list_plugins  # noqa: PLC0415
            import logging  # noqa: PLC0415
            known = [p.plugin_type for p in list_plugins(category)]
            if known and plugin_type not in known:
                logging.getLogger(__name__).warning(
                    "%s %r not in discovered %s list %s — "
                    "saved anyway (may be a user-area plugin not yet installed).",
                    config_key, plugin_type, category, known,
                )
        except Exception:
            pass  # discovery failure must never block a config write

    def apply(self, updates: Dict[str, Any], scope: str = DEFAULT_SCOPE,
              description: Optional[str] = None) -> Tuple[List[str], bool]:
        """All-or-nothing PATCH. scope='default' → settings.toml;
        scope=<profile name> → that profile file (created if absent)."""
        clean = self._validate(updates)
        with self._write_lock:
            if scope == DEFAULT_SCOPE:
                for k, v in clean.items():
                    self._settings.set(k, v)
                self._settings.save()
            else:
                # ProfileNameError propagates (422 at the router).
                self._settings.write_profile(scope, clean, description)
        restart = any(
            CONFIG_WHITELIST[k].requires == "engine-restart" for k in clean
        )
        return sorted(clean), restart

    def put_profile(self, name: str, updates: Dict[str, Any],
                    description: Optional[str] = None) -> Tuple[List[str], bool]:
        """Create/replace a profile wholesale (PUT semantics)."""
        clean = self._validate(updates)
        with self._write_lock:
            self._settings.write_profile(name, clean, description, replace=True)
        restart = any(
            CONFIG_WHITELIST[k].requires == "engine-restart" for k in clean
        )
        return sorted(clean), restart

    def delete_profile(self, name: str) -> None:
        with self._write_lock:
            if self._settings.active_profile == name:
                raise ProfileInUseError(name)
            self._settings.delete_profile(name)  # ProfileNotFoundError propagates

    def activate(self, name: str) -> None:
        with self._write_lock:
            self._settings.set_active_profile(name)  # ProfileNotFoundError propagates

    def deactivate(self) -> None:
        with self._write_lock:
            self._settings.set_active_profile(None)
