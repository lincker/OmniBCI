"""Constructor dispatch is explicit and never retries plugin failures."""
from textwrap import dedent

import pytest

from omnibci.infrastructure.plugin_management.loader import PluginLoader, PluginLoadError
from omnibci.infrastructure.plugin_management.catalog import PluginCatalog, PluginRoot
from omnibci.sdk import Effector
from omnibci.application.plugin_management.identity import PluginSelection


def descriptor(tmp_path, body="self.parameters = parameters; self.resource_root = resource_root"):
    (tmp_path / "manifest.toml").write_text(dedent('''
        manifest_version = 1
        [package]
        domain = "actuation"
        id = "example"
        publisher = "test"
        version = "1.0.0"
        api_version = "1"
        [[contributions]]
        id = "output"
        kind = "effector"
        entrypoint = "output:Output"
        [contributions.payload_schemas]
        command = "command.schema.json"
    '''), encoding="utf-8")
    (tmp_path / "command.schema.json").write_text('{"type":"object","properties":{},"additionalProperties":false}')
    (tmp_path / "output.py").write_text(dedent(f'''
        from omnibci.sdk import Effector
        class Output(Effector):
            def __init__(self, *, parameters, resource_root):
                {body}
            def setup(self): pass
            def teardown(self): pass
            def execute(self, command): pass
            def quiesce(self): pass
    '''), encoding="utf-8")
    catalog = PluginCatalog({"effector": lambda: [PluginRoot(tmp_path, "user", 0)]})
    return catalog.resolve_selection("effector", PluginSelection("test.example", "output", "1.0.0"))


def test_api_one_needs_no_registry_alias_and_copies_parameters(tmp_path):
    selected = descriptor(tmp_path)
    parameters = {"mapping": [1, 2]}
    plugin = PluginLoader.instantiate(selected, Effector, parameters=parameters)
    plugin.parameters["mapping"].append(3)
    assert parameters == {"mapping": [1, 2]}
    assert plugin.resource_root == tmp_path


@pytest.mark.parametrize("kwargs", [
    {}, {"parameters": {}, "context": {}},
    {"parameters": []},
    {"parameters": {"value": float("nan")}},
    {"parameters": {}, "unexpected": True},
    {"parameters": {"values": (1, 2)}}, {"parameters": {1: "value"}},
    {"parameters": {"huge": "x" * (1024 * 1024)}},
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
        PluginLoader.instantiate(selected, Effector, parameters={})
    assert marker.read_text() == "x"
