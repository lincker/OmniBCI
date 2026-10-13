"""Classify activation inputs without changing stored snapshots or host settings."""
from copy import deepcopy


PLUGIN_SETTING_KEYS = frozenset({
    "ACQUISITION.signal_source", "ACQUISITION.sample_rate", "ACQUISITION.channel_number",
    "ACQUISITION.DATA_PROVIDER_IP", "ACQUISITION.DATA_PROVIDER_PORT",
    "ACQUISITION.data_file_path", "ACQUISITION.file_reader_chunk_size",
    "ACQUISITION.trigger", "ACQUISITION.parallel_port_address",
    "ALGORITHM.decoder", "ALGORITHM.epoch_detector", "ALGORITHM.filters",
    "ALGORITHM.window_duration", "ALGORITHM.offset_time",
    "STIMULATION.paradigm", "ACTUATION.effector",
    "ssvep.duration", "ssvep.offset_time", "ssvep.sample_rate",
})


RUNTIME_HOST_SETTING_KEYS = frozenset({
    "ALGORITHM.baseline_duration", "NEUROENGINE.queue_size",
    "NEUROENGINE.message_server_ip", "NEUROENGINE.message_server_port",
    "NEUROENGINE.startup_timeout_s", "debug", "ACTUATION.enabled",
    "ACTUATION.server_ip", "ACTUATION.server_port", "ACTUATION.watchdog_timeout_s",
    "ACTUATION.max_command_age_s", "ACTUATION.auto_spawn",
    "STIMULATION.renderer", "STIMULATION.auto_spawn",
})

BACKEND_STARTUP_DEFAULTS = {
    "LOGGING.level": "INFO",
    "LOGGING.file_path": "omnibci.log",
    "NEUROENGINE.command_timeout_s": 10.0,
    "PERSISTENCE.session_repository": "in_memory",
    "PLUGINS.user_dir": "",
}

_RETIRED_SETTING_KEYS = frozenset({
    "SIGNAL.setup", "ACQUISITION.channel_names", "ACQUISITION.manual_montage",
    "ACQUISITION.file_reader_play_speed", "project_name", "version",
    "CONSTANT.ASYNC_SLEEP_TIME", "NEUROENGINE.COMMAND_SERVER_IP", "NEUROENGINE.COMMAND_SERVER_PORT",
})


def is_inert_activation_setting(key: str) -> bool:
    return (key in PLUGIN_SETTING_KEYS or key in _RETIRED_SETTING_KEYS
            or key in BACKEND_STARTUP_DEFAULTS or key == "PLUGINS.bindings"
            or key.startswith(("CONFIG.", "PROFILES.", "LOGGING.")))


def backend_startup_settings(settings: dict) -> dict:
    """Resolve declared backend defaults for comparison with the loaded process."""
    return {key: deepcopy(settings.get(key, default)) for key, default in BACKEND_STARTUP_DEFAULTS.items()}


def activation_host_settings(settings: dict) -> dict:
    """Copy runtime/context and unknown values; keep unknown ownership explicit."""
    return {key: deepcopy(value) for key, value in settings.items()
            if not is_inert_activation_setting(key)}
