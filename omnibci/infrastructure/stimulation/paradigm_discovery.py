"""
Paradigm discovery for the API process.

Listing paradigms by importing the paradigm plugins would pull in OpenGL/GLFW
(the renderer is constructed at import time), which must NOT happen in the API
process.  So discovery is filesystem-based: each sub-package under
``infrastructure/plugins/paradigms/`` is one paradigm.

This yields the paradigm *directory names* (e.g. ``classic40SSVEP``) without
importing anything.  Mapping a directory name to the registry ``plugin_type``
used to actually start it is a follow-up (would need an OpenGL-free manifest).
"""

import os
from typing import List

import omnibci


def _paradigms_dir() -> str:
    return os.path.join(next(iter(omnibci.__path__)), "infrastructure", "plugins", "paradigms")


def list_paradigm_names() -> List[str]:
    """Return sorted paradigm package names, or [] if the directory is absent."""
    pdir = _paradigms_dir()
    if not os.path.isdir(pdir):
        return []
    names = []
    for entry in sorted(os.listdir(pdir)):
        if entry.startswith("__"):
            continue
        sub = os.path.join(pdir, entry)
        if os.path.isdir(sub) and os.path.exists(os.path.join(sub, "__init__.py")):
            names.append(entry)
    return names
