"""
Unified plugin discovery — all categories.

Scans vendor and user plugin directories for packages that contain a
``manifest.toml``.  No Python code is imported; discovery is purely
filesystem + TOML.

Categories
----------
paradigm    plugins/paradigms/
decoder     plugins/algorithms/decoders/
acquisition plugins/acquisition/
effector    plugins/effectors/

User-area plugins (same id) shadow vendor plugins.
"""
import logging
import os
from pathlib import Path
from typing import List

import tomlkit

from omnibci.infrastructure.plugins.dirs import (
    acquisition_search_roots,
    decoder_search_roots,
    effector_search_roots,
    paradigm_search_roots,
)
from omnibci.application.queries.plugin_queries import PluginInfo

log = logging.getLogger(__name__)

_MANIFEST = "manifest.toml"

VALID_CATEGORIES = frozenset({"paradigm", "decoder", "acquisition", "effector"})

_ROOTS = {
    "paradigm":    paradigm_search_roots,
    "decoder":     decoder_search_roots,
    "acquisition": acquisition_search_roots,
    "effector":    effector_search_roots,
}


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _read_manifest(pkg_dir: Path) -> dict:
    """Parse manifest.toml; return {"package": {...}, "params": {...}}.

    Returns {} on any error or when no manifest exists.
    """
    mf = pkg_dir / _MANIFEST
    if not mf.exists():
        return {}
    try:
        doc = tomlkit.loads(mf.read_text(encoding="utf-8"))
        return {
            "package": dict(doc.get("package", {})),
            "params": dict(doc.get("params", {})),
            # [controls.<name>] nested tables → {name: {label, type, default, ...}}
            "controls": {k: dict(v) for k, v in dict(doc.get("controls", {})).items()},
        }
    except Exception as exc:
        log.warning("manifest parse error %s: %s", mf, exc)
        return {}


def _is_plugin_pkg(path: Path) -> bool:
    """True if path looks like a plugin package directory."""
    return (
        path.is_dir()
        and not path.name.startswith("__")
        and (
            (path / "__init__.py").exists()
            or (path / _MANIFEST).exists()
        )
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def list_plugins(category: str) -> List[PluginInfo]:
    """Return discovered plugins for *category*; user-area shadows vendor.

    Parameters
    ----------
    category:
        One of ``VALID_CATEGORIES``.

    Raises
    ------
    ValueError
        If *category* is not recognised.
    """
    if category not in VALID_CATEGORIES:
        raise ValueError(f"Unknown plugin category: {category!r}. "
                         f"Valid: {sorted(VALID_CATEGORIES)}")

    roots_fn = _ROOTS[category]
    seen: set[str] = set()
    results: List[PluginInfo] = []

    for root in roots_fn():
        for entry in sorted(os.listdir(root)):
            pkg_dir = root / entry
            if not _is_plugin_pkg(pkg_dir):
                continue

            manifest = _read_manifest(pkg_dir)
            package = manifest.get("package", {})
            params = manifest.get("params", {})
            controls = manifest.get("controls", {})

            # TODO(P1): if manifest is empty and plugin_type falls back to
            # pkg_id (dir name), the engine registry lookup will fail at
            # runtime. Consider skipping + log.error() instead of degrading.
            pkg_id = package.get("id") or entry
            if pkg_id in seen:
                continue
            seen.add(pkg_id)

            results.append(
                PluginInfo(
                    id=pkg_id,
                    plugin_type=package.get("plugin_type") or pkg_id,
                    display_name=package.get("display_name") or pkg_id,
                    description=package.get("description"),
                    version=package.get("version"),
                    source=package.get("source", "user"),
                    params=params,
                    controls=controls,
                )
            )

    return results
