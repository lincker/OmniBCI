"""Declared Server, Engine and module settings and their field validation."""
from copy import deepcopy
from dataclasses import dataclass
import math
from typing import Any, Callable

from omnibci.application.configuration.errors import (
    ConfigKeyNotAllowedError, ConfigValidationError, PluginProfileValidationError,
)


MODULES = ("acquisition", "processing", "stimulation", "actuation")
# Shared by nodes that may run on separate machines; any change rebuilds the graph.
ENGINE_SECTION = "engine"
SECTIONS = (*MODULES, ENGINE_SECTION)

ENGINE_SETTING_DEFAULTS = {
    "NEUROENGINE.queue_size": 100,
    "NEUROENGINE.startup_timeout_s": 5.0,
    "debug": False,
}

MODULE_SETTING_DEFAULTS = {
    "acquisition": {},
    "processing": {"ALGORITHM.baseline_duration": 0.0},
    "stimulation": {
        "STIMULATION.server_ip": "127.0.0.1", "STIMULATION.server_port": 8765,
        "STIMULATION.renderer": "moderngl", "STIMULATION.auto_spawn": True,
    },
    "actuation": {
        "ACTUATION.server_ip": "127.0.0.1", "ACTUATION.server_port": 8001,
        "ACTUATION.watchdog_timeout_s": 1.0, "ACTUATION.max_command_age_s": 0.0,
        "ACTUATION.auto_spawn": True,
    },
}

SECTION_SETTING_KEYS = {
    **{module: frozenset(settings) for module, settings in MODULE_SETTING_DEFAULTS.items()},
    "engine": frozenset(ENGINE_SETTING_DEFAULTS),
}


def _finite_number(value, *, positive=False):
    try:
        return (type(value) in (int, float) and math.isfinite(value)
                and (value > 0 if positive else value >= 0))
    except OverflowError:
        return False


@dataclass(frozen=True)
class ServerSetting:
    default: Any
    type: str
    description: str
    validate: Callable[[Any], bool]
    expectation: str


SERVER_SETTINGS = {
    "LOGGING.level": ServerSetting(
        "INFO", "string", "Server logging level",
        lambda value: isinstance(value, str) and value in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"},
        "DEBUG, INFO, WARNING, ERROR, or CRITICAL",
    ),
    "LOGGING.file_path": ServerSetting(
        "omnibci.log", "string", "Log path; relative paths use the user log directory",
        lambda value: isinstance(value, str) and bool(value.strip()), "non-empty text",
    ),
    "NEUROENGINE.command_timeout_s": ServerSetting(
        10.0, "float", "Seconds to wait for a module command result",
        lambda value: _finite_number(value, positive=True), "finite positive number",
    ),
    "PLUGINS.user_dir": ServerSetting(
        "", "string", "Plugin directory default; OMNIBCI_PLUGIN_DIR overrides it at server startup",
        lambda value: isinstance(value, str), "text (empty selects the platform default)",
    ),
}
SERVER_STARTUP_DEFAULTS = {key: definition.default for key, definition in SERVER_SETTINGS.items()}


def validate_server_settings(values: dict, *, allow_empty: bool = False) -> dict:
    """Validate a partial Server update without consulting files or running services."""
    if not isinstance(values, dict) or not values and not allow_empty:
        raise ConfigValidationError({"<body>": "updates must be a non-empty object"})
    unknown = sorted(key for key in values if key not in SERVER_SETTINGS)
    if unknown:
        raise ConfigKeyNotAllowedError(unknown)
    bad = {key: f"expected {SERVER_SETTINGS[key].expectation}" for key, value in values.items()
           if not SERVER_SETTINGS[key].validate(value)}
    if bad:
        raise ConfigValidationError(bad)
    return {key: float(value) if SERVER_SETTINGS[key].type == "float" else value
            for key, value in values.items()}


def validate_module_settings(module: str, settings: dict) -> None:
    """Apply the owning module's field rules at every configuration boundary."""
    if module not in MODULES:
        raise PluginProfileValidationError("Unsupported configuration module")
    if settings.keys() - SECTION_SETTING_KEYS[module]:
        raise PluginProfileValidationError("Settings must belong to the configuration module")
    for key, value in settings.items():
        if key.endswith("auto_spawn"):
            valid = type(value) is bool
        elif key == "STIMULATION.renderer":
            valid = isinstance(value, str) and value in ("pyopengl", "moderngl")
        elif key.endswith(".server_ip"):
            valid = isinstance(value, str) and bool(value.strip())
        elif key.endswith(".server_port"):
            valid = type(value) is int and 1 <= value <= 65535
        else:
            valid = _finite_number(value, positive=key == "ACTUATION.watchdog_timeout_s")
        if not valid:
            raise PluginProfileValidationError(f"Invalid module setting: {key}")


def validate_engine_settings(settings: dict) -> None:
    """Validate only the shared Engine foundation's settings."""
    if settings.keys() - SECTION_SETTING_KEYS["engine"]:
        raise PluginProfileValidationError("Settings must belong to the engine section")
    for key, value in settings.items():
        if key == "debug":
            valid = type(value) is bool
        elif key == "NEUROENGINE.queue_size":
            valid = type(value) is int and value > 0
        else:
            valid = _finite_number(value, positive=True)
        if not valid:
            raise PluginProfileValidationError(f"Invalid Engine setting: {key}")


def server_startup_settings(settings: dict) -> dict:
    """Resolve declared server defaults for comparison with the loaded process."""
    return {key: deepcopy(settings.get(key, default)) for key, default in SERVER_STARTUP_DEFAULTS.items()}


def merge_module_settings(current: dict, selected: dict, modules) -> dict:
    """Replace owned module values without overwriting concurrent module updates."""
    result = deepcopy(current)
    for module in modules:
        keys = SECTION_SETTING_KEYS[module]

        def belongs(key):
            return key in keys or (module == "acquisition" and key.startswith("ACQUISITION.")
                                   and key != "ACQUISITION.trigger")

        result = {key: value for key, value in result.items() if not belongs(key)}
        result.update({key: deepcopy(value) for key, value in selected.items() if belongs(key)})
        result["PLUGINS.bindings"] = [binding for binding in result.get("PLUGINS.bindings", ())
                                     if not binding.get("slot", "").startswith(f"{module}.")]
        result["PLUGINS.bindings"].extend(deepcopy(binding) for binding in selected.get("PLUGINS.bindings", ())
                                         if binding.get("slot", "").startswith(f"{module}."))
        result.setdefault("CONFIG.module_revisions", {})[module] = selected.get("CONFIG.module_revisions", {}).get(module)
    result["PLUGINS.bindings"] = sorted(result.get("PLUGINS.bindings", ()),
                                       key=lambda binding: MODULES.index(binding["slot"].split(".", 1)[0]))
    return result
