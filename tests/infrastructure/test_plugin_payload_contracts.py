"""Runtime payload schemas belong to the selected role and stay host-owned."""
import json
from unittest.mock import Mock

import pytest
from tests.plugin_configuration_helpers import prepared_snapshot

from omnibci.application.commands.stimulation_commands import StartStimTrialCommand
from omnibci.infrastructure.actuation.actuator import Actuator
from omnibci.application.commands.actuation_commands import validate_actuation_command
from tests.actuation_helpers import actuation_payload
from omnibci.infrastructure.plugins.manifest import PluginManifestError, parse_manifest
from omnibci.infrastructure.stimulation.stimulator import Stimulator
from omnibci.presentation.schemas.stimulation import StartTrialRequest
from tests.infrastructure.test_output_plugin_configuration import binding
from tests.infrastructure.test_package_installer import _manifest


TRIAL_SCHEMA = {'type': 'object', 'properties': {'event_id': {'type': 'integer', 'minimum': 0, 'maximum': 40}},
                'required': ['event_id'], 'additionalProperties': False}


def parse_payload_package(tmp_path, kind, declaration):
    document = _manifest()
    contribution = document['contributions'][0]
    contribution['kind'] = kind
    if declaration is not None:
        contribution['payload_schemas'] = declaration
    if kind == 'decoder':
        contribution['channel_requirements'] = {}
    return parse_manifest(document, tmp_path, source='user', precedence=0, host_version='1.0.0')


@pytest.mark.parametrize('kind,fields', [('paradigm', ('trial', 'result')), ('effector', ('command',))])
def test_payload_schema_paths_are_static_local_role_declarations(tmp_path, kind, fields):
    (tmp_path / 'payload.json').write_text(json.dumps(TRIAL_SCHEMA))
    (tmp_path / 'driver.py').write_text("raise AssertionError('Static validation imported code')\n")
    descriptor, = parse_payload_package(tmp_path, kind, dict.fromkeys(fields, 'payload.json'))
    assert set(descriptor.payload_schemas) == set(fields)
    assert all(schema == TRIAL_SCHEMA for schema in descriptor.payload_schemas.values())


@pytest.mark.parametrize('kind,declaration', [
    ('paradigm', None), ('paradigm', {}), ('paradigm', {'trial': 'payload.json'}),
    ('paradigm', {'trial': 'payload.json', 'result': 'payload.json', 'command': 'payload.json'}),
    ('effector', None), ('effector', {}), ('effector', {'result': 'payload.json'}),
])
def test_output_roles_require_exact_payload_schema_declarations(tmp_path, kind, declaration):
    (tmp_path / 'payload.json').write_text(json.dumps(TRIAL_SCHEMA))
    with pytest.raises(PluginManifestError, match='payload_schemas'):
        parse_payload_package(tmp_path, kind, declaration)


@pytest.mark.parametrize('kind', ['acquisition', 'trigger', 'decoder', 'filter', 'epoch_detector'])
def test_other_roles_cannot_declare_runtime_payload_schemas(tmp_path, kind):
    with pytest.raises(PluginManifestError, match='payload_schemas'):
        parse_payload_package(tmp_path, kind, {})


@pytest.mark.parametrize('path,contents', [
    ('../outside.json', '{}'), ('/outside.json', '{}'), ('bad\\path.json', '{}'),
    ('missing.json', None), ('payload.json', '{"type":"object","type":"object"}'),
    ('payload.json', '{"$ref":"https://example.test/schema"}'),
    ('payload.json', ' ' * (1024 * 1024 + 1)),
], ids=['parent', 'absolute', 'backslash', 'missing', 'duplicate-key', 'external-reference', 'oversize'])
def test_payload_schemas_reuse_bounded_local_schema_validation(tmp_path, path, contents):
    if contents is not None:
        (tmp_path / 'payload.json').write_text(contents)
    with pytest.raises(PluginManifestError, match='payload_schemas'):
        parse_payload_package(tmp_path, 'effector', {'command': path})


