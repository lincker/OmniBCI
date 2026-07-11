"""
Plugin directory resolution — cross-platform.

Two roots are searched in order; user-area first so user plugins shadow builtins:

    1. User area
         macOS : ~/Library/Application Support/OmniBCI/plugins
         Linux : ~/.local/share/OmniBCI/plugins
         Windows: %APPDATA%\\OmniBCI\\OmniBCI\\plugins
       Override (in priority order):
         a) OMNIBCI_PLUGIN_DIR environment variable
         b) PLUGINS.user_dir in settings.toml

    2. Vendor area — omnibci/infrastructure/plugins/ inside the package.
         Read-only; tracked by git / package installer.
         Upgraded by updating the application, not by the user.

This module is import-only — no code execution, no OpenGL.
"""
import logging
import os
from pathlib import Path
from typing import Optional

import omnibci

log = logging.getLogger(__name__)

_APP_NAME = "OmniBCI"
_APP_AUTHOR = "OmniBCI"  # Windows roaming path segment


def vendor_plugins_dir() -> Path:
    """Absolute path to the vendor (builtin) plugins directory."""
    pkg_root = Path(next(iter(omnibci.__path__)))
    return pkg_root / "infrastructure" / "plugins"


def user_plugins_dir() -> Optional[Path]:
    """Resolve the user plugins directory; return None if undeterminable.

    Resolution order
    ----------------
    1. ``OMNIBCI_PLUGIN_DIR`` environment variable (useful for CI / dev).
    2. ``PLUGINS.user_dir`` setting from settings.toml.
    3. The platform-specific default from *platformdirs*:
         macOS   ~/Library/Application Support/OmniBCI/plugins
         Linux   ~/.local/share/OmniBCI/plugins          (XDG_DATA_HOME honoured)
         Windows %APPDATA%\\OmniBCI\\OmniBCI\\plugins

    The directory is **not created** here — callers that need to write should
    mkdir themselves.
    """
    # 1. Env override
    env = os.environ.get("OMNIBCI_PLUGIN_DIR")
    if env:
        return Path(env)

    # 2. settings.toml override (lazy import to avoid circular dependency)
    try:
        from omnibci.conf import settings  # noqa: PLC0415
        raw = settings._raw_get(settings._data, "PLUGINS.user_dir")
        if raw:
            return Path(raw)
    except Exception:
        pass

    # 3. platformdirs (optional dependency — graceful degradation)
    try:
        from platformdirs import user_data_dir  # noqa: PLC0415
        return Path(user_data_dir(_APP_NAME, _APP_AUTHOR)) / "plugins"
    except ImportError:
        log.warning(
            "platformdirs not installed; user plugin directory unavailable. "
            "Install it with: pip install platformdirs"
        )
        return None


def _search_roots(subpath: str) -> list[Path]:
    """Return [user/<subpath>, vendor/<subpath>], omitting missing dirs."""
    roots: list[Path] = []
    user = user_plugins_dir()
    if user is not None:
        up = user / subpath
        if up.is_dir():
            roots.append(up)
    vp = vendor_plugins_dir() / subpath
    if vp.is_dir():
        roots.append(vp)
    return roots


def paradigm_search_roots() -> list[Path]:
    """Return paradigm search roots: [user_paradigms, vendor_paradigms].

    User area is listed first so its plugins shadow vendor plugins with the
    same id.  Missing directories are silently omitted.
    """
    return _search_roots("paradigms")


def decoder_search_roots() -> list[Path]:
    """Return decoder search roots: [user_decoders, vendor_decoders].

    Vendor decoders live in plugins/algorithms/decoders/.
    User-installed decoders live in <user_plugins>/algorithms/decoders/.
    """
    return _search_roots("algorithms/decoders")


def acquisition_search_roots() -> list[Path]:
    """Return acquisition plugin search roots: [user_acquisition, vendor_acquisition]."""
    return _search_roots("acquisition")


def effector_search_roots() -> list[Path]:
    """Return effector plugin search roots: [user_effector, vendor_effector]."""
    return _search_roots("effector")
