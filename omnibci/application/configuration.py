"""Portable local configuration profiles, independent of storage and accounts."""
import re
from copy import deepcopy
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from omnibci.application.config_errors import (
    ConfigProfileConflictError, ConfigProfileValidationError,
)


class ConfigProfileNotFoundError(KeyError):
    """A requested module profile does not exist."""


class ConfigProfileStorageError(OSError):
    """Local profile storage is unavailable; existing files are preserved."""


def validate_module(module: str) -> None:
    if module != "acquisition":
        raise ConfigProfileValidationError(f"Unsupported module: {module!r}")


def validate_profile_id(profile_id: str) -> None:
    try:
        if not isinstance(profile_id, str) or str(UUID(profile_id)) != profile_id:
            raise ValueError()
    except (ValueError, AttributeError):
        raise ConfigProfileValidationError("profile_id must be a canonical UUID") from None


@dataclass(frozen=True)
class ConfigProfile:
    profile_id: str
    name: str
    module: str
    plugin_type: str
    parameters: dict[str, Any]
    schema_version: int = 1

    def __post_init__(self):
        validate_profile_id(self.profile_id)
        validate_module(self.module)
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise ConfigProfileValidationError("Unsupported profile schema_version")
        if not isinstance(self.name, str) or not 1 <= len(self.name.strip()) <= 64:
            raise ConfigProfileValidationError("name must contain 1 to 64 characters")
        if any(ord(character) < 32 for character in self.name):
            raise ConfigProfileValidationError("name must be single-line text without control characters")
        object.__setattr__(self, "name", self.name.strip())
        if not isinstance(self.plugin_type, str) or not re.fullmatch(r"[a-zA-Z0-9_-]+", self.plugin_type):
            raise ConfigProfileValidationError("Invalid plugin_type")
        if not isinstance(self.parameters, dict):
            raise ConfigProfileValidationError("parameters must be an object")


_RATE = {"key": "sample_rate", "label": "Sample rate (Hz)", "type": "int", "default": 1000, "minimum": 1,
         "description": "Processing sample rate (Hz); must match the recording or device."}
_CHANNELS = {"key": "channel_number", "label": "Output channels", "type": "int", "default": 10, "minimum": 1,
             "description": "Expected output channels, including the event channel."}
_HOST = {"key": "DATA_PROVIDER_IP", "label": "Device host", "type": "str", "default": "127.0.0.1",
         "description": "Device host or IP address on this machine's network."}
_PORT = {"key": "DATA_PROVIDER_PORT", "label": "TCP port", "type": "int", "default": 4000,
         "minimum": 1, "maximum": 65535, "description": "Device TCP port."}

# Explicit schemas for the three supported acquisition adapters. No plugin
# discovery or generic schema registry is required for local profiles.
ACQUISITION_PLUGINS = {
    "neuroscan_scan45_reader": {
        "label": "Neuroscan Scan 4.5",
        "fields": [_HOST, _PORT, _RATE,
                   {**_CHANNELS, "minimum": 2,
                    "description": "Total channels sent by the device, including the final event channel."}],
    },
    "neuroscan_curry8_reader": {
        "label": "Neuroscan Curry 8",
        "fields": [_HOST, {**_PORT, "default": 4455}, _RATE, _CHANNELS],
    },
    "file_reader": {
        "label": "File reader",
        "fields": [
            {"key": "data_file_path", "label": "Recording path", "type": "str", "default": "tests/fixture/train.cnt",
             "description": "CNT file on the backend machine; relative paths use the project root."},
            {"key": "file_reader_chunk_size", "label": "Chunk size (samples)", "type": "int", "default": 100, "minimum": 1,
             "description": "Samples per replay chunk."},
            _RATE, _CHANNELS,
        ],
    },
}


def acquisition_fields(plugin_type: str) -> list[dict]:
    if plugin_type not in ACQUISITION_PLUGINS:
        raise ConfigProfileValidationError(f"Unsupported acquisition plugin: {plugin_type!r}")
    return deepcopy(ACQUISITION_PLUGINS[plugin_type]["fields"])


def validate_parameters(plugin_type: str, parameters: dict) -> dict:
    fields = acquisition_fields(plugin_type)
    if not isinstance(parameters, dict) or set(parameters) != {f["key"] for f in fields}:
        raise ConfigProfileValidationError(
            f"parameters must contain exactly: {', '.join(f['key'] for f in fields)}")
    for field in fields:
        key = field["key"]
        value = parameters[key]
        if field["type"] == "int":
            if (type(value) is not int or value < field["minimum"]
                    or ("maximum" in field and value > field["maximum"])):
                raise ConfigProfileValidationError(f"{key} must be an integer in the allowed range")
        elif not isinstance(value, str) or not value.strip() or any(c in value for c in "\x00\n\r"):
            raise ConfigProfileValidationError(f"{key} must be non-empty, single-line text")
    return deepcopy(parameters)
