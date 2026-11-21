"""Application preparation stays scoped and precedes runtime replacement."""
from copy import deepcopy
from unittest.mock import Mock

import pytest

from omnibci.application.services.engine_service import NeuroEngineService
from tests.application.test_runtime_apply_failures import harness, apply


def test_preparation_copies_inputs_and_excludes_externally_owned_outputs():
    def prepare(snapshot, modules):
        assert modules == ('processing',)
        snapshot['PLUGINS.artifacts'] = {'processing.decoder': {'pin': 'checked'}}
        return snapshot
    callback = Mock(side_effect=prepare)
    engine = NeuroEngineService(Mock(), plugin_preparer=callback)
    selected = {'PLUGINS.bindings': [], 'STIMULATION.auto_spawn': False, 'ACTUATION.auto_spawn': False}
    original = deepcopy(selected)
    prepared = engine.prepare_plugins(selected, ('processing', 'stimulation', 'actuation'))
    assert prepared['PLUGINS.artifacts']['processing.decoder']['pin'] == 'checked'
    assert selected == original
    callback.assert_called_once()


def test_module_snapshot_is_prepared_before_returning_it_to_a_host():
    callback = Mock(side_effect=lambda snapshot, modules: {**snapshot, 'PLUGINS.artifacts': {'slot': 'checked'}})
    engine = NeuroEngineService(Mock(), plugin_preparer=callback)
    engine._applied_snapshot = {'PLUGINS.bindings': [], 'CONFIG.engine_revision': 'engine'}
    engine._module_snapshot_provider = lambda module: {
        'PLUGINS.bindings': [], 'CONFIG.engine_revision': 'engine',
        'CONFIG.module_revisions': {module: 'revision'},
    }
    selected = engine.selected_module_snapshot('acquisition')
    assert selected['PLUGINS.artifacts'] == {'slot': 'checked'}
    assert callback.call_args.args[1] == ('acquisition',)
    assert 'PLUGINS.artifacts' not in engine.applied_snapshot


def test_missing_preparation_dependency_fails_explicitly_for_selected_code():
    engine = NeuroEngineService(Mock())
    with pytest.raises(RuntimeError, match='preparation'):
        engine.prepare_plugins({'PLUGINS.bindings': [{'slot': 'acquisition.source'}]}, ('acquisition',))


@pytest.mark.asyncio
async def test_failed_artifact_preparation_preserves_the_running_source(harness, monkeypatch):
    h = harness()
    before = deepcopy(h.engine.applied_snapshot)
    def reject(snapshot, modules):
        raise RuntimeError('artifact integrity failed')
    monkeypatch.setattr(h.engine, 'prepare_plugins', reject, raising=False)
    result = await apply(h)
    assert result['status'] == 'failed' and 'artifact integrity failed' in result['error']
    assert h.engine.applied_snapshot == before
    assert not any(item[0] in {'stop_module', 'stop_graph'} for item in h.log)
    assert not h.engine.admission.recovery_status
