"""Separate known plugin inputs from host settings without changing saved data."""
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


def activation_host_settings(settings: dict) -> dict:
    """Copy host/context values; plugin parameters are owned by pinned bindings."""
    return {key: deepcopy(value) for key, value in settings.items()
            if key not in PLUGIN_SETTING_KEYS and not key.startswith("LOGGING.")}