def stimulator():
    owner = Stimulator(settings_snapshot=prepared_snapshot({'PLUGINS.bindings': [
        binding('stimulation.paradigm', 'paradigm', 'STIMULATION_THUBCI_SSVEP_classic40'),
    ]}))
    owner._load_paradigm()
    owner.paradigm = Mock()
    owner.paradigm.present_trial.return_value = owner.paradigm.present_result.return_value = None
    return owner


@pytest.mark.parametrize('method,valid', [('present_trial', {'event_id': 0}), ('present_result', {'target_id': 3})])
def test_stimulation_dispatches_copied_objects_to_only_the_v1_method(method, valid):
    owner = stimulator()
    getattr(owner, method)(valid)
    callback = getattr(owner.paradigm, method)
    callback.assert_called_once_with(valid)
    assert callback.call_args.args[0] is not valid


@pytest.mark.parametrize('method,value', [
    ('present_trial', None), ('present_trial', 1), ('present_trial', {}),
    ('present_trial', {'event_id': True}), ('present_trial', {'event_id': -1}),
    ('present_trial', {'event_id': 41}), ('present_trial', {'event_id': 1, 'revision': 'private'}),
    ('present_result', 3), ('present_result', {'target_id': 0}),
    ('present_result', {'target_id': 1.5}), ('present_result', {'target_id': True}),
])
def test_stimulation_rejects_bad_payloads_before_the_plugin_callback(method, value):
    owner = stimulator()
    with pytest.raises(ValueError):
        getattr(owner, method)(value)
    assert not owner.paradigm.mock_calls


@pytest.mark.parametrize('method,value', [('present_trial', {'event_id': 1}), ('present_result', {'target_id': 2})])
def test_stimulation_callbacks_must_return_none(method, value):
    owner = stimulator()
    getattr(owner.paradigm, method).return_value = False
    with pytest.raises(TypeError, match='None'):
        getattr(owner, method)(value)


@pytest.mark.parametrize('command', [None, {'target_id': True}, {'target_id': 1.5}, {'target_id': 2, 'seq': 9},
                                      {'target_id': 2, 'unknown': 1}])
def test_effector_schema_rejects_bad_payloads_before_execute(command):
    owner = Actuator(settings_snapshot=prepared_snapshot({'PLUGINS.bindings': [binding('actuation.effector', 'effector', 'logger')]}))
    owner.setup()
    callback = owner.effector.execute = Mock(return_value=None)
    try:
        with pytest.raises(ValueError):
            owner.execute(command)
        callback.assert_not_called()
    finally:
        owner.teardown()


def test_optional_decoding_values_are_omitted_from_device_payload_when_absent():
    command = validate_actuation_command(actuation_payload(
        {'target_id': 1, 'event_id': None, 'confidence': None}, command_id='command-7', issued_at=3.0))
    assert command.parameters == {'target_id': 1}
    assert command.command_id == 'command-7' and command.issued_at == 3.0


@pytest.mark.parametrize('factory', [StartStimTrialCommand, StartTrialRequest])
@pytest.mark.parametrize('values', [
    {}, {'trial_params': [1]}, {'trial_params': [None]}, {'trial_params': []},
    {'n_trials': True, 'trial_params': [{}]}, {'n_trials': '1', 'trial_params': [{}]},
    {'n_trials': 1.5, 'trial_params': [{}]}, {'n_trials': 0, 'trial_params': []},
    {'n_trials': 2, 'trial_params': [{}]}, {'trial_params': [{'x': float('nan')}]},
    {'trial_params': [{'x': 'x' * (1024 * 1024)}]},
])
def test_trial_batch_requires_bounded_explicit_json_objects(factory, values):
    with pytest.raises((TypeError, ValueError)):
        factory(**values)


@pytest.mark.parametrize('factory', [StartStimTrialCommand, StartTrialRequest])
def test_trial_batch_preserves_arbitrary_plugin_objects_without_borrowing_input(factory):
    parameters = [{'flashes': [1, 3]}, {'flashes': [2, 4]}]
    command = factory(n_trials=2, trial_params=parameters)
    parameters[0]['flashes'].append(9)
    assert command.trial_params == [{'flashes': [1, 3]}, {'flashes': [2, 4]}]
