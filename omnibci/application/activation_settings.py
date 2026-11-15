"""Assign every runtime host setting to exactly one configuration section."""
from copy import deepcopy


MODULES = ("acquisition", "processing", "stimulation", "actuation")
# Shared by nodes that may run on separate machines; any change rebuilds the graph.
SYSTEM_SECTION = "system"
SECTIONS = (*MODULES, SYSTEM_SECTION)

SYSTEM_SETTING_DEFAULTS = {
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
    "system": frozenset(SYSTEM_SETTING_DEFAULTS),
}

RUNTIME_HOST_SETTING_KEYS = frozenset().union(*SECTION_SETTING_KEYS.values())

BACKEND_STARTUP_DEFAULTS = {
    "LOGGING.level": "INFO",
    "LOGGING.file_path": "omnibci.log",
    "NEUROENGINE.command_timeout_s": 10.0,
    "PLUGINS.user_dir": "",
}


def backend_startup_settings(settings: dict) -> dict:
    """Resolve declared backend defaults for comparison with the loaded process."""
    return {key: deepcopy(settings.get(key, default)) for key, default in BACKEND_STARTUP_DEFAULTS.items()}


def section_settings(settings: dict, section: str) -> dict:
    """Copy only the host settings owned by one configuration section."""
    return {key: deepcopy(settings[key]) for key in SECTION_SETTING_KEYS[section] if key in settings}


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
