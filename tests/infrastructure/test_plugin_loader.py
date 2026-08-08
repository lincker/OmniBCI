"""Legacy directory-based plugin loader contracts."""

from __future__ import annotations

import logging
import sys

import pytest

from omnibci.infrastructure.plugins import loader as loader_module
from omnibci.infrastructure.plugins.loader import PluginLoader


def _plugin_tree(tmp_path):
    parent = tmp_path / "isolated"
    plugin_dir = parent / "plugins"
    plugin_dir.mkdir(parents=True)
    return parent, plugin_dir


def test_missing_directory_warns_without_changing_import_path(tmp_path, caplog):
    missing = tmp_path / "missing"
    path_before = list(sys.path)

    with caplog.at_level(logging.WARNING, logger=loader_module.__name__):
        PluginLoader.load_from_dir(str(missing))

    assert sys.path == path_before
    assert "Directory not found" in caplog.text


def test_import_failure_is_logged_and_does_not_abort_scan(
    tmp_path,
    monkeypatch,
    caplog,
):
    parent, plugin_dir = _plugin_tree(tmp_path)
    (plugin_dir / "broken.py").write_text("", encoding="utf-8")
    (plugin_dir / "working.py").write_text("", encoding="utf-8")
    imported = []

    def import_module(module_name):
        imported.append(module_name)
        if module_name.endswith(".broken"):
            raise RuntimeError("broken plugin")

    monkeypatch.setattr(loader_module.importlib, "import_module", import_module)
    path_before = list(sys.path)

    with caplog.at_level(logging.DEBUG, logger=loader_module.__name__):
        PluginLoader.load_from_dir(str(plugin_dir), base_package="test_plugins")

    assert set(imported) == {"test_plugins.broken", "test_plugins.working"}
    assert "Failed to load module 'test_plugins.broken': broken plugin" in caplog.text
    assert "Loaded 1 modules" in caplog.text
    assert str(parent) not in sys.path
    assert sys.path == path_before


def test_preexisting_import_path_is_preserved(tmp_path, monkeypatch):
    parent, plugin_dir = _plugin_tree(tmp_path)
    (plugin_dir / "working.py").write_text("", encoding="utf-8")
    monkeypatch.syspath_prepend(str(parent))
    monkeypatch.setattr(loader_module.importlib, "import_module", lambda _name: None)
    path_before = list(sys.path)

    PluginLoader.load_from_dir(str(plugin_dir), base_package="test_plugins")

    assert sys.path == path_before


def test_base_exception_restores_added_import_path(tmp_path, monkeypatch):
    parent, plugin_dir = _plugin_tree(tmp_path)
    (plugin_dir / "interrupted.py").write_text("", encoding="utf-8")

    def interrupt_import(_module_name):
        raise KeyboardInterrupt

    monkeypatch.setattr(loader_module.importlib, "import_module", interrupt_import)
    path_before = list(sys.path)

    with pytest.raises(KeyboardInterrupt):
        PluginLoader.load_from_dir(str(plugin_dir), base_package="test_plugins")

    assert str(parent) not in sys.path
    assert sys.path == path_before
