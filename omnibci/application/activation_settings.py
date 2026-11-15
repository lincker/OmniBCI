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
