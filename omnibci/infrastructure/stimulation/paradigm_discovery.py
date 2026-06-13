"""
Paradigm discovery — thin shim over plugin_catalog.

All discovery logic now lives in
``omnibci.infrastructure.plugins.plugin_catalog``.
This module is kept for backwards compatibility with existing call sites.
"""
from typing import List

from omnibci.infrastructure.plugins.plugin_catalog import list_plugins
from omnibci.presentation.schemas.stimulation import ParadigmInfo


def list_paradigms() -> List[ParadigmInfo]:
    """Return discovered paradigm plugins."""
    return list_plugins("paradigm")


def list_paradigm_names() -> List[str]:
    """Compatibility shim — returns plugin_type strings (for legacy callers)."""
    return [p.plugin_type for p in list_paradigms()]
