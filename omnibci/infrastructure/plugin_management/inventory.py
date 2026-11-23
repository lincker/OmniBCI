"""Read exact plugin payload bytes without importing code or changing files."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import stat


_TOCTOU_REHASH_MAX_BYTES = 64 * 1024


def is_bytecode_path(path: Path) -> bool:
    return "__pycache__" in path.parts or path.suffix == ".pyc"


def artifact_inventory_with_stats(
    location: Path,
) -> tuple[dict[str, str], dict[str, tuple[int, int, int]]]:
    """Hash all regular artifact files and record their stat signatures."""
    if location != location.resolve() or not location.is_dir():
        raise ValueError("Plugin artifact must be a real absolute directory")
    files: dict[str, str] = {}
    stats: dict[str, tuple[int, int, int]] = {}
    for child in sorted(location.rglob("*")):
        if child.is_symlink():
            raise ValueError(f"Plugin artifact contains symlink: {child}")
        st = child.stat()
        mode = st.st_mode
        if stat.S_ISDIR(mode):
            continue
        if not stat.S_ISREG(mode):
            raise ValueError("Plugin artifacts require regular files and directories")
        relative = child.relative_to(location)
        if is_bytecode_path(relative) or relative.as_posix() == ".omnibci-install.json":
            continue
        key = relative.as_posix()
        with child.open("rb") as reader:
            files[key] = hashlib.file_digest(reader, "sha256").hexdigest()
        stats[key] = (st.st_ino, st.st_size, st.st_mtime_ns)
    return files, stats


def artifact_inventory(location: Path) -> dict[str, str]:
    files, _ = artifact_inventory_with_stats(location)
    return files


def verify_inventory_unchanged(
    location: Path,
    files: dict[str, str],
    stats: dict[str, tuple[int, int, int]],
    *,
    rehash_max_bytes: int = _TOCTOU_REHASH_MAX_BYTES,
) -> bool:
    """Confirm no artifact file changed without re-hashing unchanged large assets."""
    if location != location.resolve() or not location.is_dir():
        return False
    seen: set[str] = set()
    for child in sorted(location.rglob("*")):
        if child.is_symlink():
            return False
        st = child.stat()
        mode = st.st_mode
        if stat.S_ISDIR(mode):
            continue
        if not stat.S_ISREG(mode):
            return False
        relative = child.relative_to(location)
        if is_bytecode_path(relative) or relative.as_posix() == ".omnibci-install.json":
            continue
        key = relative.as_posix()
        expected_hash = files.get(key)
        expected_stat = stats.get(key)
        if expected_hash is None or expected_stat is None:
            return False
        seen.add(key)
        current_stat = (st.st_ino, st.st_size, st.st_mtime_ns)
        if current_stat != expected_stat or st.st_size <= rehash_max_bytes:
            with child.open("rb") as reader:
                if hashlib.file_digest(reader, "sha256").hexdigest() != expected_hash:
                    return False
    return len(seen) == len(files)


def content_digest(files: dict[str, str]) -> str:
    return hashlib.sha256(
        json.dumps(files, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
