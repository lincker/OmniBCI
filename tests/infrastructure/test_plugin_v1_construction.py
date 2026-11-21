"""The v1 author boundary exposes only role inputs and canonical domain values."""
import inspect
import sys
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock

import pytest

from omnibci import sdk
from omnibci.application.configuration.models import ProfileBinding
from omnibci.application.plugin_schema import validate_parameters
from omnibci.domain.signal_info import ChannelDescriptor, SignalInfo
from omnibci.infrastructure.plugins.loader import PluginLoader, PluginLoadError
from omnibci.infrastructure.plugins.plugin_catalog import PluginCatalog
from omnibci.infrastructure.plugins.runtime_configuration import load_configured_plugin
from tests.infrastructure.test_builtin_contributions import CAPABILITIES
from tests.plugin_configuration_helpers import builtin_selection, prepared_snapshot


PROCESSING = {'filter', 'decoder', 'epoch_detector'}


def input_signal():
    return SignalInfo(sample_rate=250, channels=(
        ChannelDescriptor(0, 'Oz', channel_id='oz', signal_type='eeg'),
    ))


def test_sdk_has_only_the_nineteen_v1_exports():
    assert set(sdk.__all__) == {
        'SignalSource', 'Filter', 'Decoder', 'EpochDetector', 'Paradigm', 'TriggerEmitter',
        'Effector', 'SignalInfo', 'ChannelDescriptor', 'SourceObservation', 'SourceCheckpoint',
        'Prediction', 'EpochBoundary', 'RenderSurface', 'Color', 'Rect', 'TextureHandle',
        'TriggerOutput', 'FrameTiming',
    }
    for name in ('PluginContext', 'PluginParameters', 'SignalChunk', 'Renderer', 'FilterContext'):
        assert not hasattr(sdk, name)


@pytest.mark.parametrize('kind,alias,port', CAPABILITIES)
def test_each_builtin_declares_only_required_keyword_constructor_inputs(monkeypatch, kind, alias, port):
    descriptor = PluginCatalog().resolve_selection(kind, builtin_selection(kind, alias))
    if kind == 'trigger':
        monkeypatch.setattr(sys, 'platform', 'win32')
    implementation = PluginLoader.load_entrypoint(descriptor, port)
    parameters = inspect.signature(implementation).parameters
    expected = {'parameters', 'resource_root'} | ({'input_signal'} if kind in PROCESSING else set())
    assert set(parameters) == expected
    assert all(item.kind is inspect.Parameter.KEYWORD_ONLY and item.default is inspect.Parameter.empty
               for item in parameters.values())


@pytest.mark.parametrize('kind,alias,port', CAPABILITIES)
def test_all_builtin_kinds_construct_through_api_one(monkeypatch, tmp_path, kind, alias, port):
    descriptor = PluginCatalog().resolve_selection(kind, builtin_selection(kind, alias))
    assert descriptor.api_version == '1'
    parameters = validate_parameters(descriptor.configuration_schema, {})
    if alias == 'file_reader':
        parameters['data_file_path'] = str(tmp_path / 'recording.cnt')
    if kind == 'trigger':
        monkeypatch.setattr(sys, 'platform', 'win32')
    kwargs = {'parameters': parameters}
    if kind in PROCESSING:
        kwargs['input_signal'] = input_signal()
    instance = PluginLoader.instantiate(descriptor, port, **kwargs)
    assert isinstance(instance, port)


@pytest.mark.parametrize('kind,alias,port', CAPABILITIES)
def test_host_does_not_pass_its_revision_or_upstream_signal(monkeypatch, kind, alias, port):
    descriptor = PluginCatalog().resolve_selection(kind, builtin_selection(kind, alias))
    slot = {'acquisition': 'acquisition.source', 'trigger': 'stimulation.trigger',
            'paradigm': 'stimulation.paradigm', 'effector': 'actuation.effector',
            'decoder': 'processing.decoder', 'epoch_detector': 'processing.epoch_detector',
            'filter': 'processing.filter.00000000-0000-0000-0000-000000000001'}[kind]
    binding = ProfileBinding(slot, kind, None, descriptor.selection, {},
                             configuration_version=descriptor.configuration_version)
    snapshot = {'PLUGINS.bindings': [binding.to_dict()], 'ACQUISITION.sample_rate': 1000,
                'CONFIG.module_revisions': {'role': 'private-revision'}}
    loader = Mock()
    if kind == 'trigger':
        monkeypatch.setattr(sys, 'platform', 'win32')
    snapshot = prepared_snapshot(snapshot)
    kwargs = {'input_signal': input_signal()} if kind in PROCESSING else {}
    load_configured_plugin(snapshot, slot, kind, port, loader=loader, **kwargs)
    assert set(loader.instantiate.call_args.kwargs) == {'parameters'} | set(kwargs)
    if kwargs:
        assert loader.instantiate.call_args.kwargs['input_signal'] is kwargs['input_signal']


