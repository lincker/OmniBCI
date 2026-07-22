"""Plugin discovery contract tests."""

from omnibci.infrastructure.plugins.plugin_catalog import list_plugins


def test_effector_catalog_includes_builtin_logger(monkeypatch, tmp_path):
    # Isolate discovery from any plugins installed in the user's real data dir.
    monkeypatch.setenv("OMNIBCI_PLUGIN_DIR", str(tmp_path / "plugins"))

    effectors = {plugin.id: plugin for plugin in list_plugins("effector")}

    assert "logger" in effectors
    assert effectors["logger"].plugin_type == "logger"
