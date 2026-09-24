"""Configuration storage uses platform defaults or an explicit override."""
from pathlib import Path
from unittest.mock import Mock

import pytest

from omnibci.application.config_errors import ConfigProfileValidationError
from omnibci.infrastructure import config_paths
from omnibci.infrastructure.repositories.plugin_profile_repository import TomlPluginProfileRepository


@pytest.mark.parametrize("override", [None, ""])
def test_platform_default_is_used_without_nonempty_override(tmp_path, monkeypatch, override):
    if override is None:
        monkeypatch.delenv("OMNIBCI_CONFIG_DIR", raising=False)
    else:
        monkeypatch.setenv("OMNIBCI_CONFIG_DIR", override)
    platform_path = tmp_path / "platform-config" / "OmniBCI"
    platform_default = Mock(return_value=platform_path)
    monkeypatch.setattr(config_paths, "user_config_path", platform_default)

    assert config_paths.config_root() == platform_path
    platform_default.assert_called_once_with("OmniBCI", appauthor=False, roaming=False)
    assert not platform_path.exists()


def test_absolute_override_is_resolved_without_creating_directory(tmp_path, monkeypatch):
    monkeypatch.setenv("OMNIBCI_CONFIG_DIR", str(tmp_path / "unused" / ".." / "config"))

    assert config_paths.config_root() == tmp_path / "config"
    assert list(tmp_path.iterdir()) == []


def test_override_expands_user_directory(monkeypatch):
    monkeypatch.setenv("OMNIBCI_CONFIG_DIR", "~/omnibci-config-path-test")

    assert config_paths.config_root() == (Path.home() / "omnibci-config-path-test").resolve()


def test_relative_override_is_rejected(monkeypatch):
    monkeypatch.setenv("OMNIBCI_CONFIG_DIR", "relative/config")

    with pytest.raises(ConfigProfileValidationError, match="OMNIBCI_CONFIG_DIR must be an absolute path"):
        config_paths.config_root()


def test_repository_uses_override_and_explicit_root_precedence(tmp_path, monkeypatch):
    override = tmp_path / "shared"
    explicit = tmp_path / "explicit"
    monkeypatch.setenv("OMNIBCI_CONFIG_DIR", str(override))

    assert TomlPluginProfileRepository().root == override
    monkeypatch.setenv("OMNIBCI_CONFIG_DIR", "invalid-relative-override")
    assert TomlPluginProfileRepository(explicit).root == explicit
    assert list(tmp_path.iterdir()) == []