def test_loader_provides_the_selected_resource_path_and_copies_only_parameters(monkeypatch):
    descriptor = PluginCatalog().resolve_selection('effector', builtin_selection('effector', 'logger'))
    descriptor = replace(descriptor, package=replace(descriptor.package, api_version='1'),
                         configuration_schema=None)
    constructor = Mock()
    monkeypatch.setattr(PluginLoader, 'load_entrypoint', Mock(return_value=constructor))
    parameters = {'items': [1, 2]}
    PluginLoader.instantiate(descriptor, sdk.Effector, parameters=parameters)
    supplied = constructor.call_args.kwargs
    assert set(supplied) == {'parameters', 'resource_root'}
    assert isinstance(supplied['resource_root'], Path)
    assert supplied['resource_root'] == descriptor.location
    supplied['parameters']['items'].append(3)
    assert parameters == {'items': [1, 2]}


@pytest.mark.parametrize('kind,kwargs', [
    ('decoder', {'parameters': {}}),
    ('decoder', {'parameters': {}, 'input_signal': None}),
    ('effector', {'parameters': {}, 'input_signal': input_signal()}),
    ('effector', {'parameters': {}, 'context': object()}),
    ('effector', {'parameters': {}, 'resource_root': Path('/tmp')}),
])
def test_invalid_role_inputs_are_rejected_before_import(monkeypatch, kind, kwargs):
    alias = 'logger' if kind == 'effector' else 'THUBCI_SSVEP_classic40CCA'
    descriptor = PluginCatalog().resolve_selection(kind, builtin_selection(kind, alias))
    descriptor = replace(descriptor, package=replace(descriptor.package, api_version='1'))
    load = Mock(side_effect=AssertionError('Invalid inputs must fail before import'))
    monkeypatch.setattr(PluginLoader, 'load_entrypoint', load)
    with pytest.raises(PluginLoadError):
        PluginLoader.instantiate(descriptor, sdk.Effector if kind == 'effector' else sdk.Decoder, **kwargs)
    load.assert_not_called()


@pytest.mark.parametrize('value', [None, {}])
def test_runtime_requires_prepared_input_even_without_channel_requirements(value):
    descriptor = PluginCatalog().resolve_selection('filter', builtin_selection('filter', 'BASELINE_CORRECTION'))
    slot = 'processing.filter.00000000-0000-0000-0000-000000000001'
    binding = ProfileBinding(slot, 'filter', None, descriptor.selection, {})
    loader = Mock()
    with pytest.raises(PluginLoadError, match='prepared input'):
        load_configured_plugin({'PLUGINS.bindings': [binding.to_dict()]}, slot, 'filter', sdk.Filter,
                               input_signal=value, loader=loader)
    loader.instantiate.assert_not_called()


def test_constructor_size_limit_includes_schema_defaults(monkeypatch):
    descriptor = PluginCatalog().resolve_selection('effector', builtin_selection('effector', 'logger'))
    descriptor = replace(descriptor, configuration_schema={
        'type': 'object', 'additionalProperties': False,
        'properties': {'huge': {'type': 'string', 'default': 'x' * (1024 * 1024)}},
    })
    load = Mock(side_effect=AssertionError('Oversize inputs must fail before import'))
    monkeypatch.setattr(PluginLoader, 'load_entrypoint', load)
    with pytest.raises(PluginLoadError, match='1 MiB'):
        PluginLoader.instantiate(descriptor, sdk.Effector, parameters={})
    load.assert_not_called()
