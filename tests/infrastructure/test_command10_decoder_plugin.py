"""Install and execute Command10 FBCCA through the actual plugin and epoch hosts."""
import inspect
from pathlib import Path

import numpy as np
import pytest

from omnibci.application.plugin_management.identity import PluginSelection
from omnibci.application.plugin_management.schema import validate_parameters
from omnibci.domain.signal import SignalChunk
from omnibci.infrastructure.plugin_management.loader import PluginLoader, PluginLoadError
from omnibci.infrastructure.plugin_management.installer import PluginPackageInstaller
from omnibci.infrastructure.plugin_management.catalog import PluginCatalog, PluginRoot
from omnibci.infrastructure.plugin_management.project_tools import package_project, validate_project
from omnibci.infrastructure.signal_processing.epoch.epoch_processor import EpochProcessor
from omnibci.sdk import Decoder
from tests.infrastructure.test_command10_decoder import PACKAGE, DEFAULTS, signal_info, synthetic


SELECTION = PluginSelection('OmniBCI.command10_fbcca', 'default', '0.1.0')


def test_builtin_manifest_defaults_and_channel_requirements():
    descriptor = PluginCatalog().resolve_selection('decoder', SELECTION)
    assert descriptor.location == PACKAGE
    assert descriptor.api_version == '1'
    assert descriptor.channel_requirements.signal_type == 'eeg'
    assert descriptor.channel_requirements.min_count == 1
    assert validate_parameters(descriptor.configuration_schema, {}) == DEFAULTS


@pytest.mark.parametrize('parameters', [
    {'window_duration': .9}, {'window_duration': 5}, {'offset_time': -1}, {'offset_time': True},
    {'min_score': -1}, {'min_score': 2}, {'min_margin': '0.05'}, {'min_margin': float('nan')},
    {'target_frequencies': [8]},
])
def test_schema_rejects_invalid_parameters_before_execution(parameters):
    descriptor = PluginCatalog().resolve_selection('decoder', SELECTION)
    with pytest.raises(ValueError):
        validate_parameters(descriptor.configuration_schema, parameters)


def test_loader_rejects_window_extending_past_stimulation():
    descriptor = PluginCatalog().resolve_selection('decoder', SELECTION)
    with pytest.raises(PluginLoadError, match='four-second'):
        PluginLoader.instantiate(descriptor, Decoder, input_signal=signal_info(),
                                 parameters={'window_duration': 4, 'offset_time': .14})


def test_standalone_zip_installs_with_all_protocol_files_and_decodes(tmp_path):
    archive = tmp_path / 'command10-fbcca.zip'
    validated = validate_project(PACKAGE)
    assert package_project(PACKAGE, archive) == validated
    assert validated['contributions'] == {'default': 'decoder'}
    assert set(validated['files']) == {'__init__.py', 'decoder.py', 'targets.py', 'README.md',
                                      'manifest.toml', 'default.schema.json'}
    installed = PluginPackageInstaller(tmp_path / 'installed').install(archive)
    catalog = PluginCatalog({'decoder': lambda: [PluginRoot(installed.location, 'user', 0)]})
    descriptor = catalog.resolve_selection('decoder', SELECTION)
    instance = PluginLoader.instantiate(descriptor, Decoder, parameters={}, input_signal=signal_info())
    assert Path(inspect.getfile(type(instance))).is_relative_to(installed.location)
    try:
        instance.setup()
        for target_id in range(1, 11):
            instance.begin_epoch()
            data = synthetic(target_id)
            result = instance.decode(data, latest_raw_segment=data[:, -25:])
            assert result.target_id == target_id
    finally:
        instance.teardown()


def test_epoch_host_keeps_cue_labels_separate_and_resets_repeated_free_choice():
    descriptor = PluginCatalog().resolve_selection('decoder', SELECTION)
    decoder = PluginLoader.instantiate(descriptor, Decoder, parameters={}, input_signal=signal_info())
    try:
        decoder.setup()
        for event_id, target_id in ((1, 9), (11, 5), (11, 10)):
            data = synthetic(target_id)
            markers = np.zeros(data.shape[1])
            markers[0] = event_id
            raw = np.vstack((data, markers))
            epoch = EpochProcessor(decoder, sample_rate=250)
            epoch.initialize_epoch(SignalChunk(1, 0, raw[:, :300]), 0, event_id)
            assert epoch.decode_if_ready() == (False, None)
            complete, classification = epoch.process_chunk(SignalChunk(2, 1, raw[:, 300:785]))
            assert complete
            assert classification.event_id == event_id
            assert classification.target_id == target_id
            assert 0 <= classification.confidence <= 1
            assert not classification.forced
            assert epoch.decode_if_ready() == (False, None)
        short = EpochProcessor(decoder, sample_rate=250)
        short.initialize_epoch(SignalChunk(3, 0, raw[:, :100]), 0, 11)
        assert short.force_decode_at_boundary() == (False, None)
    finally:
        decoder.teardown()
