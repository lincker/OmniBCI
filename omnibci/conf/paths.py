"""Portable path resolution for file paths stored in configuration."""

from os import PathLike
from pathlib import Path, PurePosixPath


_PROJECT_ROOT = Path(__file__).resolve().parents[2]


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
