"""Decoder requirements are checked against prepared input before plugin code runs."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from omnibci.application.plugin_profiles import PluginSelection, ProfileBinding
from omnibci.domain.decoder_input import DecoderChannelRequirements
from omnibci.domain.montage import ChannelDescriptor, ProcessingInput, ReferenceSpec, SourceMetadata, compile_processing_plan
from omnibci.domain.ports.algorithm import Decoder
from omnibci.infrastructure.neuroengine.signal_processing.signal_configuration import signal_context
from omnibci.infrastructure.plugins.loader import PluginLoadError
from omnibci.infrastructure.plugins.runtime_configuration import load_configured_plugin
from omnibci.sdk import PluginContext


def configured(requirements, outputs=("O1",), *, signal_type="eeg", reference=None):
    source = SourceMetadata("source", 250, (
        ChannelDescriptor(0, "O1", signal_type=signal_type, unit="uV"),
        ChannelDescriptor(1, "Oz", unit="uV"),
        ChannelDescriptor(2, "M1", signal_type="reference", unit="uV"),
    ), 3, original_reference="Cz")
    inputs = ProcessingInput(outputs=outputs, reference=reference or ReferenceSpec())
    selection = PluginSelection("test.decoder", "default", "1")
    binding = ProfileBinding("processing.decoder", "decoder", None, selection, {}, processing_input=inputs)
    snapshot = {"PLUGINS.bindings": [binding.to_dict()]}
    plan = compile_processing_plan(source, inputs)
    descriptor = SimpleNamespace(signal_input_contract=2, channel_requirements=requirements,
                                 api_version="2", configuration_version=1)
    catalog = Mock()
    catalog.resolve_selection.return_value = descriptor
    return snapshot, signal_context(snapshot, plan), catalog


@pytest.mark.parametrize("requirements, fields", [
    (DecoderChannelRequirements(required_names=("Oz",)), {}),
    (DecoderChannelRequirements(min_count=2), {}),
    (DecoderChannelRequirements(signal_type="eeg"), {"signal_type": "eog"}),
    (DecoderChannelRequirements(required_names=("M1",)),
     {"reference": ReferenceSpec(mode="single", channels=("M1",))}),
])
def test_invalid_final_input_never_constructs_decoder(requirements, fields):
    snapshot, context, catalog = configured(requirements, **fields)
    loader = Mock()
    with pytest.raises(PluginLoadError):
        load_configured_plugin(snapshot, "processing.decoder", "decoder", Decoder,
                               context=context, catalog=catalog, loader=loader)
    loader.instantiate.assert_not_called()


def test_required_names_allow_extra_channels_and_profile_order():
    requirements = DecoderChannelRequirements(signal_type="eeg", min_count=2, required_names=("O1",))
    snapshot, context, catalog = configured(requirements, outputs=("Oz", "O1"))
    loader = Mock()
    result = load_configured_plugin(snapshot, "processing.decoder", "decoder", Decoder,
                                    context=context, catalog=catalog, loader=loader)
    assert result is loader.instantiate.return_value
    assert [channel.name for channel in loader.instantiate.call_args.kwargs["context"].input_signal.channels] == ["Oz", "O1"]


def test_declared_requirements_cannot_run_without_verified_channel_metadata():
    snapshot, _, catalog = configured(DecoderChannelRequirements(signal_type="eeg"))
    loader = Mock()
    with pytest.raises(PluginLoadError, match="prepared input"):
        load_configured_plugin(snapshot, "processing.decoder", "decoder", Decoder,
                               context=PluginContext(sample_rate=250), catalog=catalog, loader=loader)
    loader.instantiate.assert_not_called()


def test_undeclared_legacy_plugin_keeps_its_existing_construction_path():
    snapshot, _, catalog = configured(None)
    catalog.resolve_selection.return_value.signal_input_contract = 1
    loader = Mock()
    load_configured_plugin(snapshot, "processing.decoder", "decoder", Decoder,
                           context=PluginContext(sample_rate=250), catalog=catalog, loader=loader)
    loader.instantiate.assert_called_once()
