"""Plugin identity is independent of Configuration and executable plugins."""
from dataclasses import FrozenInstanceError
from importlib import import_module
from pathlib import Path
import pickle
import subprocess
import sys

import pytest


def test_identity_import_loads_no_other_project_modules():
    result = subprocess.run([sys.executable, "-B", "-c", """
import sys
from omnibci.application.plugin_identity import PluginSelection, PLUGIN_KINDS

assert PluginSelection('acme.test', 'main', '1.0.0').version == '1.0.0'
assert 'effector' in PLUGIN_KINDS
assert {name for name in sys.modules if name.startswith('omnibci.')} == {
    'omnibci.application', 'omnibci.application.plugin_identity',
}
"""], cwd=Path(__file__).resolve().parents[2], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("module_name", [
    "omnibci.application.configuration.models",
    "omnibci.infrastructure.plugins.descriptors",
])
def test_previous_owners_do_not_export_identity(module_name):
    module = import_module(module_name)
    assert not hasattr(module, "PluginSelection")
    assert not hasattr(module, "PLUGIN_KINDS")


def test_selection_round_trips_and_cannot_be_mutated():
    from omnibci.application.plugin_identity import PluginSelection

    data = {"package_id": "acme.test", "contribution_id": "main", "version": "1.0.0"}
    selection = PluginSelection.from_dict(data)
    assert selection.to_dict() == data
    assert pickle.loads(pickle.dumps(selection)) == selection
    assert hash(selection) == hash(PluginSelection(**data))
    data["version"] = "2.0.0"
    selection.to_dict()["version"] = "3.0.0"
    assert selection.version == "1.0.0"
    with pytest.raises(FrozenInstanceError):
        selection.version = "4.0.0"


@pytest.mark.parametrize("version", ["1", "0.0.0", "1beta", "1.2.3-rc.1+build.4", "1" * 128])
def test_existing_exact_version_spellings_are_preserved(version):
    from omnibci.application.plugin_identity import PluginSelection

    selection = PluginSelection("acme.test", "main", version)
    assert PluginSelection.from_dict(selection.to_dict()) == selection


@pytest.mark.parametrize("version", [None, "", "*", ">=1", "1.0.*", " 1.0.0", "1.0.0.", "1" * 129, "１.0.0"])
def test_selections_require_exact_versions(version):
    from omnibci.application.plugin_identity import PluginSelection

    with pytest.raises(ValueError) as raised:
        PluginSelection("acme.test", "main", version)
    assert type(raised.value) is ValueError


@pytest.mark.parametrize(("field", "value"), [
    ("package_id", None), ("package_id", True), ("package_id", ""),
    ("package_id", " acme.test"), ("contribution_id", 1), ("contribution_id", "main "),
])
def test_selection_fields_reject_coercion_or_trimming(field, value):
    from omnibci.application.plugin_identity import PluginSelection

    data = {"package_id": "acme.test", "contribution_id": "main", "version": "1.0.0", field: value}
    with pytest.raises(ValueError) as raised:
        PluginSelection.from_dict(data)
    assert type(raised.value) is ValueError


@pytest.mark.parametrize("data", [
    None, [], {}, {"package_id": "acme.test", "version": "1.0.0"},
    {"package_id": "acme.test", "contribution_id": "main", "version": "1.0.0", "kind": "effector"},
    {"package_id": "acme.test", "contribution_id": "main", "version": "1.0.0", 0: "extra"},
])
def test_selection_documents_require_exact_fields(data):
    from omnibci.application.plugin_identity import PluginSelection

    with pytest.raises(ValueError, match="fields") as raised:
        PluginSelection.from_dict(data)
    assert type(raised.value) is ValueError
