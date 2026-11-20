"""Read exact plugin payload bytes without importing code or changing files."""
import hashlib
import json
from pathlib import Path
import stat


def is_bytecode_path(path: Path) -> bool:
    return '__pycache__' in path.parts or path.suffix == '.pyc'


def artifact_inventory(location: Path) -> dict[str, str]:
    if location != location.resolve() or not location.is_dir():
        raise ValueError('Plugin artifact must be a real absolute directory')
    files = {}
    for child in sorted(location.rglob('*')):
        if child.is_symlink():
            raise ValueError(f'Plugin artifact contains symlink: {child}')
        mode = child.stat().st_mode
        if stat.S_ISDIR(mode):
            continue
        if not stat.S_ISREG(mode):
            raise ValueError('Plugin artifacts require regular files and directories')
        relative = child.relative_to(location)
        if is_bytecode_path(relative) or relative.as_posix() == '.omnibci-install.json':
            continue
        with child.open('rb') as reader:
            files[relative.as_posix()] = hashlib.file_digest(reader, 'sha256').hexdigest()
    return files


def content_digest(files: dict[str, str]) -> str:
    return hashlib.sha256(json.dumps(files, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
