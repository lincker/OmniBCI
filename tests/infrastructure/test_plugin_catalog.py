"""Plugin discovery contract tests."""

from omnibci.infrastructure.plugin_management.catalog import PluginCatalog


def test_effector_catalog_includes_builtin_logger(monkeypatch, tmp_path):
    # Isolate discovery from any plugins installed in the user's real data dir.
    monkeypatch.setenv("OMNIBCI_PLUGIN_DIR", str(tmp_path / "plugins"))

    effectors = {plugin.identifier: plugin for plugin in PluginCatalog().list_contributions("effector")}

    assert "omnibci.logger" in effectors
    assert effectors["omnibci.logger"].plugin_type == "logger"
    assert effectors["omnibci.logger"].source == "builtin"
