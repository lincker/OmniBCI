"""Portable path resolution for file paths stored in configuration."""

import os
from os import PathLike
from pathlib import Path, PurePosixPath

from platformdirs import user_config_path

from omnibci.application.config_errors import ConfigProfileValidationError


_PROJECT_ROOT = Path(__file__).resolve().parents[2]


def config_root() -> Path:
    """Resolve the common user configuration directory without creating it."""
    override = os.environ.get("OMNIBCI_CONFIG_DIR")
    if override:
        path = Path(override).expanduser()
        if not path.is_absolute():
            raise ConfigProfileValidationError("OMNIBCI_CONFIG_DIR must be an absolute path")
        return path.resolve()
    return user_config_path("OmniBCI", appauthor=False, roaming=False)


def resolve_config_path(
    value: str | PathLike[str], *, base_dir: str | PathLike[str] | None = None
) -> Path:
    """Resolve a platform-neutral config path against a stable base directory."""
    expanded = Path(value).expanduser()
    if expanded.is_absolute():
        return expanded.resolve()

    base = Path(base_dir).expanduser() if base_dir is not None else _PROJECT_ROOT
    native_relative = Path(*PurePosixPath(str(value)).parts)
    return (base / native_relative).resolve()
