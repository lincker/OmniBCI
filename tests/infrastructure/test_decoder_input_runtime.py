"""Decoder requirements are checked against prepared input before plugin code runs."""
from pathlib import Path
from tests.plugin_configuration_helpers import builtin_selection, prepared_snapshot
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np
import pytest

from omnibci.application.plugin_identity import PluginSelection
from omnibci.application.configuration.models import ProfileBinding
from omnibci.domain.decoder_input import DecoderChannelRequirements
from omnibci.domain.signal_info import SignalInfo, ChannelDescriptor
from omnibci.domain.channel_mapping import ChannelBinding, ProcessingInput, compile_processing_plan
from omnibci.domain.ports.algorithm import Decoder
from omnibci.domain.signal import SignalChunk
from omnibci.infrastructure.signal_processing.signal_configuration import mapped_input
from omnibci.infrastructure.plugins.acquisition.neuroscan.curry8.curry8 import Curry8DataReader, Curry8Metadata
from omnibci.infrastructure.plugins.loader import PluginLoadError
from omnibci.infrastructure.plugins.plugin_catalog import PluginCatalog
from omnibci.infrastructure.plugins.runtime_configuration import load_configured_plugin


def configured(monkeypatch, requirements, outputs=("O1",), *, signal_type="eeg"):
    source = SignalInfo(sample_rate=250, channels=(
        ChannelDescriptor(0, "O1", signal_type=signal_type, unit="uV", channel_id="channel-0"),
        ChannelDescriptor(1, "Oz", signal_type="eeg", unit="uV", channel_id="channel-1"),
        ChannelDescriptor(2, "M1", signal_type="eeg", unit="uV", channel_id="channel-2"),
    ))
    ids = {channel.name: channel.channel_id for channel in source.channels}
    inputs = ProcessingInput(outputs=tuple(ChannelBinding(name, ids[name]) for name in outputs))
    selection = PluginSelection("test.decoder", "default", "1")
    binding = ProfileBinding("processing.decoder", "decoder", None, selection, {}, processing_input=inputs)
    snapshot = {"PLUGINS.bindings": [binding.to_dict()]}
    plan = compile_processing_plan(source, inputs)
    descriptor = SimpleNamespace(channel_requirements=requirements,
                                 api_version="1", configuration_version=1)
    monkeypatch.setattr("omnibci.infrastructure.plugins.runtime_configuration.resolve_configured_plugin",
                        lambda snapshot, slot, kind: (descriptor, {}))
    return snapshot, mapped_input(plan)


@pytest.mark.parametrize("requirements, fields", [
    (DecoderChannelRequirements(required_names=("Oz",)), {}),
    (DecoderChannelRequirements(min_count=2), {}),
    (DecoderChannelRequirements(signal_type="eeg"), {"signal_type": "eog"}),
    (DecoderChannelRequirements(required_names=("M1",)), {}),
])
def test_invalid_final_input_never_constructs_decoder(monkeypatch, requirements, fields):
    snapshot, context = configured(monkeypatch, requirements, **fields)
    loader = Mock()
    with pytest.raises(PluginLoadError):
        load_configured_plugin(snapshot, "processing.decoder", "decoder", Decoder,
                               input_signal=context, loader=loader)
    loader.instantiate.assert_not_called()


def test_required_names_allow_extra_channels_and_profile_order(monkeypatch):
    requirements = DecoderChannelRequirements(signal_type="eeg", min_count=2, required_names=("O1",))
    snapshot, context = configured(monkeypatch, requirements, outputs=("Oz", "O1"))
    loader = Mock()
    result = load_configured_plugin(snapshot, "processing.decoder", "decoder", Decoder,
                                    input_signal=context, loader=loader)
    assert result is loader.instantiate.return_value
    assert [channel.name for channel in loader.instantiate.call_args.kwargs["input_signal"].channels] == ["Oz", "O1"]


def test_declared_requirements_cannot_run_without_verified_channel_metadata(monkeypatch):
    snapshot, _ = configured(monkeypatch, DecoderChannelRequirements(signal_type="eeg"))
    loader = Mock()
    with pytest.raises(PluginLoadError, match="prepared input"):
        load_configured_plugin(snapshot, "processing.decoder", "decoder", Decoder,
                               input_signal=None, loader=loader)
    loader.instantiate.assert_not_called()


def test_processing_plugin_receives_its_prepared_signal(monkeypatch):
    snapshot, signal = configured(monkeypatch, None)
    loader = Mock()
    load_configured_plugin(snapshot, "processing.decoder", "decoder", Decoder,
                           input_signal=signal, loader=loader)
    loader.instantiate.assert_called_once()


@pytest.mark.asyncio
async def test_curry_unknown_types_require_processing_declaration_before_builtin_cca():
    reader = Curry8DataReader(parameters={}, resource_root=Path(__file__).parent)
    reader._is_connected = True
    reader._metadata = Curry8Metadata(803, 3, 500, 4, False, False, ("C3", "C4", "Trigger"))
    from unittest.mock import AsyncMock
    reader._load_metadata = AsyncMock(side_effect=lambda: setattr(reader, "_metadata", Curry8Metadata(803, 3, 500, 4, False, False, ("C3", "C4", "Trigger"))))
    observation = await reader.observe()
    raw_metadata = observation.native_text
    source = observation.signal_info
    assert "channel_types" not in raw_metadata
    assert all(channel.signal_type == "unknown" for channel in source.channels)
    catalog = PluginCatalog()
    definition = catalog.resolve_selection("decoder", builtin_selection("decoder", "THUBCI_SSVEP_classic40CCA"))
    undeclared = ProcessingInput()
    binding = ProfileBinding("processing.decoder", "decoder", None, definition.selection,
                             {"window_duration": 1.0, "offset_time": 0}, processing_input=undeclared)
    snapshot = prepared_snapshot({"PLUGINS.bindings": [binding.to_dict()]})
    with pytest.raises(PluginLoadError, match="eeg"):
        load_configured_plugin(snapshot, "processing.decoder", "decoder", Decoder,
                               input_signal=mapped_input(compile_processing_plan(source, undeclared)))

    declared = ProcessingInput(outputs=(ChannelBinding("Right", "channel-1", "eeg"),))
    binding = ProfileBinding("processing.decoder", "decoder", None, definition.selection,
                             binding.parameters, processing_input=declared)
    snapshot = prepared_snapshot({"PLUGINS.bindings": [binding.to_dict()]})
    plan = compile_processing_plan(source, declared)
    decoder = load_configured_plugin(snapshot, "processing.decoder", "decoder", Decoder,
                                     input_signal=mapped_input(plan))
    decoder.setup()
    decoder.begin_epoch()
    times = np.arange(501) / source.sample_rate
    signal = np.sin(2 * np.pi * 8 * times)
    chunk = SignalChunk(123, 1, np.vstack((np.zeros(501), signal, np.zeros(501))))
    mapped = plan.apply(chunk)
    np.testing.assert_array_equal(mapped.data[0], signal)
    np.testing.assert_array_equal(mapped.triggers, chunk.triggers)
    assert decoder.should_decode(mapped.data)
    assert decoder.decode(mapped.data, latest_raw_segment=mapped.data[:, -1:]).target_id == 1
    decoder.teardown()
    assert all(channel.signal_type == "unknown" for channel in source.channels)
    assert "channel_types" not in raw_metadata
