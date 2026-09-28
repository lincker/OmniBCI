"""Constructor dispatch is explicit and never retries plugin failures."""
from textwrap import dedent

import pytest

from omnibci.infrastructure.plugins.loader import PluginLoader, PluginLoadError
from omnibci.infrastructure.plugins.plugin_catalog import PluginCatalog, PluginRoot
from omnibci.sdk import Effector, PluginContext


def descriptor(tmp_path, body="self.parameters = parameters; self.context = context"):
    (tmp_path / "manifest.toml").write_text(dedent('''
        manifest_version = 2
        [package]
        id = "example"
        publisher = "test"
        version = "1.0.0"
        api_version = "2"
        [[contributions]]
        id = "output"
        kind = "effector"
        entrypoint = "output:Output"
    '''), encoding="utf-8")
    (tmp_path / "output.py").write_text(dedent(f'''
        from omnibci.sdk import Effector
        class Output(Effector):
            def __init__(self, *, parameters, context):
                {body}
            def setup(self): pass
            def teardown(self): pass
            def play(self, command): pass
    '''), encoding="utf-8")
    catalog = PluginCatalog({"effector": lambda: [PluginRoot(tmp_path, "user", 0)]})
    return catalog.resolve_legacy("effector", "output")


def test_api_two_needs_no_registry_alias_and_copies_parameters(tmp_path):
    selected = descriptor(tmp_path)
    parameters = {"mapping": [1, 2]}
    context = PluginContext(sample_rate=250, config_revision="revision-1", resource_root="ignored")
    plugin = PluginLoader.instantiate(selected, Effector, parameters=parameters, context=context)
    plugin.parameters["mapping"].append(3)
    assert parameters == {"mapping": [1, 2]}
    assert plugin.context.sample_rate == 250
    assert plugin.context.resource_root == str(tmp_path)
    assert context.resource_root == "ignored"


@pytest.mark.parametrize("kwargs", [
    {}, {"parameters": {}, "context": {}},
    {"parameters": [], "context": PluginContext()},
    {"parameters": {"value": float("nan")}, "context": PluginContext()},
    {"parameters": {}, "context": PluginContext(), "unexpected": True},
])
def test_invalid_constructor_inputs_fail_before_import(tmp_path, monkeypatch, kwargs):
    selected = descriptor(tmp_path)
    monkeypatch.setattr(PluginLoader, "load_entrypoint", lambda *a: pytest.fail("imported"))
    with pytest.raises(PluginLoadError):
        PluginLoader.instantiate(selected, Effector, **kwargs)


def test_constructor_error_is_not_retried(tmp_path):
    marker = tmp_path / "attempts"
    selected = descriptor(tmp_path, body=(
        f"from pathlib import Path; p = Path({str(marker)!r}); "
        "p.write_text(p.read_text() + 'x' if p.exists() else 'x'); "
        "raise TypeError('plugin bug')"
    ))
    with pytest.raises(PluginLoadError, match="plugin bug"):
        PluginLoader.instantiate(selected, Effector, parameters={}, context=PluginContext())
    assert marker.read_text() == "x"
