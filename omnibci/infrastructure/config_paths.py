"""Shared user-writable storage location for configuration repositories."""
import os
from pathlib import Path

from platformdirs import user_config_path

from omnibci.application.config_errors import ConfigProfileValidationError


def config_root() -> Path:
    override = os.environ.get("OMNIBCI_CONFIG_DIR")
    if override:
        path = Path(override).expanduser()
        if not path.is_absolute():
            raise ConfigProfileValidationError("OMNIBCI_CONFIG_DIR must be an absolute path")
        return path.resolve()
    return user_config_path("OmniBCI", appauthor=False, roaming=False)
