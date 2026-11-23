"""Independent Command10 package discovery, schemas and installed resources."""
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from omnibci.application.plugin_management.identity import PluginSelection
from omnibci.application.plugin_management.schema import validate_parameters
from omnibci.infrastructure.plugin_management.loader import PluginLoader
from omnibci.infrastructure.plugin_management.installer import PluginPackageInstaller
from omnibci.infrastructure.plugin_management.catalog import PluginCatalog, PluginRoot
from omnibci.infrastructure.plugin_management.project_tools import package_project, validate_project
from omnibci.sdk import FrameTiming, Paradigm, RenderSurface, TextureHandle, TriggerOutput


PACKAGE = Path(__file__).resolve().parents[2] / 'omnibci/infrastructure/plugins/paradigms/command10SSVEP'
SELECTION = PluginSelection('OmniBCI.command10SSVEP', 'default', '0.1.0')


def test_builtin_is_a_standalone_paradigm_with_generic_trial_controls():
    catalog = PluginCatalog()
    descriptor = catalog.resolve_selection('paradigm', SELECTION)
    assert descriptor.location == PACKAGE
    assert descriptor.api_version == '1'
    assert descriptor.controls['target_cue']['payload_key'] == 'cue_target_id'
    assert descriptor.controls['target_cue']['default_sequence'] == '1-10'
    assert not any(item.identifier == SELECTION.package_id for item in catalog.list_contributions('decoder'))
    assert validate_parameters(descriptor.configuration_schema, {}) == {}
    for cue in range(11):
        assert validate_parameters(descriptor.payload_schemas['trial'], {'cue_target_id': cue}) == {'cue_target_id': cue}
    for target_id in range(1, 11):
        assert validate_parameters(descriptor.payload_schemas['result'], {'target_id': target_id}) == {'target_id': target_id}


@pytest.mark.parametrize('schema, payload', [
    ('trial', {}), ('trial', {'event_id': 1}), ('trial', {'cue_target_id': -1}),
    ('trial', {'cue_target_id': 11}), ('trial', {'cue_target_id': True}),
    ('trial', {'cue_target_id': 1, 'extra': 1}), ('result', {}),
    ('result', {'target_id': 0}), ('result', {'target_id': 11}), ('result', {'target_id': False}),
])
def test_invalid_payloads_are_rejected_before_plugin_callbacks(schema, payload):
    descriptor = PluginCatalog().resolve_selection('paradigm', SELECTION)
    with pytest.raises(ValueError):
        validate_parameters(descriptor.payload_schemas[schema], payload, apply_defaults=False)


def test_installed_zip_is_complete_and_executes_without_source_resources(tmp_path):
    archive = tmp_path / 'command10.zip'
    validated = validate_project(PACKAGE)
    packaged = package_project(PACKAGE, archive)
    assert packaged == validated
    assert packaged['contributions'] == {'default': 'paradigm'}
    assert len([name for name in packaged['files'] if name.endswith('.png')]) == 11
    installed = PluginPackageInstaller(tmp_path / 'installed').install(archive)
    catalog = PluginCatalog({'paradigm': lambda: [PluginRoot(installed.location, 'user', 0)]})
    descriptor = catalog.resolve_selection('paradigm', SELECTION)
    instance = PluginLoader.instantiate(descriptor, Paradigm, parameters={})
    assert instance._resource_root == installed.location
    renderer, trigger = MagicMock(spec=RenderSurface), MagicMock(spec=TriggerOutput)
    renderer.upload_texture.side_effect = lambda pixels: TextureHandle()
    try:
        instance.setup(renderer=renderer, trigger=trigger)
        assert renderer.upload_texture.call_count == 11
        instance.present_trial({'cue_target_id': 0})
        assert instance.render_frame(FrameTiming(refresh_index=0, target_time=0, refresh_period=1 / 60))
        trigger.send.assert_called_once_with(11)
        instance.present_result({'target_id': 10})
        instance.render_frame(FrameTiming(refresh_index=240, target_time=4, refresh_period=1 / 60))
    finally:
        instance.teardown()
    assert renderer.release_texture.call_count == 11
