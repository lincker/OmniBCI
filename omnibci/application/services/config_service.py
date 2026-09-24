"""
ConfigService — layered configuration management (默认配置 + 个性化 profile).

Policy lives here (whitelist, types, effect-on); storage primitives live in
``omnibci.conf.OmniBCISettings``.  See docs/frontend-separation.md §五.8 and
the stage-4 design discussion.

    effective = active profile overlay ⊕ default (settings.toml)
"""
import logging
import threading
from contextlib import nullcontext
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple
from uuid import uuid4

from omnibci.conf import OmniBCISettings, ProfileNameError, ProfileNotFoundError
from omnibci.application.config_errors import ConfigProfileConflictError, ConfigProfileValidationError
from omnibci.application.configuration import (
    ACQUISITION_PLUGINS, ConfigProfile, ConfigProfileNotFoundError,
    acquisition_fields, validate_module, validate_parameters,
)
from omnibci.application.ports.config_profile_repository import ConfigProfileRepository


@dataclass(frozen=True)
class ConfigPolicy:
    type: str           # "str" | "int" | "float" | "bool" | "list[str]"
    requires: str       # "hot" | "engine-restart"
    description: str


#: Config keys that map to a plugin_type and can be soft-validated against
#: the discovered plugin catalog (warn-only, never block a write).
_PLUGIN_SOFT_VALIDATE: Dict[str, str] = {
    "STIMULATION.paradigm":      "paradigm",
    "ACTUATION.effector":        "effector",
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
    "ACQUISITION.parallel_port_address": ConfigPolicy(
        "str", "engine-restart",
        "Trigger parallel-port I/O base address in hex (e.g. 0x3100); "
        "restart stimulation to apply",
    ),
    "ALGORITHM.decoder": ConfigPolicy("str", "engine-restart", "解码器插件 id"),
    "ALGORITHM.window_duration": ConfigPolicy("float", "engine-restart", "解码窗长 (s)"),
    "ALGORITHM.offset_time": ConfigPolicy("float", "engine-restart", "触发偏移 (s)"),
    "ALGORITHM.baseline_duration": ConfigPolicy("float", "engine-restart", "基线时长 (s)"),
    "ALGORITHM.filters": ConfigPolicy("list[str]", "engine-restart", "预处理滤波器链"),
    "STIMULATION.paradigm": ConfigPolicy("str", "hot", "刺激范式 plugin_type"),
    "ACTUATION.effector": ConfigPolicy("str", "engine-restart", "控制输出 plugin_type"),
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

    def __init__(
        self,
        settings: OmniBCISettings,
        plugin_catalog: Optional[Callable[[str], List[Any]]] = None,
        *,
        repository: ConfigProfileRepository | None = None,
        change_guard: Callable | None = None,
    ) -> None:
        """*plugin_catalog*: ``category -> [PluginInfo]`` 查询函数，用于对
        plugin_type 类配置键做警告级软校验。由组合侧注入（presentation/main
        传 infrastructure 的 list_plugins）；None = 跳过软校验（单测默认）。
        P0-1：替代原先在方法内延迟 import infrastructure.plugin_catalog。"""
        self._settings = settings
        self._plugin_catalog = plugin_catalog
        self._repository = repository
        self._change_guard = change_guard or (lambda requires_idle: nullcontext())
        self._write_lock = threading.RLock()

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    def list_entries(self) -> Tuple[List[dict], Optional[str]]:
        snapshot = self.resolve_snapshot()
        entries = []
        for key, pol in CONFIG_WHITELIST.items():
            default = self._settings.get_default(key)
            override = self._settings.get_override(key)
            entries.append({
                "key": key,
                "default_value": default,
                "profile_value": override,
                "effective_value": snapshot.get(key),
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
        if self._plugin_catalog is not None:
            for key, category in _PLUGIN_SOFT_VALIDATE.items():
                if key in updates:
                    self._soft_validate_plugin(str(updates[key]), category, key)
        # Normalise int→float for float keys so the toml type stays stable.
        return {
            k: (float(v) if CONFIG_WHITELIST[k].type == "float" else v)
            for k, v in updates.items()
        }

    def _soft_validate_plugin(self, plugin_type: str, category: str, config_key: str) -> None:
        """Warn (never raise) if plugin_type is not in the discovered list for category."""
        try:
            known = [p.plugin_type for p in self._plugin_catalog(category)]
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
        with self._write_lock, self._change_guard(self._legacy_affects_acquisition(scope, clean)):
            self._check_module_ownership(scope, clean)
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
            affected = clean
            if self._settings.active_profile == name:
                # A full replacement can also remove acquisition overrides.
                affected = {**self._settings.read_profile(name), **clean}
            with self._change_guard(self._legacy_affects_acquisition(name, affected)):
                self._check_module_ownership(name, affected)
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
        with self._write_lock, self._change_guard(True):
            self._settings.set_active_profile(name)  # ProfileNotFoundError propagates

    def deactivate(self) -> None:
        with self._write_lock, self._change_guard(True):
            self._settings.set_active_profile(None)

    def _legacy_affects_acquisition(self, scope: str, updates: dict) -> bool:
        return (scope in (DEFAULT_SCOPE, self._settings.active_profile)
                and any(k.startswith("ACQUISITION.") for k in updates))

    def _check_module_ownership(self, scope: str, updates: dict) -> None:
        if self._legacy_affects_acquisition(scope, updates) and self._repository is not None:
            active = self._repository.get_active("acquisition")
            if active is not None:
                owned = {"ACQUISITION.signal_source"} | {
                    f"ACQUISITION.{field['key']}"
                    for field in acquisition_fields(self.module_profile("acquisition", active).plugin_type)
                }
                if owned.intersection(updates):
                    raise ConfigProfileConflictError(
                        "An acquisition profile owns these fields; edit it or deactivate it first")

    # Module profiles: local persistence is injected; no account or sync concepts.

    def _profiles(self) -> ConfigProfileRepository:
        if self._repository is None:
            raise RuntimeError("Module profile repository is not configured")
        return self._repository

    def resolve_snapshot(self) -> dict:
        """Resolve once at engine start; callers pass this copy to all workers."""
        with self._write_lock:
            snapshot = self._settings.snapshot()
            if self._repository is not None:
                active = self._repository.get_active("acquisition")
                if active is not None:
                    profile = self.module_profile("acquisition", active)
                    snapshot["ACQUISITION.signal_source"] = profile.plugin_type
                    snapshot.update({f"ACQUISITION.{k}": v for k, v in profile.parameters.items()})
            return snapshot

    def list_module_profiles(self, module: str, plugin_type: str | None = None) -> list[ConfigProfile]:
        validate_module(module)
        if plugin_type is not None:
            acquisition_fields(plugin_type)
        return self._profiles().list(module, plugin_type)

    def module_profile(self, module: str, profile_id: str) -> ConfigProfile:
        profile = self._profiles().get(module, profile_id)
        validate_parameters(profile.plugin_type, profile.parameters)
        return profile

    def module_detail(self, module: str) -> dict:
        validate_module(module)
        result = {
            "module": module, "active_profile_id": None, "effective_plugin_type": None,
            "effective_parameters": {}, "selection_error": None,
            "plugins": [{"plugin_type": key, "label": value["label"],
                         "fields": acquisition_fields(key)}
                        for key, value in ACQUISITION_PLUGINS.items()],
        }
        with self._write_lock:
            try:
                result["active_profile_id"] = self._profiles().get_active(module)
                snapshot = self.resolve_snapshot()
                plugin_type = snapshot.get("ACQUISITION.signal_source")
                result["effective_plugin_type"] = plugin_type
                result["effective_parameters"] = {
                    f["key"]: snapshot.get(f"ACQUISITION.{f['key']}", f["default"])
                    for f in acquisition_fields(plugin_type)
                }
            except (ConfigProfileValidationError, ConfigProfileNotFoundError, OSError) as exc:
                # Configuration management stays available to repair or clear a
                # broken selection, while resolve_snapshot still blocks startup.
                result["selection_error"] = str(exc)
        return result

    def _check_name(self, profile: ConfigProfile) -> None:
        if any(p.profile_id != profile.profile_id and p.name.strip().casefold() == profile.name.casefold()
               for p in self._profiles().list(profile.module, profile.plugin_type)):
            raise ConfigProfileConflictError("A profile with this name already exists for the plugin")

    def create_module_profile(self, module: str, plugin_type: str, name: str,
                              parameters: dict) -> ConfigProfile:
        profile = ConfigProfile(str(uuid4()), name, module, plugin_type,
                                validate_parameters(plugin_type, parameters))
        with self._write_lock:
            self._check_name(profile)
            self._profiles().save(profile)
        return profile

    def update_module_profile(self, module: str, profile_id: str, name: str,
                              parameters: dict) -> ConfigProfile:
        with self._write_lock:
            previous = self._profiles().get(module, profile_id)
            profile = ConfigProfile(profile_id, name, module, previous.plugin_type,
                                    validate_parameters(previous.plugin_type, parameters))
            affects_active = (self._profiles().get_active(module) == profile_id
                              and previous.parameters != profile.parameters)
            with self._change_guard(affects_active):
                self._check_name(profile)
                self._profiles().save(profile)
            return profile

    def delete_module_profile(self, module: str, profile_id: str) -> None:
        with self._write_lock:
            if self._profiles().get_active(module) == profile_id:
                raise ConfigProfileConflictError("The profile is active; deactivate or switch first")
            self._profiles().delete(module, profile_id)

    def select_module_profile(self, module: str, profile_id: str | None) -> None:
        with self._write_lock, self._change_guard(True):
            validate_module(module)
            if profile_id is not None:
                self.module_profile(module, profile_id)
            self._profiles().set_active(module, profile_id)
