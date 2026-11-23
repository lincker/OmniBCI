"""Runtime loading consumes private prepared pins instead of rediscovering code."""
from copy import deepcopy
import json
from unittest.mock import Mock

import pytest

from omnibci.application.configuration.definitions import merge_module_settings
from omnibci.application.configuration.models import ProfileBinding
from omnibci.domain.ports.effector import Effector
from omnibci.infrastructure.plugin_management.loader import PluginLoadError
from omnibci.infrastructure.plugin_management.catalog import PluginCatalog
from omnibci.infrastructure.plugin_management.preparation import PluginArtifactPreparer
from omnibci.infrastructure.plugin_management.runtime import load_configured_plugin
from tests.infrastructure.test_plugin_preparation import installed
from tests.plugin_configuration_helpers import builtin_selection


def snapshot(binding):
    return {'PLUGINS.bindings': [binding.to_dict()], 'CONFIG.generation': 'run',
            'CONFIG.module_revisions': {'actuation': 'revision'}}


def pinned(store, binding):
    result = snapshot(binding)
    result['PLUGINS.artifacts'] = {binding.slot: PluginArtifactPreparer(store).prepare(binding)}
    return result


def test_host_consumes_prepared_json_without_global_catalog_search(installed, monkeypatch):
    store, binding, _ = installed
    prepared = pinned(store, binding)
    monkeypatch.setattr(PluginCatalog, 'resolve_selection', lambda *a: pytest.fail('Global rediscovery'))
    loader = Mock()
    load_configured_plugin(json.loads(json.dumps(prepared)), binding.slot, binding.kind, Effector, loader=loader)
    descriptor = loader.instantiate.call_args.args[0]
    assert descriptor.location == store.packages_dir / 'acme.device/1.0.0'
    assert loader.instantiate.call_args.kwargs == {'parameters': {'gain': 1.0}}


def test_runtime_rejects_unprepared_binding_even_when_the_builtin_is_installed():
    binding = ProfileBinding('actuation.effector', 'effector', None, builtin_selection('effector', 'logger'), {})
    loader = Mock()
    with pytest.raises(PluginLoadError, match='prepared artifact'):
        load_configured_plugin(snapshot(binding), binding.slot, binding.kind, Effector, loader=loader)
    loader.instantiate.assert_not_called()


@pytest.mark.parametrize('change', ['code', 'parameters', 'removed_pin'])
def test_stale_preparation_never_reaches_constructor(installed, monkeypatch, change):
    store, binding, _ = installed
    prepared = pinned(store, binding)
    monkeypatch.setenv('OMNIBCI_PLUGIN_DIR', str(store.plugin_dir))
    if change == 'code':
        (store.packages_dir / 'acme.device/1.0.0/driver.py').write_text('changed')
    elif change == 'parameters':
        prepared['PLUGINS.bindings'][0]['parameters']['gain'] = 2.0
    else:
        prepared['PLUGINS.artifacts'].clear()
    loader = Mock()
    with pytest.raises(PluginLoadError):
        load_configured_plugin(prepared, binding.slot, binding.kind, Effector, loader=loader)
    loader.instantiate.assert_not_called()


def test_snapshot_preparation_is_scoped_copied_and_reuses_pins_without_rediscovery(installed, monkeypatch):
    store, binding, _ = installed
    candidate = snapshot(binding)
    candidate['PLUGINS.bindings'].append({'slot': 'processing.decoder', 'invalid': 'dormant'})
    original = deepcopy(candidate)
    preparer = PluginArtifactPreparer(store)
    prepared = preparer.prepare_snapshot(candidate, ('actuation',))
    assert candidate == original and 'PLUGINS.artifacts' not in candidate
    assert set(prepared['PLUGINS.artifacts']) == {binding.slot}
    monkeypatch.setattr(PluginCatalog, 'resolve_selection', lambda *a: pytest.fail('Repeated resolution'))
    again = preparer.prepare_snapshot(prepared, ('actuation',))
    assert again == prepared and again is not prepared
    again['PLUGINS.bindings'][0]['parameters']['gain'] = 9
    assert prepared['PLUGINS.bindings'][0]['parameters']['gain'] == 1


def test_snapshot_preparation_does_not_silently_repin_changed_bytes(installed):
    store, binding, _ = installed
    prepared = pinned(store, binding)
    (store.packages_dir / 'acme.device/1.0.0/driver.py').write_text('changed')
    with pytest.raises(PluginLoadError, match='content'):
        PluginArtifactPreparer(store).prepare_snapshot(prepared, ('actuation',))


@pytest.mark.parametrize('selected_pins', [None, {}, {'processing.decoder': {'pin': 'new'}}])
def test_module_merge_replaces_only_its_own_artifact_pins(selected_pins):
    before = {'PLUGINS.bindings': [], 'PLUGINS.artifacts': {
        'acquisition.source': {'pin': 'source'}, 'processing.decoder': {'pin': 'old'},
        'processing.filter.removed': {'pin': 'filter'},
    }}
    selected = {'PLUGINS.bindings': []}
    if selected_pins is not None:
        selected['PLUGINS.artifacts'] = selected_pins
    merged = merge_module_settings(before, selected, ('processing',))
    assert merged['PLUGINS.artifacts'] == {'acquisition.source': {'pin': 'source'}, **(selected_pins or {})}
    merged['PLUGINS.artifacts']['acquisition.source']['pin'] = 'edited'
    assert before['PLUGINS.artifacts']['acquisition.source']['pin'] == 'source'


def test_preparing_empty_foundation_never_resolves_a_contribution(installed, monkeypatch):
    store, _, _ = installed
    monkeypatch.setattr(PluginCatalog, 'resolve_selection', lambda *a: pytest.fail('No selected contribution'))
    assert PluginArtifactPreparer(store).prepare_snapshot({'PLUGINS.bindings': []}, ()) == {
        'PLUGINS.bindings': [], 'PLUGINS.artifacts': {},
    }
