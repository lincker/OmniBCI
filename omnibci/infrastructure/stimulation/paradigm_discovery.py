"""
Paradigm discovery — dual-root, manifest-based.

Scans vendor_dir then user_dir for paradigm packages.  For each subdirectory
that looks like a plugin package (has ``__init__.py`` OR ``manifest.toml``),
it reads the optional ``manifest.toml`` to build a rich ``ParadigmInfo``.

User-area plugins with the same ``id`` shadow vendor (builtin) plugins.

No code execution here — no OpenGL imports.
"""
import logging
import os
from pathlib import Path
from typing import List

import tomlkit

from omnibci.infrastructure.plugins.dirs import paradigm_search_roots
from omnibci.presentation.schemas.stimulation import ParadigmInfo

log = logging.getLogger(__name__)

_MANIFEST = "manifest.toml"


def _read_manifest(pkg_dir: Path) -> dict:
    """Parse manifest.toml[package] section; return {} on any error."""
    mf = pkg_dir / _MANIFEST
    if not mf.exists():
        return {}
    try:
        doc = tomlkit.loads(mf.read_text(encoding="utf-8"))
        return dict(doc.get("package", {}))
    except Exception as exc:
        log.warning("paradigm manifest parse error %s: %s", mf, exc)
        return {}


def _is_paradigm_pkg(path: Path) -> bool:
    return (
        path.is_dir()
        and not path.name.startswith("__")
        and (
            (path / "__init__.py").exists()
            or (path / _MANIFEST).exists()
        )
    )


def list_paradigms() -> List[ParadigmInfo]:
    """Return discovered paradigms; user-area packages shadow vendor packages
    with the same ``id``."""
    seen_ids: set[str] = set()
    results: List[ParadigmInfo] = []

    for root in paradigm_search_roots():
        for entry in sorted(os.listdir(root)):
            pkg_dir = root / entry
            if not _is_paradigm_pkg(pkg_dir):
                continue

            manifest = _read_manifest(pkg_dir)

            # id: manifest > directory name
            pkg_id = manifest.get("id") or entry
            if pkg_id in seen_ids:
                # Earlier root (user area) already registered this id — skip.
                continue
            seen_ids.add(pkg_id)

            # TODO(P1): if manifest is empty (parse failure or absent) and
            # plugin_type falls back to pkg_id (the dir name), the engine
            # registry lookup will fail at runtime with no clear error.
            # Consider: skip the entry and log.error() instead of degrading
            # silently to an invalid plugin_type.
            results.append(
                ParadigmInfo(
                    id=pkg_id,
                    plugin_type=manifest.get("plugin_type") or pkg_id,
                    display_name=manifest.get("display_name") or pkg_id,
                    description=manifest.get("description"),
                    version=manifest.get("version"),
                    source=manifest.get("source", "user"),
                )
            )

    return results


def list_paradigm_names() -> List[str]:
    """Compatibility shim — returns plugin_type strings (for legacy callers)."""
    return [p.plugin_type for p in list_paradigms()]
