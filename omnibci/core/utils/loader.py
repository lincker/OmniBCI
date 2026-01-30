#!/usr/bin/env python
# -*- coding: utf-8 -*-
# @Author   : lincker <haolin.d.wu@gmail.com>
# @Date     : 2026-01-28 05:58
import sys
import logging
import importlib
from pathlib import Path

logger = logging.getLogger(__name__)

class PluginLoader:

    @staticmethod
    def load_from_dir(plugin_dir: str, base_package: str = "omnibci.plugins") -> None:
        root_path = Path(plugin_dir).resolve()
        if not root_path.exists():
            logger.warning(f"[PluginLoader] Directory not found: {root_path}")
            return

        if str(root_path.parent) not in sys.path:
            sys.path.insert(0, str(root_path.parent))

        count = 0
        for file_path in root_path.rglob("*.py"):
            try:
                relative_path = file_path.relative_to(root_path)
            except ValueError:
                continue

            if file_path.name == "__init__.py":
                module_parts = relative_path.parent.parts
            else:
                module_parts =  relative_path.with_suffix("").parts

            if not module_parts:
                continue

            module_name = f"{base_package}.{'.'.join(module_parts)}"

            if module_name in sys.modules:
                continue

            # dynamic load
            try:
                importlib.import_module(module_name)
                count += 1
                logger.debug(f"Loaded plugin module: {module_name}")
            except Exception as e:
                logger.error(f"Failed to load module '{module_name}': {e}", exc_info=True)

        logger.info(f"[PluginLoader] Scanned '{plugin_dir}' - Loaded {count} modules.")