"""Assign every runtime host setting to exactly one configuration section."""
from copy import deepcopy


MODULES = ("acquisition", "processing", "stimulation", "actuation")
# Shared by nodes that may run on separate machines; any change rebuilds the graph.
SYSTEM_SECTION = "system"
SECTIONS = (*MODULES, SYSTEM_SECTION)

SECTION_SETTING_KEYS = {
    "acquisition": frozenset(),
    "processing": frozenset({"ALGORITHM.baseline_duration"}),
    "stimulation": frozenset({"STIMULATION.renderer", "STIMULATION.auto_spawn"}),
    "actuation": frozenset({
        "ACTUATION.server_ip", "ACTUATION.server_port",
        "ACTUATION.watchdog_timeout_s", "ACTUATION.max_command_age_s", "ACTUATION.auto_spawn",
    }),
    "system": frozenset({
        "NEUROENGINE.message_server_ip", "NEUROENGINE.message_server_port",
        "NEUROENGINE.startup_timeout_s", "debug",
        "NEUROENGINE.queue_size",
    }),
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
